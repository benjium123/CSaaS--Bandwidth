"""Admin-creates-a-member WITH inbox grants: one transaction, or nothing at all.

The optional ``inbox_ids`` field on ``POST /api/v1/orgs/current/members`` folds the
``InboxGrant`` rows into the SAME transaction that creates the account, so a teammate is
created with their number(s) or not at all. These tests pin the grant rows, the refusal
paths (foreign inbox, owner role, missing permission) and the atomicity claim
(a refused request leaves no account behind).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import AuditLogEntry, InboxGrant, OrgMembership, Role
from app.models.rbac import SYSTEM_ROLES
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login

pytestmark = pytest.mark.usefixtures("paid_seats")  # adds members; not about seats

PASSWORD = "correct-horse-battery"
ENDPOINT = "/api/v1/orgs/current/members"


# ----------------------------------------------------------------------------------
# Helpers (mirroring tests/test_admin_create_member.py and test_p15_inbox_assignments.py)
# ----------------------------------------------------------------------------------
async def _owner_org(client):
    token = await register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@example.com")
    org = await create_org(client, token, "Acme")
    return token, org, auth_headers(token, org["id"])


async def _create_member(
    client,
    headers,
    *,
    email: str,
    password: str = PASSWORD,
    role_name: str = "agent",
    full_name: str = "New Agent",
    inbox_ids=None,
):
    body = {
        "email": email,
        "full_name": full_name,
        "password": password,
        "role_name": role_name,
    }
    # ``inbox_ids=None`` means the key is ABSENT from the JSON body entirely, which is the
    # regression guard in test_create_member_without_inbox_ids_writes_no_grants.
    if inbox_ids is not None:
        body["inbox_ids"] = inbox_ids
    return await client.post(ENDPOINT, json=body, headers=headers)


async def _inbox_id_for(client, headers, e164) -> str:
    r = await client.get("/api/v1/inboxes", headers=headers)
    assert r.status_code == 200, r.text
    return next(i for i in r.json() if i["e164"] == e164)["id"]


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


async def _user_grants(session, org_id: uuid.UUID, user_id: uuid.UUID) -> list[InboxGrant]:
    set_org_context(session, org_id)
    return list(
        (
            await session.execute(
                sa.select(InboxGrant).where(
                    InboxGrant.grantee_type == "user",
                    InboxGrant.grantee_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )


# ----------------------------------------------------------------------------------
# 1. Two inboxes granted in one call
# ----------------------------------------------------------------------------------
async def test_create_member_with_two_inboxes_grants_both(client, session):
    _token, org, h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    a, b = "+12145552001", "+12145552002"
    for e164 in (a, b):
        created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert created.status_code == 201, created.text
    inbox_a = await _inbox_id_for(client, h, a)
    inbox_b = await _inbox_id_for(client, h, b)

    email = f"two-inboxes-{uuid.uuid4().hex[:8]}@example.com"
    r = await _create_member(client, h, email=email, inbox_ids=[inbox_a, inbox_b])
    assert r.status_code == 201, r.text
    new_user_id = uuid.UUID(r.json()["user_id"])

    rows = await _user_grants(session, org_id, new_user_id)
    assert len(rows) == 2
    assert all(g.grantee_type == "user" for g in rows)
    assert all(g.role == "member" for g in rows)
    assert {str(g.inbox_id) for g in rows} == {inbox_a, inbox_b}

    assignments = await _assignments(client, h, new_user_id)
    assert len(assignments) == 2
    assert {row["inbox_id"]: row["direct_role"] for row in assignments} == {
        inbox_a: "member",
        inbox_b: "member",
    }


# ----------------------------------------------------------------------------------
# 2. Regression guard: no inbox_ids at all => no grants, identical body/audit shape
# ----------------------------------------------------------------------------------
async def test_create_member_without_inbox_ids_writes_no_grants(client, session):
    _token, org, h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])
    email = f"no-inboxes-{uuid.uuid4().hex[:8]}@example.com"

    r = await _create_member(client, h, email=email)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == email
    assert body["full_name"] == "New Agent"
    assert body["role_name"] == "agent"
    assert "password" not in body
    assert "hashed_password" not in body
    new_user_id = uuid.UUID(body["user_id"])

    assert await _user_grants(session, org_id, new_user_id) == []

    set_org_context(session, org_id)
    org_rows = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.org_id == org_id)
        )
    ).scalars().all()
    member_created = [e for e in org_rows if e.action == "member.created"]
    assert len(member_created) == 1
    assert "inbox_ids" not in (member_created[0].detail or {})


# ----------------------------------------------------------------------------------
# 3. A foreign org's inbox is refused and NOTHING is written  <-- the atomicity test
# ----------------------------------------------------------------------------------
async def test_foreign_org_inbox_is_refused_and_no_user_row_is_written(client, session):
    _token_a, org_a, h_a = await _owner_org(client)
    _token_b, org_b, h_b = await _owner_org(client)
    org_b_id = uuid.UUID(org_b["id"])

    e164_b = "+12145553001"
    created = await client.post("/api/v1/numbers", json={"e164": e164_b}, headers=h_b)
    assert created.status_code == 201, created.text
    inbox_b = await _inbox_id_for(client, h_b, e164_b)

    email = f"foreign-{uuid.uuid4().hex[:8]}@example.com"
    r = await _create_member(client, h_a, email=email, inbox_ids=[inbox_b])

    # THE ATOMICITY CLAIM: a 404 from the shared cross-org inbox check means the whole
    # request was rejected before create_user ran, so no account and no grant exist.
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"

    assert await users_repo.get_by_email(session, email) is None

    set_org_context(session, org_b_id)
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.inbox_id == uuid.UUID(inbox_b),
                InboxGrant.grantee_type == "user",
            )
        )
    ).scalars().all()
    assert rows == []


# ----------------------------------------------------------------------------------
# 4. A repeated id is not an error and creates exactly one row
# ----------------------------------------------------------------------------------
async def test_duplicate_inbox_id_in_list_does_not_500(client, session):
    _token, org, h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    e164 = "+12145554001"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, h, e164)

    email = f"dupe-inbox-{uuid.uuid4().hex[:8]}@example.com"
    r = await _create_member(client, h, email=email, inbox_ids=[inbox_id, inbox_id])
    assert r.status_code == 201, r.text
    new_user_id = uuid.UUID(r.json()["user_id"])

    rows = await _user_grants(session, org_id, new_user_id)
    assert len(rows) == 1
    assert str(rows[0].inbox_id) == inbox_id


# ----------------------------------------------------------------------------------
# 5. "owner" is still refused, and the refusal writes nothing
# ----------------------------------------------------------------------------------
async def test_owner_role_still_refused_with_inbox_ids(client, session):
    _token, org, h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    e164 = "+12145555001"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, h, e164)

    email = f"owner-with-inbox-{uuid.uuid4().hex[:8]}@example.com"
    r = await _create_member(
        client, h, email=email, role_name="owner", inbox_ids=[inbox_id]
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"

    assert await users_repo.get_by_email(session, email) is None

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.inbox_id == uuid.UUID(inbox_id),
                InboxGrant.grantee_type == "user",
            )
        )
    ).scalars().all()
    assert rows == []


# ----------------------------------------------------------------------------------
# 6. A department grant on the same inbox survives
# ----------------------------------------------------------------------------------
async def test_department_grant_on_the_inbox_survives(client, session):
    _token, org, h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    e164 = "+12145556001"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, h, e164)

    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h)
    assert dept.status_code == 201, dept.text
    dept_id = uuid.UUID(dept.json()["id"])

    await _set_grants(
        client,
        h,
        inbox_id,
        [{"grantee_type": "department", "grantee_id": str(dept_id), "role": "member"}],
    )

    email = f"dept-survives-{uuid.uuid4().hex[:8]}@example.com"
    r = await _create_member(client, h, email=email, inbox_ids=[inbox_id])
    assert r.status_code == 201, r.text
    new_user_id = uuid.UUID(r.json()["user_id"])

    set_org_context(session, org_id)
    dept_rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.inbox_id == uuid.UUID(inbox_id),
                InboxGrant.grantee_type == "department",
                InboxGrant.grantee_id == dept_id,
            )
        )
    ).scalars().all()
    assert len(dept_rows) == 1
    assert dept_rows[0].role == "member"

    user_rows = (
        await session.execute(
            sa.select(InboxGrant).where(
                InboxGrant.inbox_id == uuid.UUID(inbox_id),
                InboxGrant.grantee_type == "user",
                InboxGrant.grantee_id == new_user_id,
            )
        )
    ).scalars().all()
    assert len(user_rows) == 1
    assert user_rows[0].role == "member"


# ----------------------------------------------------------------------------------
# 7. members:invite without inboxes:admin may create a teammate, never grant a number
# ----------------------------------------------------------------------------------
async def test_caller_with_members_invite_but_not_inboxes_admin_is_refused(client, session):
    owner_token, org, owner_h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    e164 = "+12145557001"
    created = await client.post("/api/v1/numbers", json={"e164": e164}, headers=owner_h)
    assert created.status_code == 201, created.text
    inbox_id = await _inbox_id_for(client, owner_h, e164)

    inviter_email = f"inviter-{uuid.uuid4().hex[:8]}@example.com"
    inviter_token = await register_and_login(client, inviter_email)
    inviter_user = await users_repo.get_by_email(session, inviter_email)

    # A NON-system role holding members:invite and everything an agent has, but NOT
    # inboxes:admin. Mirrors the direct-ORM pattern in tests/test_rbac.py. It must carry
    # agent's permissions too, or _role_assignable_by would refuse creating an agent.
    set_org_context(session, org_id)
    inviter_role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"inviter-{uuid.uuid4().hex[:8]}",
        permissions=sorted(set(SYSTEM_ROLES["agent"]) | {"members:invite"}),
        is_system=False,
    )
    session.add(inviter_role)
    # The role row must exist before the membership that references it: there is no ORM
    # relationship between OrgMembership.role_id and Role, so SQLAlchemy has no dependency
    # information and would otherwise be free to INSERT the membership first (FK failure).
    await session.flush()
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=inviter_user.id, role_id=inviter_role.id
        )
    )
    await session.commit()

    h = auth_headers(inviter_token, org["id"])

    # Non-empty inbox_ids: the inboxes:admin gate refuses, and nothing is written.
    gated_email = f"gated-{uuid.uuid4().hex[:8]}@example.com"
    gated = await _create_member(client, h, email=gated_email, inbox_ids=[inbox_id])
    assert gated.status_code == 403
    assert gated.json()["error"]["code"] == "permission_denied"
    assert await users_repo.get_by_email(session, gated_email) is None

    # No inbox_ids at all: the gate is scoped to the NEW field; the route still works.
    allowed_email = f"ungated-{uuid.uuid4().hex[:8]}@example.com"
    allowed = await _create_member(client, h, email=allowed_email)
    assert allowed.status_code == 201, allowed.text
