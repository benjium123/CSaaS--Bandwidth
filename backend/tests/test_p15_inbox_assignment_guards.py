"""P15: guard/refusal cases for the user-centric inbox assignment endpoints.

GET/PUT /api/v1/inboxes/assignments must refuse RBAC failures, foreign users and
foreign inbox ids without writing anything, de-duplicate a repeated inbox while
keeping the more permissive role, and reject an unknown role. The happy-path coverage
lives in the sibling module test_p15_inbox_assignments.py.
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


# ----------------------------------------------------------------------------------
# 1. An agent (no inboxes:admin) is refused on both verbs
# ----------------------------------------------------------------------------------
async def test_non_admin_caller_refused(client, session):
    owner_token = await register_and_login(client, "ig1@example.com")
    org = await create_org(client, owner_token, "Org IG1")
    org_id = uuid.UUID(org["id"])

    agent_token = await _register_member(client, session, org_id, "agentig1@example.com")
    h_agent = auth_headers(agent_token, org["id"])

    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentig1@example.com")

    denied_get = await client.get(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(agent.id)},
        headers=h_agent,
    )
    assert denied_get.status_code == 403
    assert denied_get.json()["error"]["code"] == "permission_denied"

    denied_put = await client.put(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(agent.id)},
        json={"inboxes": []},
        headers=h_agent,
    )
    assert denied_put.status_code == 403
    assert denied_put.json()["error"]["code"] == "permission_denied"


# ----------------------------------------------------------------------------------
# 2. A user_id from another org is a 404 - membership is never confirmed cross-org
# ----------------------------------------------------------------------------------
async def test_user_id_from_other_org_is_404(client, session):
    owner1_token = await register_and_login(client, "ig2a@example.com")
    org1 = await create_org(client, owner1_token, "Org IG2A")
    org1_id = uuid.UUID(org1["id"])
    h1 = auth_headers(owner1_token, org1["id"])

    owner2_token = await register_and_login(client, "ig2b@example.com")
    org2 = await create_org(client, owner2_token, "Org IG2B")
    org2_id = uuid.UUID(org2["id"])

    e164 = "+12145552001"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h1)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, h1, e164)

    await _register_member(client, session, org2_id, "agentig2@example.com")
    set_org_context(session, org2_id)
    foreign = await users_repo.get_by_email(session, "agentig2@example.com")

    denied_get = await client.get(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(foreign.id)},
        headers=h1,
    )
    assert denied_get.status_code == 404
    assert denied_get.json()["error"]["code"] == "not_found"

    denied_put = await client.put(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(foreign.id)},
        json={"inboxes": [{"inbox_id": inbox_id, "role": "member"}]},
        headers=h1,
    )
    assert denied_put.status_code == 404
    assert denied_put.json()["error"]["code"] == "not_found"

    set_org_context(session, org1_id)
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(InboxGrant.grantee_id == foreign.id)
        )
    ).scalars().all()
    assert rows == []


# ----------------------------------------------------------------------------------
# 3. A body naming a foreign inbox is rejected all-or-nothing: nothing is written
# ----------------------------------------------------------------------------------
async def test_inbox_id_from_other_org_rejected_and_nothing_written(client, session):
    owner1_token = await register_and_login(client, "ig3a@example.com")
    org1 = await create_org(client, owner1_token, "Org IG3A")
    org1_id = uuid.UUID(org1["id"])
    h1 = auth_headers(owner1_token, org1["id"])

    owner2_token = await register_and_login(client, "ig3b@example.com")
    org2 = await create_org(client, owner2_token, "Org IG3B")
    h2 = auth_headers(owner2_token, org2["id"])

    e164_1 = "+12145552011"
    e164_2 = "+12145552012"
    created1 = await client.post("/api/v1/numbers", json={"e164": e164_1}, headers=h1)
    assert created1.status_code == 201, created1.text
    created2 = await client.post("/api/v1/numbers", json={"e164": e164_2}, headers=h2)
    assert created2.status_code == 201, created2.text

    own_inbox = await _inbox_id_for(client, h1, e164_1)
    foreign_inbox = await _inbox_id_for(client, h2, e164_2)

    await _register_member(client, session, org1_id, "agentig3@example.com")
    set_org_context(session, org1_id)
    agent = await users_repo.get_by_email(session, "agentig3@example.com")

    denied = await client.put(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(agent.id)},
        json={
            "inboxes": [
                {"inbox_id": own_inbox, "role": "member"},
                {"inbox_id": foreign_inbox, "role": "member"},
            ]
        },
        headers=h1,
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"

    # All-or-nothing: the valid half of the body must NOT have been applied.
    set_org_context(session, org1_id)
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.grantee_type == "user",
                InboxGrant.grantee_id == agent.id,
            )
        )
    ).scalars().all()
    assert rows == []


# ----------------------------------------------------------------------------------
# 4. The same inbox named twice collapses to one row, "member" winning
# ----------------------------------------------------------------------------------
async def test_put_dedupes_same_inbox_keeping_member(client, session):
    owner_token = await register_and_login(client, "ig4@example.com")
    org = await create_org(client, owner_token, "Org IG4")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164 = "+12145552021"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, h, e164)

    await _register_member(client, session, org_id, "agentig4@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentig4@example.com")

    resp = await client.put(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(agent.id)},
        json={
            "inboxes": [
                {"inbox_id": inbox_id, "role": "viewer"},
                {"inbox_id": inbox_id, "role": "member"},
            ]
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["inbox_id"] == inbox_id)
    # "member" always wins: grants only ever ADD capability.
    assert row["direct_role"] == "member"

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.inbox_id == uuid.UUID(inbox_id),
                InboxGrant.grantee_id == agent.id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].role == "member"


# ----------------------------------------------------------------------------------
# 5. An unknown role is a 422 validation failure and writes nothing
# ----------------------------------------------------------------------------------
async def test_put_rejects_bad_role_and_writes_nothing(client, session):
    owner_token = await register_and_login(client, "ig5@example.com")
    org = await create_org(client, owner_token, "Org IG5")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    e164 = "+12145552031"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, h, e164)

    await _register_member(client, session, org_id, "agentig5@example.com")
    set_org_context(session, org_id)
    agent = await users_repo.get_by_email(session, "agentig5@example.com")

    resp = await client.put(
        "/api/v1/inboxes/assignments",
        params={"user_id": str(agent.id)},
        json={"inboxes": [{"inbox_id": inbox_id, "role": "owner"}]},
        headers=h,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.grantee_type == "user",
                InboxGrant.grantee_id == agent.id,
            )
        )
    ).scalars().all()
    assert rows == []
