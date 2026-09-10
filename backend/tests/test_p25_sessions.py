from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import sqlalchemy as sa

from app.auth.security import create_access_token
from app.db.base import set_org_context
from app.models import AuditLogEntry, LoginEvent, OrgMembership, Role, User
from app.models import Session as IdentitySession
from app.services import session_cache
from tests.conftest import TEST_JWT_SECRET, auth_headers, create_org, register_and_login


@pytest.fixture(autouse=True)
def _clear_session_cache():
    from app.services import session_cache

    session_cache.reset_memory_cache()
    yield
    session_cache.reset_memory_cache()


def _error_code(resp):
    body = resp.json()
    if isinstance(body, dict):
        # The app wraps every CsaasError as {"error": {"code", "message", "request_id"}}.
        error = body.get("error")
        if isinstance(error, dict) and "code" in error:
            return error["code"]
        if "code" in body:
            return body["code"]
    return None


async def _user_id(session, email):
    user = (await session.execute(sa.select(User).where(User.email == email))).scalars().first()
    return user.id


async def _add_membership(session, *, user_id, org_id, role_name="agent"):
    """Directly create an OrgMembership with a tenant-local role."""
    set_org_context(session, org_id)
    role = (
        await session.execute(
            sa.select(Role).where(Role.org_id == org_id, Role.name == role_name)
        )
    ).scalars().first()
    if role is None:
        role = Role(id=uuid.uuid4(), org_id=org_id, name=role_name, permissions=[])
        session.add(role)
        await session.flush()
    membership = OrgMembership(
        id=uuid.uuid4(), org_id=org_id, user_id=user_id, role_id=role.id
    )
    session.add(membership)
    await session.commit()


async def _make_org_with_member(client, session, owner_email, member_email):
    owner_token = await register_and_login(client, owner_email)
    org = await create_org(client, owner_token, "Identity Org")
    org_id = uuid.UUID(org["id"])
    member_token = await register_and_login(client, member_email)
    owner_id = await _user_id(session, owner_email)
    member_id = await _user_id(session, member_email)
    await _add_membership(session, user_id=member_id, org_id=org_id, role_name="agent")
    return owner_token, member_token, owner_id, member_id, org_id


