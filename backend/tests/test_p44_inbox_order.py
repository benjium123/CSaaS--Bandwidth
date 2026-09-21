"""P44: per-member custom ordering of the Lines rail.

``PUT /api/v1/me/inbox-order`` saves one member's dragged order (org_memberships.
inbox_order); ``GET /api/v1/inboxes`` returns it in that order once saved, and in a
computed default (own numbers, then department numbers, then everything else, each
group alphabetical) before that member has ever dragged anything.
"""

from __future__ import annotations

import uuid

from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login

PASSWORD = "correct-horse-battery"


async def _owner_org(client):
    token = await register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@example.com")
    org = await create_org(client, token, "Acme")
    return token, org, auth_headers(token, org["id"])


async def _add_number(client, headers, e164: str) -> None:
    r = await client.post("/api/v1/numbers", json={"e164": e164}, headers=headers)
    assert r.status_code == 201, r.text


async def _inboxes(client, headers) -> list[dict]:
    r = await client.get("/api/v1/inboxes", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _inbox_id_for(client, headers, e164: str) -> str:
    return next(i for i in await _inboxes(client, headers) if i["e164"] == e164)["id"]


async def _create_member(client, headers, *, email: str, inbox_ids=None) -> str:
    body = {
        "email": email,
        "full_name": "Teammate",
        "password": PASSWORD,
        "role_name": "agent",
    }
    if inbox_ids is not None:
        body["inbox_ids"] = inbox_ids
    r = await client.post("/api/v1/orgs/current/members", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


async def _login(client, email: str, org_id: str) -> dict:
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return auth_headers(r.json()["access_token"], org_id)


async def _set_grants(client, headers, inbox_id: str, grants: list[dict]) -> None:
    r = await client.put(
        f"/api/v1/inboxes/{inbox_id}/grants", json={"grants": grants}, headers=headers
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------------------
# Custom order
# --------------------------------------------------------------------------------------
async def test_saved_order_is_applied_and_reversible(client):
    _token, org, h = await _owner_org(client)
    for e164 in ("+12145553001", "+12145553002", "+12145553003"):
        await _add_number(client, h, e164)
    ids = {e164: await _inbox_id_for(client, h, e164) for e164 in
           ("+12145553001", "+12145553002", "+12145553003")}

    reversed_order = [ids["+12145553003"], ids["+12145553002"], ids["+12145553001"]]
    r = await client.put(
        "/api/v1/me/inbox-order", json={"inbox_ids": reversed_order}, headers=h
    )
    assert r.status_code == 204, r.text

    got = [i["id"] for i in await _inboxes(client, h)]
    assert got == reversed_order


async def test_saved_order_ignores_foreign_and_duplicate_ids(client):
    _token, org, h = await _owner_org(client)
    await _add_number(client, h, "+12145554001")
    await _add_number(client, h, "+12145554002")
    a = await _inbox_id_for(client, h, "+12145554001")
    b = await _inbox_id_for(client, h, "+12145554002")
    foreign = str(uuid.uuid4())

    r = await client.put(
        "/api/v1/me/inbox-order",
        json={"inbox_ids": [b, foreign, a, a]},
        headers=h,
    )
    assert r.status_code == 204, r.text

    got = [i["id"] for i in await _inboxes(client, h)]
    assert got == [b, a]


async def test_inbox_granted_after_the_save_appends_at_the_end(client):
    _token, org, h = await _owner_org(client)
    await _add_number(client, h, "+12145555001")
    a = await _inbox_id_for(client, h, "+12145555001")

    r = await client.put("/api/v1/me/inbox-order", json={"inbox_ids": [a]}, headers=h)
    assert r.status_code == 204, r.text

    await _add_number(client, h, "+12145555002")
    b = await _inbox_id_for(client, h, "+12145555002")

    got = [i["id"] for i in await _inboxes(client, h)]
    assert got == [a, b]


async def test_order_is_per_member_not_shared(client):
    _token, org, owner_h = await _owner_org(client)
    org_id = org["id"]
    for e164 in ("+12145556001", "+12145556002"):
        await _add_number(client, owner_h, e164)
    a = await _inbox_id_for(client, owner_h, "+12145556001")
    b = await _inbox_id_for(client, owner_h, "+12145556002")

    email = f"member-{uuid.uuid4().hex[:8]}@example.com"
    await _create_member(client, owner_h, email=email, inbox_ids=[a, b])
    member_h = await _login(client, email, org_id)

    r = await client.put("/api/v1/me/inbox-order", json={"inbox_ids": [b, a]}, headers=owner_h)
    assert r.status_code == 204, r.text

    # The owner's drag must not have touched the other member's (still-default) order.
    owner_order = [i["id"] for i in await _inboxes(client, owner_h)]
    member_order = [i["id"] for i in await _inboxes(client, member_h)]
    assert owner_order == [b, a]
    assert member_order != owner_order


# --------------------------------------------------------------------------------------
# Default order (nobody has dragged anything yet)
# --------------------------------------------------------------------------------------
async def test_default_order_is_mine_then_department_then_unassigned(client, session):
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    owner_token = await register_and_login(client, owner_email)
    org = await create_org(client, owner_token, "Acme")
    owner_h = auth_headers(owner_token, org["id"])

    # Alphabetically reversed on purpose so a name-only sort would fail this test.
    for e164, name in [
        ("+12145557001", "Zzz unassigned"),
        ("+12145557002", "Bbb department"),
        ("+12145557003", "Aaa mine"),
    ]:
        await _add_number(client, owner_h, e164)
        inbox_id = await _inbox_id_for(client, owner_h, e164)
        r = await client.patch(
            f"/api/v1/inboxes/{inbox_id}", json={"name": name}, headers=owner_h
        )
        assert r.status_code == 200, r.text

    mine = await _inbox_id_for(client, owner_h, "+12145557003")
    dept_inbox = await _inbox_id_for(client, owner_h, "+12145557002")
    unassigned = await _inbox_id_for(client, owner_h, "+12145557001")

    # The owner is already full-access by role - these two EXPLICIT grants exist only
    # so the default-order bucketing (a plain query over InboxGrant, independent of the
    # admin-bypass in resolve_access) has something to find for "mine"/"department".
    # `unassigned` gets no grant at all; the owner still sees it, via the role bypass.
    owner = await users_repo.get_by_email(session, owner_email)

    await _set_grants(
        client, owner_h, mine,
        [{"grantee_type": "user", "grantee_id": str(owner.id), "role": "member"}],
    )
    r = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=owner_h)
    assert r.status_code == 201, r.text
    department_id = r.json()["id"]
    r = await client.put(
        f"/api/v1/departments/{department_id}/members",
        json={"user_ids": [str(owner.id)]},
        headers=owner_h,
    )
    assert r.status_code == 200, r.text
    await _set_grants(
        client, owner_h, dept_inbox,
        [{"grantee_type": "department", "grantee_id": department_id, "role": "member"}],
    )

    got = [i["id"] for i in await _inboxes(client, owner_h)]
    assert got == [mine, dept_inbox, unassigned]
