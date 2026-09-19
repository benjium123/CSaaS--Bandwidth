"""Admin-creates-a-member: the second way into a workspace, beside the emailed invite.

The invite flow is untouched and still covered by tests/test_invites.py; these tests cover
``POST /api/v1/orgs/current/members`` - the path an admin uses when SMTP is not configured
on the production box and an emailed accept link can never be delivered.

A 201 only proves a row exists; the login test proves the credential actually works.
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import AccountAuditEntry, AuditLogEntry, OrgMembership, Role
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login

PASSWORD = "correct-horse-battery"
ENDPOINT = "/api/v1/orgs/current/members"


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
):
    return await client.post(
        ENDPOINT,
        json={
            "email": email,
            "full_name": full_name,
            "password": password,
            "role_name": role_name,
        },
        headers=headers,
    )


async def test_admin_created_agent_can_actually_log_in(client):
    _token, _org, h = await _owner_org(client)

    r = await _create_member(client, h, email="newagent@example.com", full_name="New Agent")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "newagent@example.com"
    assert body["full_name"] == "New Agent"
    assert body["role_name"] == "agent"
    assert "password" not in body
    assert "hashed_password" not in body

    # A 201 proves a row; the login proves the supplied credential works.
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "newagent@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]


async def test_owner_role_is_refused(client, session):
    _token, _org, h = await _owner_org(client)

    r = await _create_member(client, h, email="wannabe-owner@example.com", role_name="owner")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"

    # A refusal that still created the account would be worse than useless.
    assert await users_repo.get_by_email(session, "wannabe-owner@example.com") is None


async def test_weak_password_is_refused_by_policy(client, session):
    _token, _org, h = await _owner_org(client)

    r = await _create_member(client, h, email="weakpw@example.com", password="short")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "weak_password"

    assert await users_repo.get_by_email(session, "weakpw@example.com") is None


async def test_duplicate_email_returns_409(client):
    _token, _org, h = await _owner_org(client)

    first = await _create_member(client, h, email="dupe@example.com")
    assert first.status_code == 201, first.text

    second = await _create_member(client, h, email="dupe@example.com")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


async def test_caller_without_members_invite_is_refused(client, session):
    _owner_token, org, _h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    agent_email = f"agent-{uuid.uuid4().hex[:8]}@example.com"
    agent_token = await register_and_login(client, agent_email)
    agent_user = await users_repo.get_by_email(session, agent_email)

    # Same shape as tests/test_rbac.py::test_agent_denied_members_read: make the second
    # user an AGENT member of the owner's org by inserting the row directly.
    set_org_context(session, org_id)
    agent_role = (
        await session.execute(sa.select(Role).where(Role.name == "agent"))
    ).scalar_one()
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=agent_user.id, role_id=agent_role.id
        )
    )
    await session.commit()

    r = await _create_member(
        client,
        auth_headers(agent_token, org["id"]),
        email="another@example.com",
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"


async def test_created_user_is_member_of_callers_org_only(client, session):
    _owner_a_token, org_a, h_a = await _owner_org(client)
    _owner_b_token, org_b, h_b = await _owner_org(client)

    r = await _create_member(client, h_a, email="scoped@example.com")
    assert r.status_code == 201, r.text
    new_user_id = uuid.UUID(r.json()["user_id"])

    # JUSTIFIED allow_unscoped: we are deliberately asking about EVERY membership this
    # brand-new user has, across all workspaces.
    rows = (
        await session.execute(
            sa.select(OrgMembership)
            .where(OrgMembership.user_id == new_user_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].org_id == uuid.UUID(org_a["id"])
    assert rows[0].org_id != uuid.UUID(org_b["id"])

    listing = await client.get("/api/v1/orgs/current/members", headers=h_b)
    assert listing.status_code == 200
    assert "scoped@example.com" not in [m["email"] for m in listing.json()]


async def test_audit_rows_never_contain_the_password(client, session):
    _token, org, h = await _owner_org(client)
    org_id = uuid.UUID(org["id"])

    r = await _create_member(client, h, email="audited@example.com")
    assert r.status_code == 201, r.text
    new_user_id = uuid.UUID(r.json()["user_id"])

    # AuditLogEntry is tenant-scoped; AccountAuditEntry belongs to the person, not an org.
    set_org_context(session, org_id)
    org_rows = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.org_id == org_id)
        )
    ).scalars().all()
    account_rows = (
        await session.execute(
            sa.select(AccountAuditEntry).where(AccountAuditEntry.user_id == new_user_id)
        )
    ).scalars().all()

    assert any(e.action == "member.created" for e in org_rows)
    assert any(e.action == "account.created_by_admin" for e in account_rows)

    for entry in list(org_rows) + list(account_rows):
        detail = entry.detail or {}
        assert PASSWORD not in json.dumps(detail)
        assert "password" not in detail
        assert "hashed_password" not in detail
