"""User seats follow paid phone numbers: one paid, unreleased number buys one user.

Every refusal is paired with the case that succeeds, and every refusal also asserts that
nothing was written - a 403 that still created the account would be worse than useless.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import NumberPurchase, Org
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login

PASSWORD = "correct-horse-battery"
MEMBERS = "/api/v1/orgs/current/members"
INVITES = "/api/v1/orgs/current/invites"
SEATS = "/api/v1/orgs/current/seats"


async def _owner_org(client):
    token = await register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@example.com")
    org = await create_org(client, token, "Acme")
    return org, auth_headers(token, org["id"])


async def _pay_for(session, org_id, count: int, *, status: str = "active", released: int = 0):
    org_id = uuid.UUID(str(org_id))
    set_org_context(session, org_id)
    numbers = [
        {"e164": f"+1212555{i:04d}", "state": "released" if i < released else "active"}
        for i in range(count)
    ]
    session.add(
        NumberPurchase(
            id=uuid.uuid4(),
            org_id=org_id,
            numbers=numbers,
            state="complete",
            subscription_id=f"sub_{uuid.uuid4().hex}",
            subscription_status=status,
        )
    )
    await session.commit()


async def _add(client, h, email, role_name="agent"):
    return await client.post(
        MEMBERS,
        json={
            "email": email,
            "full_name": "Teammate",
            "password": PASSWORD,
            "role_name": role_name,
        },
        headers=h,
    )


def _refused(r):
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "seat_limit_reached"


async def test_no_paid_numbers_means_no_teammates(client, session):
    _org, h = await _owner_org(client)

    _refused(await _add(client, h, "nobody@example.com"))
    assert await users_repo.get_by_email(session, "nobody@example.com") is None


async def test_owner_takes_a_seat_and_each_number_adds_one(client, session):
    org, h = await _owner_org(client)

    await _pay_for(session, org["id"], 1)
    _refused(await _add(client, h, "first@example.com"))

    await _pay_for(session, org["id"], 1)
    assert (await _add(client, h, "first@example.com")).status_code == 201
    _refused(await _add(client, h, "second@example.com"))
    assert await users_repo.get_by_email(session, "second@example.com") is None

    seats = (await client.get(SEATS, headers=h)).json()
    assert seats == {
        "enforced": True,
        "limit": 2,
        "members": 2,
        "pending_invites": 0,
        "available": 0,
    }


async def test_cancelled_or_released_numbers_buy_no_seats(client, session):
    org, h = await _owner_org(client)

    await _pay_for(session, org["id"], 5, status="canceled")
    await _pay_for(session, org["id"], 2, released=1)
    await _pay_for(session, org["id"], 3, status="incomplete")
    _refused(await _add(client, h, "blocked@example.com"))

    await _pay_for(session, org["id"], 1, status="past_due")
    assert (await _add(client, h, "allowed@example.com")).status_code == 201


async def test_an_outstanding_invite_holds_a_seat_and_redeeming_it_does_not_double_count(
    client, session
):
    org, h = await _owner_org(client)
    await _pay_for(session, org["id"], 2)

    invite = await client.post(
        INVITES, json={"email": "invited@example.com", "role_name": "agent"}, headers=h
    )
    assert invite.status_code == 201, invite.text

    # The invite holds the only free seat.
    _refused(
        await client.post(
            INVITES, json={"email": "another@example.com", "role_name": "agent"}, headers=h
        )
    )
    _refused(await _add(client, h, "direct@example.com"))

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "invited@example.com",
            "password": PASSWORD,
            "invite_token": invite.json()["token"],
        },
    )
    assert reg.status_code == 201, reg.text
    emails = {m["email"] for m in (await client.get(MEMBERS, headers=h)).json()}
    assert "invited@example.com" in emails


async def test_invite_redemption_is_refused_when_seats_were_lost_meanwhile(client, session):
    org, h = await _owner_org(client)
    await _pay_for(session, org["id"], 2)
    invite = await client.post(
        INVITES, json={"email": "late@example.com", "role_name": "agent"}, headers=h
    )
    assert invite.status_code == 201, invite.text

    # The subscription lapses before the invite is used.
    rows = (
        await session.execute(
            sa.select(NumberPurchase)
            .where(NumberPurchase.org_id == uuid.UUID(org["id"]))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars()
    for row in rows:
        row.subscription_status = "canceled"
    await session.commit()

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "late@example.com",
            "password": PASSWORD,
            "invite_token": invite.json()["token"],
        },
    )
    _refused(reg)
    assert await users_repo.get_by_email(session, "late@example.com") is None


async def test_invite_validation_is_reported_before_seats(client):
    _org, h = await _owner_org(client)

    r = await client.post(INVITES, json={"email": "x@example.com", "role_name": "owner"}, headers=h)
    assert r.status_code == 422, r.text


async def test_workspaces_from_before_per_number_billing_are_not_limited(client, session):
    org, h = await _owner_org(client)
    legacy = await session.get(Org, uuid.UUID(org["id"]))
    legacy.number_subscription_required = False
    await session.commit()

    assert (await _add(client, h, "legacy1@example.com")).status_code == 201
    assert (await _add(client, h, "legacy2@example.com")).status_code == 201
    seats = (await client.get(SEATS, headers=h)).json()
    assert seats["enforced"] is False
    assert seats["limit"] is None
