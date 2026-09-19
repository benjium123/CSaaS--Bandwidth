"""P15: the BULK inbox-assignments read (GET /api/v1/inboxes/assignments, no user_id).

Without a user_id the route returns ONE entry per member of the caller's org, sorted by
str(user_id), each entry's ``assignments`` covering EVERY inbox in the org sorted by e164.
The single-user shape is compared by value so the frontend keeps one parser, and an N+1
gate asserts the query count does not grow with the member count - the reason the bulk
branch exists at all.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import InboxGrant, OrgMembership, Role
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login


async def _register_member(client, session, org_id: uuid.UUID, email, role_name="agent") -> str:
    """Register a user and attach them to the org with an existing system role, directly
    through the ORM.

    NOT the ``/orgs/current/invites`` endpoint: conftest's default ``allow_open_
    registration=True`` makes every registration take the bootstrap branch (see
    app/api/routes/auth.py::register), which never redeems an invite_token at all - see
    tests/test_invites.py's ``settings`` fixture docstring for the same trap. Mirrors the
    direct-membership pattern in tests/test_rbac.py::test_agent_denied_members_read.
    """
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = (await session.execute(sa.select(Role).where(Role.name == role_name))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def _inbox_id_for(client, headers, e164) -> str:
    r = await client.get("/api/v1/inboxes", headers=headers)
    assert r.status_code == 200, r.text
    row = next(i for i in r.json() if i["e164"] == e164)
    return row["id"]


async def _set_grants(client, headers, inbox_id, grants) -> None:
    r = await client.put(
        f"/api/v1/inboxes/{inbox_id}/grants", json={"grants": grants}, headers=headers
    )
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------------------
# Bulk: every member of the org appears, with their own direct grants
# ----------------------------------------------------------------------------------
async def test_bulk_lists_every_member_with_their_grants(client, session):
    owner_token = await register_and_login(client, "ib1@example.com")
    org = await create_org(client, owner_token, "Org IB1")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164s = [
        "+12145553001",
        "+12145553002",
        "+12145553003",
        "+12145553004",
        "+12145553005",
        "+12145553006",
    ]
    for e164 in e164s:
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentib1a@example.com")
    await _register_member(client, session, org_id, "agentib1b@example.com")
    set_org_context(session, org_id)
    owner = await users_repo.get_by_email(session, "ib1@example.com")
    agent1 = await users_repo.get_by_email(session, "agentib1a@example.com")
    agent2 = await users_repo.get_by_email(session, "agentib1b@example.com")

    granted = e164s[:2]
    for e164 in granted:
        inbox_id = await _inbox_id_for(client, h, e164)
        await _set_grants(
            client,
            h,
            inbox_id,
            [{"grantee_type": "user", "grantee_id": str(agent1.id), "role": "member"}],
        )

    resp = await client.get("/api/v1/inboxes/assignments", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert {e["user_id"] for e in body} == {str(owner.id), str(agent1.id), str(agent2.id)}
    assert [e["user_id"] for e in body] == sorted(e["user_id"] for e in body)

    by_user = {e["user_id"]: e["assignments"] for e in body}

    a1 = by_user[str(agent1.id)]
    assert len(a1) == 6
    assert [r["e164"] for r in a1] == sorted(e164s)
    assert {r["e164"]: r["direct_role"] for r in a1} == {
        e164: ("member" if e164 in granted else None) for e164 in e164s
    }

    # A member with NO grants must still appear, with every inbox direct_role=None,
    # rather than being omitted from the list.
    a2 = by_user[str(agent2.id)]
    assert len(a2) == 6
    assert [r["e164"] for r in a2] == sorted(e164s)
    assert all(r["direct_role"] is None for r in a2)
    assert all(r["via_department"] == [] for r in a2)


# ----------------------------------------------------------------------------------
# The bulk element shape IS the single-user shape - one parser for both
# ----------------------------------------------------------------------------------
async def test_bulk_element_shape_matches_single_user_get(client, session):
    owner_token = await register_and_login(client, "ib2@example.com")
    org = await create_org(client, owner_token, "Org IB2")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164s = ["+12145553101", "+12145553102"]
    for e164 in e164s:
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentib2@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentib2@example.com")

    inbox_id = await _inbox_id_for(client, h, e164s[0])
    await _set_grants(
        client,
        h,
        inbox_id,
        [{"grantee_type": "user", "grantee_id": str(agent.id), "role": "member"}],
    )

    bulk = await client.get("/api/v1/inboxes/assignments", headers=h)
    assert bulk.status_code == 200, bulk.text
    entry = next(e for e in bulk.json() if e["user_id"] == str(agent.id))

    single = await client.get(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(agent.id)},
        headers=h,
    )
    assert single.status_code == 200, single.text

    # Equal as PARSED JSON, not merely same-length: the frontend relies on this contract.
    assert entry["assignments"] == single.json()


# ----------------------------------------------------------------------------------
# Cross-tenant gate: another org's members never appear in this org's list
# ----------------------------------------------------------------------------------
async def test_bulk_excludes_members_of_another_org(client, session):
    owner1_token = await register_and_login(client, "ib3a@example.com")
    org1 = await create_org(client, owner1_token, "Org IB3A")
    org1_id = uuid.UUID(org1["id"])
    h1 = auth_headers(owner1_token, org1["id"])

    owner2_token = await register_and_login(client, "ib3b@example.com")
    org2 = await create_org(client, owner2_token, "Org IB3B")
    org2_id = uuid.UUID(org2["id"])
    h2 = auth_headers(owner2_token, org2["id"])

    for headers, e164 in ((h1, "+12145553201"), (h2, "+12145553202")):
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=headers)
        assert created.status_code == 201, created.text

    await _register_member(client, session, org1_id, "agentib3a@example.com")
    await _register_member(client, session, org2_id, "agentib3b@example.com")

    set_org_context(session, org1_id)
    owner1 = await users_repo.get_by_email(session, "ib3a@example.com")
    agent1 = await users_repo.get_by_email(session, "agentib3a@example.com")
    set_org_context(session, org2_id)
    owner2 = await users_repo.get_by_email(session, "ib3b@example.com")
    agent2 = await users_repo.get_by_email(session, "agentib3b@example.com")

    # The cross-tenant gate: OrgMembership is TenantScoped, so org 2's members must never
    # surface in org 1's bulk list.
    resp = await client.get("/api/v1/inboxes/assignments", headers=h1)
    assert resp.status_code == 200, resp.text
    seen = {e["user_id"] for e in resp.json()}

    assert str(owner2.id) not in seen
    assert str(agent2.id) not in seen
    # Org 1's OWN members ARE present, so this cannot pass merely by returning nothing.
    assert str(owner1.id) in seen
    assert str(agent1.id) in seen


# ----------------------------------------------------------------------------------
# Department-derived access is reported (read-only) and writes nothing
# ----------------------------------------------------------------------------------
async def test_bulk_reports_department_access_read_only(client, session):
    owner_token = await register_and_login(client, "ib4@example.com")
    org = await create_org(client, owner_token, "Org IB4")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164 = "+12145553301"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentib4@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentib4@example.com")

    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h)
    assert dept.status_code == 201, dept.text
    dept_id = uuid.UUID(dept.json()["id"])

    members = await client.put(
        f"/api/v1/departments/{dept_id}/members",
        json={"user_ids": [str(agent.id)]},
        headers=h,
    )
    assert members.status_code == 200, members.text

    inbox_a = await _inbox_id_for(client, h, e164)
    # A DEPARTMENT grant only - the agent holds no direct user grant on A.
    await _set_grants(
        client,
        h,
        inbox_a,
        [{"grantee_type": "department", "grantee_id": str(dept_id), "role": "member"}],
    )

    resp = await client.get("/api/v1/inboxes/assignments", headers=h)
    assert resp.status_code == 200, resp.text
    entry = next(e for e in resp.json() if e["user_id"] == str(agent.id))
    row = next(r for r in entry["assignments"] if r["inbox_id"] == inbox_a)
    assert row["direct_role"] is None
    assert row["via_department"] == [
        {"department_id": str(dept_id), "department_name": "Sales", "role": "member"}
    ]

    # This read endpoint must have written nothing: the department grant row is intact.
    set_org_context(session, org_id)
    dept_grant = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.grantee_type == "department",
                InboxGrant.grantee_id == dept_id,
            )
        )
    ).scalar_one()
    assert dept_grant.role == "member"
    assert dept_grant.inbox_id == uuid.UUID(inbox_a)


# ----------------------------------------------------------------------------------
# RBAC: the bulk read is inboxes:admin only
# ----------------------------------------------------------------------------------
async def test_bulk_refused_without_inboxes_admin(client, session):
    owner_token = await register_and_login(client, "ib5@example.com")
    org = await create_org(client, owner_token, "Org IB5")
    org_id = uuid.UUID(org["id"])

    agent_token = await _register_member(client, session, org_id, "agentib5@example.com")
    ah = auth_headers(agent_token, org["id"])

    resp = await client.get("/api/v1/inboxes/assignments", headers=ah)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "permission_denied"


# ----------------------------------------------------------------------------------
# N+1 gate: the query count is independent of the number of members
# ----------------------------------------------------------------------------------
async def test_bulk_query_count_does_not_grow_with_member_count(client, session, query_counter):
    owner_token = await register_and_login(client, "ib6@example.com")
    org = await create_org(client, owner_token, "Org IB6")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    for e164 in ("+12145553401", "+12145553402"):
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentib6a@example.com")
    await _register_member(client, session, org_id, "agentib6b@example.com")

    query_counter.reset()
    small_resp = await client.get("/api/v1/inboxes/assignments", headers=h)
    assert small_resp.status_code == 200, small_resp.text
    small = query_counter.count
    small_entries = len(small_resp.json())

    for email in (
        "agentib6c@example.com",
        "agentib6d@example.com",
        "agentib6e@example.com",
        "agentib6f@example.com",
    ):
        await _register_member(client, session, org_id, email)

    query_counter.reset()
    big_resp = await client.get("/api/v1/inboxes/assignments", headers=h)
    assert big_resp.status_code == 200, big_resp.text
    big = query_counter.count
    big_entries = len(big_resp.json())

    # The response really did grow with the member count...
    assert big_entries > small_entries
    # ...but the number of queries must not - that is the whole point of the bulk branch.
    assert small == big, f"query count grew with member count ({small} -> {big}): N+1"
