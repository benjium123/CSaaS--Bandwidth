from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - populates Base.metadata
from app.db.base import Base
from app.errors import CsaasError, ValidationFailedError
from app.models import (
    KycProfile,
    Org,
    OrgMembership,
    PlatformOperator,
    Subscription,
    User,
)
from app.models import Session as IdentitySession
from app.repositories import users as users_repo
from app.services import admin_invites, operators
from tests.conftest import auth_headers, register_and_login

ADMIN_REGISTER = "/api/v1/auth/admin/register"
ADMIN_LOGIN = "/api/v1/auth/admin/login"
ADMIN_ACCEPT = "/api/v1/auth/admin/invites/accept"

PASSWORD = "correct-horse-battery"


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
async def issue_invite(session, email: str, *, expires_hours: int = 24):
    """Trusted issuance: issue + commit, returning (row, plaintext token)."""
    row, token = await admin_invites.issue(session, email=email, expires_hours=expires_hours)
    await session.commit()
    return row, token


async def count(session, model) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(model)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def get_user(session, email: str) -> User | None:
    return (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one_or_none()


async def get_operator(session, user_id) -> PlatformOperator | None:
    return (
        await session.execute(
            sa.select(PlatformOperator)
            .where(PlatformOperator.user_id == user_id)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one_or_none()


async def session_count(session, user_id) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(IdentitySession)
            .where(IdentitySession.user_id == user_id)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def make_operator(session, email: str, *, is_active: bool = True) -> User:
    """Create a user + platform operator directly (trusted setup)."""
    user = await users_repo.create_user(
        session, email=email, password=PASSWORD, full_name=email.split("@")[0]
    )
    await session.commit()
    op = await operators.grant(session, email=email, role="admin")
    if not is_active:
        op.is_active = False
    await session.commit()
    return user


# ----------------------------------------------------------------------------------
# registration
# ----------------------------------------------------------------------------------
async def test_admin_register_success(client, session):
    _, token = await issue_invite(session, "newadmin@example.com")
    r = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "newadmin@example.com",
            "password": PASSWORD,
            "full_name": "New Admin",
            "invite_token": token,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "newadmin@example.com"
    assert body["full_name"] == "New Admin"
    assert body["is_platform_operator"] is True
    assert body["operator_role"] == "admin"
    assert body["requires_2fa_enrollment"] is True
    assert "access_token" not in body

    assert await count(session, User) == 1
    assert await count(session, Org) == 0
    assert await count(session, OrgMembership) == 0
    assert await count(session, KycProfile) == 0
    assert await count(session, Subscription) == 0
    user = await get_user(session, "newadmin@example.com")
    assert user is not None
    assert await session_count(session, user.id) == 0
    op = await get_operator(session, user.id)
    assert op is not None and op.role == "admin" and op.is_active is True


async def test_admin_register_missing_invite_is_422(client):
    r = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "nobody@example.com",
            "password": PASSWORD,
            "full_name": "Nobody",
            "invite_token": "not-a-real-token",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_admin_invite"


async def test_admin_register_expired_invite_is_422(client, session):
    row, token = await issue_invite(session, "expired@example.com")
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    r = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "expired@example.com",
            "password": PASSWORD,
            "full_name": "Expired",
            "invite_token": token,
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_admin_invite"


async def test_admin_register_reused_invite_is_422(client, session):
    _, token = await issue_invite(session, "reuse@example.com")
    first = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "reuse@example.com",
            "password": PASSWORD,
            "full_name": "Reuse",
            "invite_token": token,
        },
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "reuse@example.com",
            "password": PASSWORD,
            "full_name": "Reuse",
            "invite_token": token,
        },
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "invalid_admin_invite"


async def test_admin_register_wrong_email_invite_is_422(client, session):
    _, token = await issue_invite(session, "bound@example.com")
    r = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "other@example.com",
            "password": PASSWORD,
            "full_name": "Other",
            "invite_token": token,
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_admin_invite"


async def test_admin_register_email_normalized(client, session):
    _, token = await issue_invite(session, "  Mixed@Example.COM  ")
    r = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "mixed@example.com",
            "password": PASSWORD,
            "full_name": "Mixed",
            "invite_token": token,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "mixed@example.com"


async def test_admin_register_existing_email_409_keeps_invite_and_password(client, session):
    await register_and_login(client, "existing@example.com")
    user = await get_user(session, "existing@example.com")
    original_hash = user.hashed_password

    row, token = await issue_invite(session, "existing@example.com")
    r = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "existing@example.com",
            "password": "a-brand-new-password",
            "full_name": "Existing",
            "invite_token": token,
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "existing_account_login_required"

    await session.refresh(row)
    assert row.consumed_at is None
    user = await get_user(session, "existing@example.com")
    await session.refresh(user)
    assert user.hashed_password == original_hash

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "existing@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text


async def test_admin_register_password_policy_leaves_token_reusable(
    client, session, monkeypatch
):
    _, token = await issue_invite(session, "weak@example.com")

    from app.services import password_policy

    async def _reject(*args, **kwargs):
        raise ValidationFailedError("password too weak")

    monkeypatch.setattr(password_policy, "check", _reject)
    bad = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "weak@example.com",
            "password": PASSWORD,
            "full_name": "Weak",
            "invite_token": token,
        },
    )
    assert bad.status_code == 422, bad.text
    monkeypatch.undo()

    good = await client.post(
        ADMIN_REGISTER,
        json={
            "email": "weak@example.com",
            "password": PASSWORD,
            "full_name": "Weak",
            "invite_token": token,
        },
    )
    assert good.status_code == 201, good.text