async def test_login_creates_session_row(client, session):
    email = "sessions-login@example.com"
    password = "correct-horse-battery"
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": email.split("@")[0],
        },
    )
    assert r.status_code == 201, r.text

    headers = {
        "X-Forwarded-For": "203.0.113.9, 10.0.0.1",
        "User-Agent": "z" * 300,
    }
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    payload = jwt.decode(
        r.json()["access_token"], TEST_JWT_SECRET, algorithms=["HS256"]
    )
    user_id = uuid.UUID(payload["sub"])
    rows = (
        await session.execute(
            sa.select(IdentitySession).where(IdentitySession.user_id == user_id)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.ip == "203.0.113.9"
    assert row.user_agent == "z" * 255


async def test_token_carries_sid(client, session):
    token = await register_and_login(client, "token-sid@example.com")
    payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"])
    sid = uuid.UUID(payload["sid"])
    row = (
        await session.execute(
            sa.select(IdentitySession).where(IdentitySession.id == sid)
        )
    ).scalars().first()
    assert row is not None


async def test_old_token_without_sid_still_works(client, session):
    email = "pre-token@example.com"
    await register_and_login(client, email)
    user = (await session.execute(sa.select(User).where(User.email == email))).scalars().first()
    tok = create_access_token(user.id, TEST_JWT_SECRET)
    r = await client.get("/api/v1/auth/me", headers=auth_headers(tok))
    assert r.status_code == 200


async def test_forged_sid_rejected(client, session):
    email = "forged-sid@example.com"
    await register_and_login(client, email)
    user = (await session.execute(sa.select(User).where(User.email == email))).scalars().first()
    tok = create_access_token(user.id, TEST_JWT_SECRET, sid=uuid.uuid4())
    r = await client.get("/api/v1/auth/me", headers=auth_headers(tok))
    assert r.status_code == 401


async def test_revoked_session_rejected(client):
    token = await register_and_login(client, "revoked-session@example.com")
    payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"])
    sid = uuid.UUID(payload["sid"])

    r = await client.get("/api/v1/me/sessions", headers=auth_headers(token))
    assert r.status_code == 200
    assert any(uuid.UUID(item["id"]) == sid for item in r.json())

    r = await client.delete(f"/api/v1/me/sessions/{sid}", headers=auth_headers(token))
    assert r.status_code == 204

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 401
    assert _error_code(r) == "unauthenticated"


async def test_expired_session_rejected(client, session):
    token = await register_and_login(client, "expired-session@example.com")
    payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"])
    sid = uuid.UUID(payload["sid"])

    row = (
        await session.execute(
            sa.select(IdentitySession).where(IdentitySession.id == sid)
        )
    ).scalars().first()
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 401


async def test_list_own_sessions_only(client):
    token_a = await register_and_login(client, "list-a@example.com")
    token_b = await register_and_login(client, "list-b@example.com")
    payload_b = jwt.decode(token_b, TEST_JWT_SECRET, algorithms=["HS256"])
    sid_b = uuid.UUID(payload_b["sid"])

    r = await client.get("/api/v1/me/sessions", headers=auth_headers(token_a))
    assert r.status_code == 200
    assert sid_b not in [uuid.UUID(item["id"]) for item in r.json()]


async def test_delete_other_users_session_is_404(client):
    token_a = await register_and_login(client, "del-a@example.com")
    token_b = await register_and_login(client, "del-b@example.com")
    payload_b = jwt.decode(token_b, TEST_JWT_SECRET, algorithms=["HS256"])
    sid_b = uuid.UUID(payload_b["sid"])

    r = await client.delete(
        f"/api/v1/me/sessions/{sid_b}", headers=auth_headers(token_a)
    )
    assert r.status_code == 404

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token_b))
    assert r.status_code == 200


async def test_sign_out_everywhere_keeps_current(client):
    email = "everywhere@example.com"
    password = "correct-horse-battery"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Everywhere"},
    )
    assert r.status_code == 201, r.text

    async def login_once():
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    token1 = await login_once()
    token2 = await login_once()
    token3 = await login_once()

    r = await client.post(
        "/api/v1/me/sessions/revoke-all", headers=auth_headers(token3)
    )
    assert r.status_code == 200
    assert r.json()["revoked"] == 2

    assert (await client.get("/api/v1/auth/me", headers=auth_headers(token1))).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=auth_headers(token2))).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=auth_headers(token3))).status_code == 200


