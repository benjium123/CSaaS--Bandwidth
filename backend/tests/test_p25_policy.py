from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.main import create_app
from app.models import AuditLogEntry, LoginEvent, Org, OrgMembership, Role, User
from app.services import identity as identity_svc
from tests.conftest import (
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)


@pytest.fixture(autouse=True)
def _clear_session_cache():
    from app.services import session_cache

    session_cache.reset_memory_cache()
    yield
    session_cache.reset_memory_cache()



def _as_utc(value: str) -> datetime:
    """Parse an API timestamp as UTC-aware.

    On the SQLite backend a timezone=True column round-trips NAIVE, so the serialised
    value carries no offset; on Postgres it does. Normalise so the assertion holds on both.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


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


def _custom_client(settings):
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


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
    org = await create_org(client, owner_token, "Policy Org")
    org_id = uuid.UUID(org["id"])
    member_token = await register_and_login(client, member_email)
    owner_id = await _user_id(session, owner_email)
    member_id = await _user_id(session, member_email)
    await _add_membership(session, user_id=member_id, org_id=org_id, role_name="agent")
    return owner_token, member_token, owner_id, member_id, org_id



async def _give_totp(session, user_id):
    """Mark a user as TOTP-enrolled.

    Enabling require_2fa is refused unless the CALLER already has a second factor (the
    no-lockout guard), so every test that turns the policy on through the API needs this.
    """
    user = await session.get(User, user_id)
    user.totp_enabled = True
    user.totp_secret = "x"
    await session.commit()


async def _set_org_policy(
    session,
    org_id,
    *,
    require_2fa=None,
    grace_until=None,
    ip_allowlist=None,
):
    set_org_context(session, org_id)
    org = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalars().first()
    if require_2fa is not None:
        org.require_2fa = require_2fa
    if grace_until is not None:
        org.require_2fa_grace_until = grace_until
    if ip_allowlist is not None:
        org.ip_allowlist = ip_allowlist
    await session.commit()
    return org


async def test_get_security_requires_settings_read(client, session):
    owner_token, member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "policy-owner@example.com",
        "policy-agent@example.com",
    )

    r = await client.get(
        "/api/v1/orgs/current/security", headers=auth_headers(member_token, org_id)
    )
    assert r.status_code == 403

    r = await client.get(
        "/api/v1/orgs/current/security", headers=auth_headers(owner_token, org_id)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["require_2fa"] is False
    assert not body["ip_allowlist"]
    assert body["sso"] is None


async def test_patch_require_2fa_sets_grace(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "patch-2fa@example.com",
        "patch-2fa-agent@example.com",
    )
    await _give_totp(session, _owner_id)

    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"require_2fa": True},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200

    grace = _as_utc(r.json()["require_2fa_grace_until"])
    expected = datetime.now(timezone.utc) + timedelta(days=7)
    assert abs((grace - expected).total_seconds()) < 120


async def test_patch_require_2fa_does_not_reset_existing_grace(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "patch-2fa-grace@example.com",
        "patch-2fa-grace-agent@example.com",
    )

    past = datetime.now(timezone.utc) - timedelta(days=30)
    await _set_org_policy(session, org_id, require_2fa=True, grace_until=past)
    await _give_totp(session, _owner_id)

    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"require_2fa": True},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200

    new_grace = _as_utc(r.json()["require_2fa_grace_until"])
    assert abs((new_grace - past).total_seconds()) < 2


async def test_require_2fa_before_grace_allows_access(client, session):
    owner_token, member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "2fa-before-owner@example.com",
        "2fa-before-agent@example.com",
    )

    await _set_org_policy(
        session,
        org_id,
        require_2fa=True,
        grace_until=datetime.now(timezone.utc) + timedelta(days=7),
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(member_token, org_id)
    )
    assert r.status_code == 200


async def test_require_2fa_after_grace_blocks(client, session):
    owner_token, member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "2fa-after-owner@example.com",
        "2fa-after-agent@example.com",
    )

    await _set_org_policy(
        session,
        org_id,
        require_2fa=True,
        grace_until=datetime.now(timezone.utc) - timedelta(days=1),
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(member_token, org_id)
    )
    assert r.status_code == 403
    assert _error_code(r) == "two_factor_required"

    # And an ordinary org route is gated too.
    r = await client.get("/api/v1/contacts", headers=auth_headers(member_token, org_id))
    assert r.status_code == 403
    assert _error_code(r) == "two_factor_required"


async def test_require_2fa_exempts_own_profile_and_2fa_setup(client, session):
    owner_token, member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "2fa-exempt-owner@example.com",
        "2fa-exempt-agent@example.com",
    )

    await _set_org_policy(
        session,
        org_id,
        require_2fa=True,
        grace_until=datetime.now(timezone.utc) - timedelta(days=1),
    )

    r = await client.get("/api/v1/auth/me", headers=auth_headers(member_token))
    assert r.status_code == 200

    r = await client.get("/api/v1/me/sessions", headers=auth_headers(member_token))
    assert r.status_code == 200


async def test_owner_is_not_exempt_from_require_2fa(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "2fa-owner-owner@example.com",
        "2fa-owner-agent@example.com",
    )

    await _set_org_policy(
        session,
        org_id,
        require_2fa=True,
        grace_until=datetime.now(timezone.utc) - timedelta(days=1),
    )

    r = await client.get(
        "/api/v1/orgs/current/security", headers=auth_headers(owner_token, org_id)
    )
    assert r.status_code == 403
    assert _error_code(r) == "two_factor_required"


async def test_ip_allowlist_allows_inside_cidr(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "ip-allow-inside-owner@example.com",
        "ip-allow-inside-agent@example.com",
    )

    await _set_org_policy(session, org_id, ip_allowlist=["203.0.113.0/24"])
    headers = {
        **auth_headers(owner_token, org_id),
        "X-Forwarded-For": "203.0.113.7",
    }
    r = await client.get("/api/v1/orgs/current/security", headers=headers)
    assert r.status_code == 200


async def test_ip_allowlist_blocks_outside_cidr(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "ip-allow-block-owner@example.com",
        "ip-allow-block-agent@example.com",
    )

    await _set_org_policy(session, org_id, ip_allowlist=["203.0.113.0/24"])
    headers = {
        **auth_headers(owner_token, org_id),
        "X-Forwarded-For": "198.51.100.4",
    }
    r = await client.get("/api/v1/orgs/current/security", headers=headers)
    assert r.status_code == 403
    assert _error_code(r) == "ip_not_allowed"

    event = (
        await session.execute(
            sa.select(LoginEvent).where(
                LoginEvent.org_id == org_id,
                LoginEvent.outcome == "blocked_ip",
            )
        )
    ).scalars().first()
    assert event is not None


async def test_ip_allowlist_denies_when_ip_unknown(client, session, monkeypatch):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "ip-unknown-owner@example.com",
        "ip-unknown-agent@example.com",
    )

    await _set_org_policy(session, org_id, ip_allowlist=["203.0.113.0/24"])
    monkeypatch.setattr(identity_svc, "client_ip", lambda request: None)

    r = await client.get(
        "/api/v1/orgs/current/security", headers=auth_headers(owner_token, org_id)
    )
    assert r.status_code == 403
    assert _error_code(r) == "ip_not_allowed"


async def test_patch_ip_allowlist_rejects_locking_yourself_out(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "ip-lockout-owner@example.com",
        "ip-lockout-agent@example.com",
    )

    headers = {
        **auth_headers(owner_token, org_id),
        "X-Forwarded-For": "198.51.100.4",
    }
    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"ip_allowlist": ["203.0.113.0/24"]},
        headers=headers,
    )
    assert r.status_code == 422
    assert _error_code(r) == "ip_allowlist_would_lock_you_out"

    org_row = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalars().first()
    assert org_row.ip_allowlist is None


async def test_patch_ip_allowlist_accepts_when_caller_inside(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "ip-inside-owner@example.com",
        "ip-inside-agent@example.com",
    )

    headers = {
        **auth_headers(owner_token, org_id),
        "X-Forwarded-For": "203.0.113.5",
    }
    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"ip_allowlist": ["203.0.113.0/24"]},
        headers=headers,
    )
    assert r.status_code == 200

    org_row = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalars().first()
    assert org_row.ip_allowlist == ["203.0.113.0/24"]


async def test_invalid_cidr_rejected(client, session):
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "ip-invalid-owner@example.com",
        "ip-invalid-agent@example.com",
    )

    headers = {
        **auth_headers(owner_token, org_id),
        "X-Forwarded-For": "203.0.113.5",
    }
    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"ip_allowlist": ["not-a-cidr"]},
        headers=headers,
    )
    assert r.status_code == 422

    org_row = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalars().first()
    assert org_row.ip_allowlist is None


async def test_sso_secret_encrypted_and_never_echoed(session):
    custom_settings = make_settings(
        credentials_master_key=Fernet.generate_key().decode()
    )
    secret = "top-secret-client-secret"

    async with _custom_client(custom_settings) as client:
        owner_token = await register_and_login(client, "sso-secret@example.com")
        org = await create_org(client, owner_token, "SSO Secret Org")
        org_id = uuid.UUID(org["id"])

        r = await client.patch(
            "/api/v1/orgs/current/security",
            json={
                "sso": {
                    "issuer": "https://idp.example.com",
                    "client_id": "client-123",
                    "client_secret": secret,
                    "domain": "example.com",
                    "enforce": False,
                }
            },
            headers=auth_headers(owner_token, org_id),
        )
        assert r.status_code == 200, r.text

        body = r.json()
        assert body["sso"]["client_secret_set"] is True
        assert secret not in r.text

        set_org_context(session, org_id)
        org_row = (
            await session.execute(sa.select(Org).where(Org.id == org_id))
        ).scalars().first()
        assert "client_secret_encrypted" in org_row.sso
        assert "client_secret" not in org_row.sso


async def test_sso_patch_without_secret_keeps_existing(session):
    custom_settings = make_settings(
        credentials_master_key=Fernet.generate_key().decode()
    )
    secret = "keep-me-secret"

    async with _custom_client(custom_settings) as client:
        owner_token = await register_and_login(client, "sso-keep@example.com")
        org = await create_org(client, owner_token, "SSO Keep Org")
        org_id = uuid.UUID(org["id"])

        r = await client.patch(
            "/api/v1/orgs/current/security",
            json={
                "sso": {
                    "issuer": "https://idp.example.com",
                    "client_id": "client-123",
                    "client_secret": secret,
                    "domain": "example.com",
                    "enforce": False,
                }
            },
            headers=auth_headers(owner_token, org_id),
        )
        assert r.status_code == 200, r.text

        set_org_context(session, org_id)
        org_row = (
            await session.execute(sa.select(Org).where(Org.id == org_id))
        ).scalars().first()
        before = org_row.sso["client_secret_encrypted"]

        r = await client.patch(
            "/api/v1/orgs/current/security",
            json={"sso": {"issuer": "https://new-idp.example.com"}},
            headers=auth_headers(owner_token, org_id),
        )
        assert r.status_code == 200, r.text

        await session.commit()
        session.expire_all()
        set_org_context(session, org_id)
        org_row = (
            await session.execute(sa.select(Org).where(Org.id == org_id))
        ).scalars().first()
        assert org_row.sso["client_secret_encrypted"] == before


async def test_sso_enforce_requires_complete_config(client):
    owner_token = await register_and_login(client, "sso-enforce@example.com")
    org = await create_org(client, owner_token, "SSO Enforce Org")
    org_id = uuid.UUID(org["id"])

    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"sso": {"enforce": True}},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 422


async def test_security_patch_writes_audit_entry(session):
    custom_settings = make_settings(
        credentials_master_key=Fernet.generate_key().decode()
    )
    secret = "audit-super-secret"
    cidr = "203.0.113.0/24"

    async with _custom_client(custom_settings) as client:
        owner_token = await register_and_login(client, "audit-secret@example.com")
        org = await create_org(client, owner_token, "Audit Secret Org")
        org_id = uuid.UUID(org["id"])

        r = await client.patch(
            "/api/v1/orgs/current/security",
            json={
                "require_2fa": False,
                "ip_allowlist": [cidr],
                "sso": {
                    "issuer": "https://idp.example.com",
                    "client_id": "client-id",
                    "client_secret": secret,
                    "domain": "example.com",
                    "enforce": False,
                },
            },
            headers={
                **auth_headers(owner_token, org_id),
                "X-Forwarded-For": "203.0.113.5",
            },
        )
        assert r.status_code == 200, r.text

        set_org_context(session, org_id)
        audit = (
            await session.execute(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.org_id == org_id,
                    AuditLogEntry.action == "org.security_updated",
                )
            )
        ).scalars().first()
        assert audit is not None

        fields = audit.detail["fields"]
        assert "require_2fa" in fields
        assert "ip_allowlist" in fields
        assert "sso" in fields
        assert secret not in str(fields)
        assert cidr not in str(fields)


async def test_security_policy_is_per_org(client, session):
    owner_token = await register_and_login(client, "tenancy@example.com")
    org_a = await create_org(client, owner_token, "Tenancy A")
    org_b = await create_org(client, owner_token, "Tenancy B")
    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    set_org_context(session, org_a_id)
    org_a_row = (
        await session.execute(sa.select(Org).where(Org.id == org_a_id))
    ).scalars().first()
    org_a_row.ip_allowlist = ["203.0.113.0/24"]
    await session.commit()

    headers_b = {
        **auth_headers(owner_token, org_b_id),
        "X-Forwarded-For": "198.51.100.4",
    }
    r = await client.get("/api/v1/orgs/current/security", headers=headers_b)
    assert r.status_code == 200

    headers_a = {
        **auth_headers(owner_token, org_a_id),
        "X-Forwarded-For": "198.51.100.4",
    }
    r = await client.get("/api/v1/orgs/current/security", headers=headers_a)
    assert r.status_code == 403
    assert _error_code(r) == "ip_not_allowed"


async def test_enabling_require_2fa_without_own_totp_is_refused(client, session):
    """The policy must never be able to eat its own off switch."""
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "2fa-guard-owner@example.com",
        "2fa-guard-agent@example.com",
    )

    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"require_2fa": True},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 422
    assert _error_code(r) == "two_factor_required_for_actor"

    org = await session.get(Org, org_id)
    await session.refresh(org)
    assert org.require_2fa is False
    assert org.require_2fa_grace_until is None


async def test_turning_require_2fa_off_needs_no_totp(client, session):
    """Only ENABLING is guarded - an org that already has the policy can always relax it
    (the caller necessarily has 2FA to be reaching this route at all)."""
    owner_token, _member_token, _owner_id, _member_id, org_id = await _make_org_with_member(
        client,
        session,
        "2fa-off-owner@example.com",
        "2fa-off-agent@example.com",
    )
    await _give_totp(session, _owner_id)
    await _set_org_policy(
        session,
        org_id,
        require_2fa=True,
        grace_until=datetime.now(timezone.utc) + timedelta(days=1),
    )

    r = await client.patch(
        "/api/v1/orgs/current/security",
        json={"require_2fa": False},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200
    assert r.json()["require_2fa"] is False