# ----------------------------------------------------------------------------------
# login
# ----------------------------------------------------------------------------------
async def test_admin_login_nonadmin_is_403(client):
    await register_and_login(client, "plain@example.com")
    r = await client.post(
        ADMIN_LOGIN, json={"email": "plain@example.com", "password": PASSWORD}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "admin_access_required"


async def test_admin_login_nonadmin_creates_no_session_or_token(client, session):
    await register_and_login(client, "nosession@example.com")
    user = await get_user(session, "nosession@example.com")
    before = await session_count(session, user.id)
    r = await client.post(
        ADMIN_LOGIN, json={"email": "nosession@example.com", "password": PASSWORD}
    )
    assert r.status_code == 403
    assert r.json().get("access_token") is None
    assert await session_count(session, user.id) == before


async def test_admin_login_enrolled_nonadmin_is_403_no_pending(client, session):
    await register_and_login(client, "enrolled-nonadmin@example.com")
    user = await get_user(session, "enrolled-nonadmin@example.com")
    user.totp_enabled = True
    await session.commit()
    before = await session_count(session, user.id)
    r = await client.post(
        ADMIN_LOGIN,
        json={"email": "enrolled-nonadmin@example.com", "password": PASSWORD},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "admin_access_required"
    assert r.json().get("pending_token") is None
    assert r.json().get("access_token") is None
    assert await session_count(session, user.id) == before


async def test_admin_login_inactive_operator_is_403(client, session):
    await make_operator(session, "inactive@example.com", is_active=False)
    r = await client.post(
        ADMIN_LOGIN, json={"email": "inactive@example.com", "password": PASSWORD}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "admin_access_required"


async def test_admin_login_unknown_email_is_401(client):
    r = await client.post(
        ADMIN_LOGIN, json={"email": "ghost@example.com", "password": PASSWORD}
    )
    assert r.status_code == 401


async def test_admin_login_wrong_password_is_401(client, session):
    await make_operator(session, "op@example.com")
    r = await client.post(
        ADMIN_LOGIN, json={"email": "op@example.com", "password": "wrong-password"}
    )
    assert r.status_code == 401


async def test_admin_login_factorless_operator_requires_enrollment(client, session):
    await make_operator(session, "factorless@example.com")
    r = await client.post(
        ADMIN_LOGIN, json={"email": "factorless@example.com", "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("requires_2fa_enrollment") is True
    assert body.get("access_token")


async def test_admin_login_enrolled_operator_requires_pending_flow(client, session):
    user = await make_operator(session, "enrolled@example.com")
    user.totp_enabled = True
    await session.commit()
    r = await client.post(
        ADMIN_LOGIN, json={"email": "enrolled@example.com", "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] is None
    assert body.get("requires_2fa") is True
    assert body.get("pending_token")


# ----------------------------------------------------------------------------------
# invite accept
# ----------------------------------------------------------------------------------
async def test_admin_accept_grants_admin_without_password_change(client, session):
    await register_and_login(client, "grant@example.com")
    user = await get_user(session, "grant@example.com")
    original_hash = user.hashed_password
    orgs_before = await count(session, Org)

    _, token = await issue_invite(session, "grant@example.com")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "grant@example.com", "password": PASSWORD}
    )
    access = login.json()["access_token"]

    r = await client.post(
        ADMIN_ACCEPT, json={"invite_token": token}, headers=auth_headers(access)
    )
    assert r.status_code == 200, r.text

    user = await get_user(session, "grant@example.com")
    await session.refresh(user)
    assert user.hashed_password == original_hash
    op = await get_operator(session, user.id)
    assert op is not None and op.role == "admin"
    assert await count(session, Org) == orgs_before


async def test_admin_accept_requires_auth(client, session):
    _, token = await issue_invite(session, "noauth@example.com")
    r = await client.post(ADMIN_ACCEPT, json={"invite_token": token})
    assert r.status_code == 401


# ----------------------------------------------------------------------------------
# public register never elevates
# ----------------------------------------------------------------------------------
async def test_public_register_invalid_account_type_is_422(client, session):
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "badtype@example.com",
            "password": PASSWORD,
            "full_name": "Bad",
            "account_type": "admin",
        },
    )
    assert r.status_code == 422
    assert await get_user(session, "badtype@example.com") is None


async def test_public_register_extra_flags_do_not_elevate(client, session):
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": PASSWORD,
            "full_name": "Sneaky",
            "account_type": "business",
            "is_platform_operator": True,
            "operator_role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    user = await get_user(session, "sneaky@example.com")
    assert user is not None
    assert await get_operator(session, user.id) is None


# ----------------------------------------------------------------------------------
# service semantics
# ----------------------------------------------------------------------------------
async def test_issue_stores_hash_not_plaintext(session):
    row, token = await issue_invite(session, "hashcheck@example.com")
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert len(row.token_hash) == 64
    assert token not in (row.token_hash or "")
    assert row.consumed_at is None
    assert row.issued_via


async def test_consume_is_atomic_and_marks_consumed(session):
    row, token = await issue_invite(session, "consume@example.com")
    consumed = await admin_invites.consume(
        session, token=token, email="consume@example.com"
    )
    assert consumed.id == row.id
    assert consumed.consumed_at is not None

    with pytest.raises(CsaasError) as exc:
        await admin_invites.consume(session, token=token, email="consume@example.com")
    assert exc.value.code == "invalid_admin_invite"


async def test_consume_wrong_email_rejected(session):
    _, token = await issue_invite(session, "bound2@example.com")
    with pytest.raises(CsaasError) as exc:
        await admin_invites.consume(session, token=token, email="other@example.com")
    assert exc.value.code == "invalid_admin_invite"


# ----------------------------------------------------------------------------------
# MFA invariant
# ----------------------------------------------------------------------------------
async def test_active_operator_requires_second_factor(client, session, settings):
    from app.services import second_factor

    user = await make_operator(session, "mfa@example.com")
    assert await second_factor.requires_second_factor(session, settings, user) is True

    login = await client.post(
        "/api/v1/auth/login", json={"email": "mfa@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body.get("requires_2fa_enrollment") is True or body.get("requires_2fa") is True


async def test_factorless_operator_me_and_orgs_blocked(client, session):
    await make_operator(session, "blocked@example.com")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "blocked@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body.get("requires_2fa_enrollment") is True
    token = body.get("access_token")
    assert token

    me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200, me.text
    assert me.json().get("second_factor_required") is True

    orgs = await client.post(
        "/api/v1/orgs", json={"name": "Blocked Org"}, headers=auth_headers(token)
    )
    assert orgs.status_code == 403, orgs.text
    assert orgs.json()["error"]["code"] == "two_factor_required"

    queue = await client.get("/api/v1/ops/queue", headers=auth_headers(token))
    assert queue.status_code == 403, queue.text
    assert queue.json()["error"]["code"] == "two_factor_required"


# ----------------------------------------------------------------------------------
# CLI helper
# ----------------------------------------------------------------------------------
async def test_build_invite_url_keeps_token_in_fragment():
    from scripts.invite_admin import _build_invite_url

    url = _build_invite_url("https://console.example.com", "tok-123", "a@example.com")
    assert url.startswith("https://console.example.com")
    assert "tok-123" in url.split("#", 1)[1]
    assert "tok-123" not in url.split("#", 1)[0]


# ----------------------------------------------------------------------------------
# concurrency race (file-backed SQLite, independent sessions)
# ----------------------------------------------------------------------------------
async def test_concurrent_consume_exactly_one_wins(tmp_path):
    db_file = tmp_path / "race.sqlite3"
    url = f"sqlite+aiosqlite:///{db_file}"
    eng = create_async_engine(url)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with maker() as s:
            _, token = await admin_invites.issue(
                s, email="race@example.com", expires_hours=24
            )
            await s.commit()

        async def attempt():
            async with maker() as s:
                try:
                    await admin_invites.consume(
                        s, token=token, email="race@example.com"
                    )
                    await s.commit()
                    return "ok"
                except CsaasError as exc:
                    await s.rollback()
                    return exc.code

        results = await asyncio.wait_for(
            asyncio.gather(attempt(), attempt()), timeout=30
        )
        assert results.count("ok") == 1, results
        assert results.count("invalid_admin_invite") == 1, results
    finally:
        await eng.dispose()
