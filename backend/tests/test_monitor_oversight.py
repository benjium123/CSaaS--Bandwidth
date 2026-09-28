"""Monitoring oversight (LEFTOVER A10 steps 2-5): hourly rescore, shared-recipient signal,
daily ops digest, weekly random spot checks. None of them pauses or restricts anyone."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Call,
    MonitorSignal,
    Org,
    OrgMonitoring,
    OrgNumber,
    PlatformOperator,
    SecurityAlert,
    User,
)
from app.services import mailer, monitor_oversight, monitor_score
from tests.conftest import make_settings

UNSCOPED = {"allow_unscoped": True}


@pytest.fixture
def settings():
    return make_settings(monitor_enforced=True, monitor_auto_action=False)


async def _org(session, name="Co", active=True) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name=name, slug=f"ov-{org_id.hex[:10]}", is_active=active))
    await session.commit()
    return org_id


async def _call(session, org_id, to: str, when: datetime | None = None) -> None:
    set_org_context(session, org_id)
    session.add(
        Call(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="outbound",
            contact_e164=to,
            our_e164="+15125550000",
            carrier="telnyx",
            status="completed",
            **({"created_at": when} if when else {}),
        )
    )
    await session.commit()


async def _alerts(session, kind: str) -> list[SecurityAlert]:
    return list(
        (
            await session.execute(
                sa.select(SecurityAlert)
                .where(SecurityAlert.kind == kind)
                .execution_options(**UNSCOPED)
            )
        ).scalars()
    )


# ------------------------------------------------------------------------ rescore
async def test_rescore_lowers_a_score_once_its_signals_age_out(session, settings):
    org_id = await _org(session)
    set_org_context(session, org_id)
    old = datetime.now(timezone.utc) - timedelta(days=settings.monitor_signal_window_days + 1)
    session.add(
        MonitorSignal(
            id=uuid.uuid4(),
            org_id=org_id,
            kind="volume_spike",
            weight=40,
            summary="old burst",
            created_at=old,
        )
    )
    session.add(OrgMonitoring(id=uuid.uuid4(), org_id=org_id, score=40, level="watch"))
    await session.commit()

    assert await monitor_oversight.rescore_tick(session, settings) == 1
    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=False)
    assert (state.score, state.level) == (0, "normal")


async def test_rescore_does_not_repeat_a_pending_recommendation(session, settings):
    org_id = await _org(session)
    for n in range(3):  # 3 x 25 = 75: restricted, recommended only (not confirmed evidence)
        await monitor_score.add_signal(session, settings, org_id, "text_blocked", f"b{n}")
    await session.commit()
    first = len(await _alerts(session, "monitor_action_recommended"))
    assert first >= 1

    await monitor_oversight.rescore_tick(session, settings)
    await monitor_oversight.rescore_tick(session, settings)

    assert len(await _alerts(session, "monitor_action_recommended")) == first
    set_org_context(session, org_id)
    assert (await monitor_score.get_state(session, org_id, create=False)).level != "paused"


# ------------------------------------------------------------------------ overlap
async def test_the_same_list_worked_from_several_workspaces_is_signalled_once_a_day(
    session, settings
):
    orgs = [await _org(session, f"List {i}") for i in range(3)]
    shared = [f"+1214555{i:04d}" for i in range(5)]
    for org_id in orgs:
        for number in shared:
            await _call(session, org_id, number)
    loner = await _org(session, "Loner")
    for number in shared[:2]:
        await _call(session, loner, number)

    assert await monitor_oversight.overlap_tick(session, settings) == 3
    kinds = {
        (s.org_id, s.kind)
        for s in (
            await session.execute(sa.select(MonitorSignal).execution_options(**UNSCOPED))
        ).scalars()
    }
    assert {(o, "shared_recipients") for o in orgs} <= kinds
    assert (loner, "shared_recipients") not in kinds  # only 2 shared recipients
    assert await monitor_oversight.overlap_tick(session, settings) == 0  # once a day


async def test_platform_numbers_short_codes_and_old_calls_are_not_a_shared_list(session, settings):
    orgs = [await _org(session, f"Peer {i}") for i in range(3)]
    set_org_context(session, orgs[0])
    own = [f"+1469555{i:04d}" for i in range(5)]
    for number in own:
        session.add(OrgNumber(id=uuid.uuid4(), org_id=orgs[0], e164=number, carrier="telnyx"))
    await session.commit()
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    for org_id in orgs:
        for number in own:
            await _call(session, org_id, number)  # each other's (platform) numbers
        await _call(session, org_id, "933")
        for i in range(5):
            await _call(session, org_id, f"+1972555{i:04d}", when=stale)
    assert await monitor_oversight.overlap_tick(session, settings) == 0


# ------------------------------------------------------------------------ digest
async def test_the_digest_goes_to_ops_admins_once_a_day(session, settings):
    admin = User(id=uuid.uuid4(), email="ops@platform.example", hashed_password="x")
    session.add(admin)
    await session.flush()
    session.add(PlatformOperator(id=uuid.uuid4(), user_id=admin.id, role="admin", is_active=True))
    org_id = await _org(session, "Risky Ltd")
    set_org_context(session, org_id)
    session.add(OrgMonitoring(id=uuid.uuid4(), org_id=org_id, score=45, level="watch"))
    await session.commit()

    mailer.outbox.clear()
    assert await monitor_oversight.digest_tick(session, settings) is True
    sent = [m for m in mailer.outbox if "ops@platform.example" in m["To"]]
    assert len(sent) == 1
    body = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "Risky Ltd: 45 (watch)" in body

    mailer.outbox.clear()
    assert await monitor_oversight.digest_tick(session, settings) is None  # not due again
    assert mailer.outbox == []


async def test_a_quiet_day_sends_no_digest(session, settings):
    mailer.outbox.clear()
    assert await monitor_oversight.digest_tick(session, settings) is False
    assert mailer.outbox == []


# ------------------------------------------------------------------------ spot checks
async def test_weekly_spot_checks_pick_active_workspaces_only(session, settings):
    active = [await _org(session, f"Busy {i}") for i in range(5)]
    for org_id in active:
        await _call(session, org_id, "+13125550100")
    idle = await _org(session, "Idle")
    disabled = await _org(session, "Disabled", active=False)
    await _call(session, disabled, "+13125550100")

    # Room for everyone eligible, so the filter - not luck - decides who is queued.
    roomy = make_settings(monitor_spot_check_count=10)
    picked = await monitor_oversight.spot_check_tick(session, roomy, rng=random.Random(7))
    assert picked == 5
    queued = {a.org_id for a in await _alerts(session, "monitor_spot_check")}
    assert queued == set(active)
    assert idle not in queued and disabled not in queued
    assert await monitor_oversight.spot_check_tick(session, roomy) is None  # weekly


async def test_spot_checks_pick_the_configured_number(session, settings):
    for i in range(5):
        await _call(session, await _org(session, f"Busy {i}"), "+13125550100")
    assert await monitor_oversight.spot_check_tick(session, settings) == 3
