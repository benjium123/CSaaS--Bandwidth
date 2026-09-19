"""P15: user-centric inbox assignments (GET/PUT /api/v1/inboxes/assignments).

GET renders the org's whole inbox list as a per-user checklist; PUT replaces that user's
DIRECT grants across the org in one transaction and must never touch department grants.
The refusal/guard cases (RBAC, unknown user, bad role/inbox) live in a sibling module.
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


async def _assignments(client, headers, user_id) -> list[dict]:
    r = await client.get(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(user_id)},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _put_assignments(client, headers, user_id, inboxes) -> list[dict]:
    r = await client.put(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(user_id)},
        json={"inboxes": inboxes},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------------------------
# GET: every inbox in the org is listed, granted or not
# ----------------------------------------------------------------------------------
async def test_get_assignments_lists_every_inbox_two_granted(client, session):
    owner_token = await register_and_login(client, "ia1@example.com")
    org = await create_org(client, owner_token, "Org IA1")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164s = [
        "+12145551001",
        "+12145551002",
        "+12145551003",
        "+12145551004",
        "+12145551005",
        "+12145551006",
    ]
    for e164 in e164s:
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentia1@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentia1@example.com")

    granted = e164s[:2]
    for e164 in granted:
        inbox_id = await _inbox_id_for(client, h, e164)
        await _set_grants(
            client,
            h,
            inbox_id,
            [{"grantee_type": "user", "grantee_id": str(agent.id), "role": "member"}],
        )

    rows = await _assignments(client, h, agent.id)
    assert len(rows) == 6
    assert {r["e164"] for r in rows} == set(e164s)
    assert [r["e164"] for r in rows] == sorted(e164s)
    assert all(r["via_department"] == [] for r in rows)
    assert {r["e164"]: r["direct_role"] for r in rows} == {
        e164: ("member" if e164 in granted else None) for e164 in e164s
    }


# ----------------------------------------------------------------------------------
# PUT with an explicit set: replaces the user's direct grants across the whole org
# ----------------------------------------------------------------------------------
async def test_put_replaces_direct_grants_across_org(client, session):
    owner_token = await register_and_login(client, "ia2@example.com")
    org = await create_org(client, owner_token, "Org IA2")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    a, b, c, d = "+12145551011", "+12145551012", "+12145551013", "+12145551014"
    for e164 in (a, b, c, d):
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentia2@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentia2@example.com")

    inbox = {e164: await _inbox_id_for(client, h, e164) for e164 in (a, b, c, d)}

    # A pre-existing direct grant on A - the PUT below must replace (and drop) it.
    await _set_grants(
        client,
        h,
        inbox[a],
        [{"grantee_type": "user", "grantee_id": str(agent.id), "role": "member"}],
    )

    expected = {a: None, b: "member", c: "viewer", d: "member"}
    body = [
        {"inbox_id": inbox[b], "role": "member"},
        {"inbox_id": inbox[c], "role": "viewer"},
        {"inbox_id": inbox[d], "role": "member"},
    ]
    put = await _put_assignments(client, h, agent.id, body)
    assert {r["e164"]: r["direct_role"] for r in put} == expected

    got = await _assignments(client, h, agent.id)
    assert {r["e164"]: r["direct_role"] for r in got} == expected

    # The DB row, not just the response body.
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.grantee_type == "user",
                InboxGrant.grantee_id == agent.id,
            )
        )
    ).scalars().all()
    assert len(rows) == 3
    assert {r.inbox_id: r.role for r in rows} == {
        uuid.UUID(inbox[b]): "member",
        uuid.UUID(inbox[c]): "viewer",
        uuid.UUID(inbox[d]): "member",
    }
    assert uuid.UUID(inbox[a]) not in {r.inbox_id for r in rows}


# ----------------------------------------------------------------------------------
# PUT with an empty set: drops direct grants but leaves department grants intact
# ----------------------------------------------------------------------------------
async def test_put_with_no_inboxes_preserves_department_grant(client, session):
    owner_token = await register_and_login(client, "ia3@example.com")
    org = await create_org(client, owner_token, "Org IA3")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164 = "+12145551021"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentia3@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentia3@example.com")

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

    # ONE call seeding BOTH a department grant and a direct user grant on A.
    await _set_grants(
        client,
        h,
        inbox_a,
        [
            {"grantee_type": "department", "grantee_id": str(dept_id), "role": "member"},
            {"grantee_type": "user", "grantee_id": str(agent.id), "role": "viewer"},
        ],
    )

    via_sales = [
        {"department_id": str(dept_id), "department_name": "Sales", "role": "member"}
    ]
    before = await _assignments(client, h, agent.id)
    row = next(r for r in before if r["inbox_id"] == inbox_a)
    assert row["direct_role"] == "viewer"
    assert row["via_department"] == via_sales

    cleared = await _put_assignments(client, h, agent.id, [])
    row_after = next(r for r in cleared if r["inbox_id"] == inbox_a)
    assert row_after["direct_role"] is None
    assert row_after["via_department"] == via_sales

    # The department-preservation guarantee: deleting a department grant here would
    # silently strip a whole team's access to a phone number.
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

    direct_rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.grantee_type == "user",
                InboxGrant.grantee_id == agent.id,
            )
        )
    ).scalars().all()
    assert len(direct_rows) == 0


# ----------------------------------------------------------------------------------
# A deactivated department is not advertised as via_department
# ----------------------------------------------------------------------------------
async def test_deactivated_department_not_reported_as_via_department(client, session):
    owner_token = await register_and_login(client, "ia4@example.com")
    org = await create_org(client, owner_token, "Org IA4")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164 = "+12145551031"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text

    await _register_member(client, session, org_id, "agentia4@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentia4@example.com")

    dept = await client.post("/api/v1/departments", json={"name": "Ops"}, headers=h)
    assert dept.status_code == 201, dept.text
    dept_id = dept.json()["id"]

    members = await client.put(
        f"/api/v1/departments/{dept_id}/members",
        json={"user_ids": [str(agent.id)]},
        headers=h,
    )
    assert members.status_code == 200, members.text

    inbox_a = await _inbox_id_for(client, h, e164)
    await _set_grants(
        client,
        h,
        inbox_a,
        [{"grantee_type": "department", "grantee_id": dept_id, "role": "member"}],
    )

    before = await _assignments(client, h, agent.id)
    row = next(r for r in before if r["inbox_id"] == inbox_a)
    assert row["via_department"] == [
        {"department_id": dept_id, "department_name": "Ops", "role": "member"}
    ]

    deactivated = await client.patch(
        f"/api/v1/departments/{dept_id}", json={"is_active": False}, headers=h
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False

    # resolve_access treats a deactivated department's grants as void at send/dial time;
    # this read must agree, or the checklist would advertise access sending then refuses.
    after = await _assignments(client, h, agent.id)
    row_after = next(r for r in after if r["inbox_id"] == inbox_a)
    assert row_after["via_department"] == []
