"""10DLC phase 2: customer notices (email + in-app) on every texting registration change."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.db.session import get_sessionmaker
from app.models import Notification, OrgMembership, Role, SecurityAlert, User
from app.services import mailer, tendlc
from tests.test_tendlc_self_serve import (  # noqa: F401
    _checkout,
    _pay,
    _settings,
    _stage,
    _workspace,
    stripe_stub,
    telnyx,
)
from tests.test_tendlc_webhook import _ev

_UNSCOPED = {ALLOW_UNSCOPED_KEY: True}


async def _owner(session, org_id) -> uuid.UUID:
    user = User(
        id=uuid.uuid4(), email=f"o-{uuid.uuid4().hex[:8]}@texting.test",
        hashed_password="x", is_active=True,
    )
    session.add(user)
    await session.flush()
    role = Role(
        id=uuid.uuid4(), org_id=org_id, name="owner", permissions=[], is_system=True
    )
    session.add(role)
    await session.flush()
    session.add(OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id))
    await session.commit()
    return user.id


async def _drive(session, telnyx, *, with_owner=True):
    org, brand, campaign, number = await _workspace(session)
    org_id = org.id
    owner_id = await _owner(session, org_id) if with_owner else None
    reg = await _checkout(session, org, brand, campaign)
    reg_id = reg.id
    await _pay(session, reg)
    sm = get_sessionmaker()
    mailer.outbox.clear()
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_filed"
    return org_id, owner_id, reg_id, sm


async def _notes(org_id):
    async with get_sessionmaker()() as s:
        return (
            (
                await s.execute(
                    sa.select(Notification)
                    .where(Notification.org_id == org_id, Notification.kind == "texting")
                    .order_by(Notification.created_at)
                    .execution_options(**_UNSCOPED)
                )
            )
            .scalars()
            .all()
        )


async def test_active_notifies_owner_once(session, stripe_stub, telnyx):
    org_id, owner_id, reg_id, sm = await _drive(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "active"
    notes = [n for n in await _notes(org_id) if n.user_id == owner_id]
    assert len([n for n in notes if "you can text now" in n.body]) == 1
    mails = [m for m in mailer.outbox if "you can text now" in m["Subject"]]
    assert len(mails) == 1


async def test_rejection_notice_carries_the_reason(session, stripe_stub, telnyx):
    org_id, owner_id, reg_id, sm = await _drive(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    ev = _ev(
        "10dlc.campaign.update",
        {
            "campaignId": "C456",
            "type": "MNO_REVIEW",
            "status": "REJECTED",
            "reasons": [{"description": "Opt-in is not described"}],
        },
        "n-e2",
    )
    mailer.outbox.clear()
    async with sm() as s:
        await tendlc.handle_carrier_event(s, _settings(), ev, verified=True, client=telnyx.client)
    assert await _stage(reg_id) == "campaign_rejected"
    notes = [n for n in await _notes(org_id) if "needs changes" in n.body]
    assert len(notes) == 1 and "Opt-in is not described" in notes[0].body
    mails = [m for m in mailer.outbox if "needs changes" in m["Subject"]]
    assert len(mails) == 1
    text = mails[0].get_body(preferencelist=("plain",)).get_content()
    assert "Opt-in is not described" in text
    assert "/settings/messaging" in text


async def test_paid_is_silent(session, stripe_stub, telnyx):
    org, brand, campaign, _ = await _workspace(session)
    org_id = org.id
    await _owner(session, org_id)
    reg = await _checkout(session, org, brand, campaign)
    mailer.outbox.clear()
    await _pay(session, reg)
    assert await _notes(org_id) == []
    assert mailer.outbox == []


async def test_no_owner_email_still_writes_nothing_and_does_not_crash(
    session, stripe_stub, telnyx
):
    org_id, _, reg_id, sm = await _drive(session, telnyx, with_owner=False)
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "active"
    assert await _notes(org_id) == []


async def test_failed_assignment_notifies_and_opens_ops_alert(session, stripe_stub, telnyx):
    org_id, owner_id, reg_id, sm = await _drive(session, telnyx)
    telnyx.fail_assignment = True
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    failed = [n for n in await _notes(org_id) if "could not be added" in n.body]
    assert len(failed) == 1
    async with sm() as s:
        alerts = (
            (
                await s.execute(
                    sa.select(SecurityAlert)
                    .where(SecurityAlert.kind == "tendlc_number_failed",
                           SecurityAlert.org_id == org_id)
                    .execution_options(**_UNSCOPED)
                )
            )
            .scalars()
            .all()
        )
    assert len(alerts) == 1
    assert alerts[0].detail["registration_id"] == str(reg_id)