async def test_admin_revokes_member_sessions(client, session):
    owner_token, member_token, _owner_id, member_id, org_id = await _make_org_with_member(
        client,
        session,
        "owner-revoke@example.com",
        "member-revoke@example.com",
    )

    r = await client.delete(
        f"/api/v1/orgs/current/members/{member_id}/sessions",
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200
    assert r.json()["revoked"] == 1

    r = await client.get("/api/v1/auth/me", headers=auth_headers(member_token))
    assert r.status_code == 401

    set_org_context(session, org_id)
    audit = (
        await session.execute(
            sa.select(AuditLogEntry).where(
                AuditLogEntry.org_id == org_id,
                AuditLogEntry.action == "member.sessions_revoked",
            )
        )
    ).scalars().first()
    assert audit is not None


async def test_admin_revoke_requires_permission(client, session):
    owner_token, member_token, owner_id, member_id, org_id = await _make_org_with_member(
        client,
        session,
        "owner-revoke-perm@example.com",
        "member-revoke-perm@example.com",
    )

    r = await client.delete(
        f"/api/v1/orgs/current/members/{owner_id}/sessions",
        headers=auth_headers(member_token, org_id),
    )
    assert r.status_code == 403


async def test_admin_cannot_revoke_non_member(client, session):
    owner_a_token = await register_and_login(client, "owner-a@example.com")
    org_a = await create_org(client, owner_a_token, "Org A")
    org_a_id = uuid.UUID(org_a["id"])

    user_b_token = await register_and_login(client, "user-b@example.com")
    _org_b = await create_org(client, user_b_token, "Org B")
    user_b_id = await _user_id(session, "user-b@example.com")

    r = await client.delete(
        f"/api/v1/orgs/current/members/{user_b_id}/sessions",
        headers=auth_headers(owner_a_token, org_a_id),
    )
    assert r.status_code == 404

    r = await client.get("/api/v1/auth/me", headers=auth_headers(user_b_token))
    assert r.status_code == 200


async def test_login_events_written_for_every_outcome(client, session):
    password = "correct-horse-battery"
    known_email = "events-known@example.com"
    unknown_email = "events-unknown@example.com"

    r = await client.post(
        "/api/v1/auth/register",
        json={"email": known_email, "password": password, "full_name": "Known"},
    )
    assert r.status_code == 201, r.text

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": known_email, "password": "wrong-password"},
    )
    assert r.status_code == 401

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": unknown_email, "password": "any-password"},
    )
    assert r.status_code == 401

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": known_email, "password": password},
    )
    assert r.status_code == 200

    known_rows = (
        await session.execute(
            sa.select(LoginEvent).where(LoginEvent.email == known_email)
        )
    ).scalars().all()
    assert sorted(row.outcome for row in known_rows) == ["bad_password", "ok"]

    unknown_rows = (
        await session.execute(
            sa.select(LoginEvent).where(LoginEvent.email == unknown_email)
        )
    ).scalars().all()
    assert len(unknown_rows) == 1
    assert unknown_rows[0].outcome == "bad_password"
    assert unknown_rows[0].user_id is None

    all_rows = (await session.execute(sa.select(LoginEvent))).scalars().all()
    for row in all_rows:
        for column in LoginEvent.__table__.columns:
            assert password not in str(getattr(row, column.name))


async def test_me_login_events_scoped_to_self(client, session):
    token_a = await register_and_login(client, "events-me-a@example.com")
    await register_and_login(client, "events-me-b@example.com")
    user_a_id = await _user_id(session, "events-me-a@example.com")
    user_b_id = await _user_id(session, "events-me-b@example.com")

    r = await client.get("/api/v1/me/login-events", headers=auth_headers(token_a))
    assert r.status_code == 200
    ids = {
        uuid.UUID(row["user_id"])
        for row in r.json()
        if row.get("user_id") is not None
    }
    assert user_a_id in ids
    assert user_b_id not in ids


async def test_org_login_events_requires_members_read(client, session):
    owner_token, member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "org-events-owner@example.com",
        "org-events-agent@example.com",
    )

    r = await client.get(
        "/api/v1/orgs/current/login-events", headers=auth_headers(member_token, org_id)
    )
    assert r.status_code == 403

    r = await client.get(
        "/api/v1/orgs/current/login-events", headers=auth_headers(owner_token, org_id)
    )
    assert r.status_code == 200


async def test_org_login_events_csv_escapes_formulas(client, session):
    owner_token = await register_and_login(client, "csv-owner@example.com")
    org = await create_org(client, owner_token, "CSV Org")
    org_id = uuid.UUID(org["id"])
    owner_id = await _user_id(session, "csv-owner@example.com")

    session.add(
        LoginEvent(
            id=uuid.uuid4(),
            user_id=owner_id,
            org_id=org_id,
            email="csv-owner@example.com",
            at=datetime.now(timezone.utc),
            outcome="ok",
            detail="=cmd|/c calc",
        )
    )
    await session.commit()

    r = await client.get(
        "/api/v1/orgs/current/login-events?format=csv",
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "'=cmd|" in r.text


async def test_session_cache_fails_open_on_backend_error(client, session, monkeypatch):
    token = await register_and_login(client, "cache-fail@example.com")
    payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"])
    sid = uuid.UUID(payload["sid"])

    def _boom(*args, **kwargs):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(session_cache._memory_cache, "get", _boom)

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 200

    row = (
        await session.execute(
            sa.select(IdentitySession).where(IdentitySession.id == sid)
        )
    ).scalars().first()
    row.revoked_at = datetime.now(timezone.utc)
    await session.commit()

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 401
