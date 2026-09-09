from __future__ import annotations

import time
import uuid
from urllib.parse import parse_qsl

import httpx
import pyotp
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.config import Settings
from app.db.base import MissingTenantContextError, set_org_context
from app.errors import ConfigurationError, UnauthenticatedError, ValidationFailedError
from app.main import create_app
from app.models import OrgMembership, Role, User
from app.models.numbers import Brand
from app.repositories import users as users_repo
from app.services import apikeys as apikeys_svc
from tests.conftest import (
    TEST_JWT_SECRET,
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)


async def _attach_member(
    session, org_id: uuid.UUID, email: str, permissions: list[str]
) -> uuid.UUID:
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, email)
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"test-{uuid.uuid4().hex[:8]}",
        permissions=permissions,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return user.id


def _valid_prod_settings(**overrides) -> Settings:
    values = dict(
        app_env="production",
        jwt_secret=TEST_JWT_SECRET,
        session_secret="test-session-secret",
        credential_encryption_key=Fernet.generate_key().decode(),
        credentials_master_key="b" * 32,
        allow_open_registration=False,
        public_base_url="https://api.csaas.test",
        public_web_url="https://console.csaas.test",
        cors_origins="https://console.csaas.test",
        database_url="sqlite+aiosqlite:///:memory:",
        sweeper_enabled=False,
        media_store_backend="memory",
        # Isolate from whatever carrier credentials the real .env happens to define -
        # this helper is about the auth/config guards, not carrier live-ness.
        bandwidth_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


async def test_1_2_totp_replay_rejects_used_step(monkeypatch):
    from app.api.routes.twofa import TOTP_STEP, _check_code

    user = User()
    secret = pyotp.random_base32()
    fixed_now = 1_700_000_000
    monkeypatch.setattr("app.api.routes.twofa.time.time", lambda: fixed_now)

    counter = fixed_now // TOTP_STEP
    code = pyotp.TOTP(secret).at(counter * TOTP_STEP)
    step = _check_code(user, secret, code)
    assert step == counter
    # Callers persist the accepted step onto the user row after a successful check
    # (see enroll/activate/verify/disable) - mirror that here before replaying.
    user.totp_last_used_step = step

    with pytest.raises(UnauthenticatedError):
        _check_code(user, secret, code)

    monkeypatch.setattr("app.api.routes.twofa.time.time", lambda: fixed_now + 30)
    next_code = pyotp.TOTP(secret).at((counter + 1) * TOTP_STEP)
    assert _check_code(user, secret, next_code) == counter + 1


async def test_1_4_member_remove_update_and_last_owner_guard(client, session):
    owner_email = "1.4-owner@example.com"
    member_email = "1.4-member@example.com"
    owner_token = await register_and_login(client, owner_email)
    await register_and_login(client, member_email)
    org = await create_org(client, owner_token, "Org 1.4")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    owner = await users_repo.get_by_email(session, owner_email)
    member = await users_repo.get_by_email(session, member_email)

    agent_role = (
        await session.execute(
            sa.select(Role).where(Role.org_id == org_id, Role.name == "agent")
        )
    ).scalar_one()
    admin_role = (
        await session.execute(
            sa.select(Role).where(Role.org_id == org_id, Role.name == "admin")
        )
    ).scalar_one()

    session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=member.id,
            role_id=agent_role.id,
        )
    )
    await session.commit()

    h = auth_headers(owner_token, org_id)

    r = await client.patch(
        f"/api/v1/orgs/current/members/{member.id}",
        json={"role_name": "admin"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["role_name"] == "admin"

    r = await client.patch(
        f"/api/v1/orgs/current/members/{owner.id}",
        json={"role_name": "agent"},
        headers=h,
    )
    assert r.status_code == 409, r.text

    r = await client.delete(f"/api/v1/orgs/current/members/{member.id}", headers=h)
    assert r.status_code == 204, r.text

    r = await client.delete(f"/api/v1/orgs/current/members/{owner.id}", headers=h)
    assert r.status_code == 409, r.text


# ----------------------------------------------------------------------------------
# C1: PATCH/DELETE members must never let an actor grant/act on a role with more
# permissions than their own (an admin self-promoting to owner, or removing an owner).
# ----------------------------------------------------------------------------------
async def test_c1_admin_self_promote_to_owner_is_forbidden(client, session):
    owner_email = "c1a-owner@example.com"
    admin_email = "c1a-admin@example.com"
    owner_token = await register_and_login(client, owner_email)
    admin_token = await register_and_login(client, admin_email)
    org = await create_org(client, owner_token, "Org C1A")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    admin_user = await users_repo.get_by_email(session, admin_email)
    admin_role = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == "admin"))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=admin_user.id, role_id=admin_role.id)
    )
    await session.commit()

    r = await client.patch(
        f"/api/v1/orgs/current/members/{admin_user.id}",
        json={"role_name": "owner"},
        headers=auth_headers(admin_token, org_id),
    )
    assert r.status_code == 403, r.text


async def test_c1_owner_can_promote_a_member_to_admin(client, session):
    owner_email = "c1b-owner@example.com"
    member_email = "c1b-member@example.com"
    owner_token = await register_and_login(client, owner_email)
    await register_and_login(client, member_email)
    org = await create_org(client, owner_token, "Org C1B")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    member = await users_repo.get_by_email(session, member_email)
    agent_role = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == "agent"))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=member.id, role_id=agent_role.id)
    )
    await session.commit()

    r = await client.patch(
        f"/api/v1/orgs/current/members/{member.id}",
        json={"role_name": "admin"},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["role_name"] == "admin"


async def test_c1_admin_cannot_remove_an_owner(client, session):
    owner_email = "c1c-owner@example.com"
    admin_email = "c1c-admin@example.com"
    owner_token = await register_and_login(client, owner_email)
    admin_token = await register_and_login(client, admin_email)
    org = await create_org(client, owner_token, "Org C1C")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    owner_user = await users_repo.get_by_email(session, owner_email)
    admin_user = await users_repo.get_by_email(session, admin_email)
    admin_role = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == "admin"))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=admin_user.id, role_id=admin_role.id)
    )
    await session.commit()

    r = await client.delete(
        f"/api/v1/orgs/current/members/{owner_user.id}",
        headers=auth_headers(admin_token, org_id),
    )
    assert r.status_code == 403, r.text


# ----------------------------------------------------------------------------------
# E1: PATCH members must also check the CURRENT (target) role, not only the new one -
# an admin could otherwise demote a peer owner by assigning a role that is itself a
# subset of the admin's own permissions.
# ----------------------------------------------------------------------------------
async def test_e1_admin_cannot_demote_a_peer_owner(client, session):
    owner1_email = "e1-owner1@example.com"
    owner2_email = "e1-owner2@example.com"
    admin_email = "e1-admin@example.com"
    owner1_token = await register_and_login(client, owner1_email)
    await register_and_login(client, owner2_email)
    admin_token = await register_and_login(client, admin_email)
    org = await create_org(client, owner1_token, "Org E1")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    owner2_user = await users_repo.get_by_email(session, owner2_email)
    admin_user = await users_repo.get_by_email(session, admin_email)
    owner_role = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == "owner"))
    ).scalar_one()
    admin_role = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == "admin"))
    ).scalar_one()
    session.add_all(
        [
            OrgMembership(
                id=uuid.uuid4(), org_id=org_id, user_id=owner2_user.id, role_id=owner_role.id
            ),
            OrgMembership(
                id=uuid.uuid4(), org_id=org_id, user_id=admin_user.id, role_id=admin_role.id
            ),
        ]
    )
    await session.commit()

    r = await client.patch(
        f"/api/v1/orgs/current/members/{owner2_user.id}",
        json={"role_name": "admin"},
        headers=auth_headers(admin_token, org_id),
    )
    assert r.status_code == 403, r.text

    r2 = await client.patch(
        f"/api/v1/orgs/current/members/{owner2_user.id}",
        json={"role_name": "admin"},
        headers=auth_headers(owner1_token, org_id),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["role_name"] == "admin"


async def test_1_5_existing_user_accepts_invite_to_org(client, session):
    owner_email = "1.5-owner@example.com"
    invitee_email = "1.5-invitee@example.com"
    owner_token = await register_and_login(client, owner_email)
    invitee_token = await register_and_login(client, invitee_email)
    org = await create_org(client, owner_token, "Org 1.5")
    org_id = uuid.UUID(org["id"])

    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": invitee_email, "role_name": "agent"},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 201, r.text
    raw_token = r.json()["token"]

    r = await client.post(
        "/api/v1/auth/invites/accept",
        json={"token": raw_token},
        headers=auth_headers(invitee_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["org_id"] == str(org_id)

    set_org_context(session, org_id)
    invitee = await users_repo.get_by_email(session, invitee_email)
    membership = (
        await session.execute(
            sa.select(OrgMembership).where(
                OrgMembership.org_id == org_id, OrgMembership.user_id == invitee.id
            )
        )
    ).scalar_one_or_none()
    assert membership is not None


async def test_1_6_status_route_requires_platform_operator(engine):
    settings = make_settings(platform_ops_token="opsecret", allow_open_registration=True)
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "1.6-owner@example.com")
        org = await create_org(client, token, "Org 1.6")
        org_id = uuid.UUID(org["id"])
        h = auth_headers(token, org_id)

        r = await client.post(
            "/api/v1/registration/brands",
            json={"name": "Brand 1.6"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        brand_id = r.json()["id"]

        url = f"/api/v1/registration/brands/{brand_id}/status"
        r = await client.post(url, json={"status": "approved"}, headers=h)
        assert r.status_code == 403, r.text

        r = await client.post(
            url,
            json={"status": "approved"},
            headers={**h, "X-Platform-Ops-Token": "wrong"},
        )
        assert r.status_code == 403, r.text

        r = await client.post(
            url,
            json={"status": "approved"},
            headers={**h, "X-Platform-Ops-Token": "opsecret"},
        )
        assert r.status_code == 200, r.text


async def test_1_6_status_route_unset_ops_token_gives_503(engine):
    settings = make_settings(platform_ops_token="", allow_open_registration=True)
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "1.6b-owner@example.com")
        org = await create_org(client, token, "Org 1.6b")
        org_id = uuid.UUID(org["id"])
        h = auth_headers(token, org_id)

        r = await client.post(
            "/api/v1/registration/brands",
            json={"name": "Brand 1.6b"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        brand_id = r.json()["id"]

        r = await client.post(
            f"/api/v1/registration/brands/{brand_id}/status",
            json={"status": "approved"},
            headers={**h, "X-Platform-Ops-Token": "anything"},
        )
        assert r.status_code == 503, r.text


async def test_1_8_api_key_scopes_cannot_exceed_creator_permissions(client, session):
    owner_token = await register_and_login(client, "1.8-owner@example.com")
    org = await create_org(client, owner_token, "Org 1.8")
    org_id = uuid.UUID(org["id"])

    member_email = "1.8-member@example.com"
    await register_and_login(client, member_email)
    member_id = await _attach_member(session, org_id, member_email, ["inbox:read"])

    with pytest.raises(ValidationFailedError):
        await apikeys_svc.create(
            session,
            org_id,
            name="bad",
            scopes=["inbox:read", "compliance:manage"],
            actor_user_id=member_id,
        )

    key, _raw = await apikeys_svc.create(
        session,
        org_id,
        name="ok",
        scopes=["inbox:read"],
        actor_user_id=member_id,
    )
    assert key.scopes == ["inbox:read"]


# ----------------------------------------------------------------------------------
# C2: an API-key-authenticated caller has no actor_user_id/created_by, so the 1.8
# subset check above was entirely skipped for this path - a key could mint a new key
# with scopes exceeding its own. Must gate on the authenticating key's own scopes.
# ----------------------------------------------------------------------------------
async def test_c2_api_key_cannot_mint_a_key_with_broader_scopes_than_its_own(client):
    owner_token = await register_and_login(client, "c2-owner@example.com")
    org = await create_org(client, owner_token, "Org C2")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org_id)

    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "narrow key", "scopes": ["org:update"]},
        headers=h,
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    key_headers = {"Authorization": f"Bearer {key}"}

    r = await client.post(
        "/api/v1/api-keys",
        json={"name": "escalated", "scopes": ["org:update", "compliance:manage"]},
        headers=key_headers,
    )
    assert r.status_code == 422, r.text

    r2 = await client.post(
        "/api/v1/api-keys",
        json={"name": "same-scope", "scopes": ["org:update"]},
        headers=key_headers,
    )
    assert r2.status_code == 201, r2.text


async def test_1_9_2fa_enroll_and_disable_require_password(engine):
    settings = make_settings(
        credential_encryption_key=Fernet.generate_key().decode(),
        allow_open_registration=True,
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "1.9@example.com")
        h = auth_headers(token)

        r = await client.post("/api/v1/auth/2fa/enroll", json={}, headers=h)
        assert r.status_code == 422, r.text

        r = await client.post(
            "/api/v1/auth/2fa/enroll",
            json={"password": "wrong-password"},
            headers=h,
        )
        assert r.status_code == 401, r.text

        r = await client.post(
            "/api/v1/auth/2fa/enroll",
            json={"password": "correct-horse-battery"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        secret = r.json()["secret"]

        code = pyotp.TOTP(secret).now()
        r = await client.post(
            "/api/v1/auth/2fa/activate",
            json={"code": code},
            headers=h,
        )
        assert r.status_code == 200, r.text

        r = await client.post(
            "/api/v1/auth/2fa/disable",
            json={"code": code, "password": "wrong-password"},
            headers=h,
        )
        assert r.status_code == 401, r.text

        next_counter = int(time.time()) // 30 + 1
        fresh_code = pyotp.TOTP(secret).at(next_counter * 30)
        r = await client.post(
            "/api/v1/auth/2fa/disable",
            json={"code": fresh_code, "password": "correct-horse-battery"},
            headers=h,
        )
        assert r.status_code == 200, r.text


async def test_1_12_cross_tenant_delete_is_guarded(client, session):
    token_a = await register_and_login(client, "1.12a-owner@example.com")
    token_b = await register_and_login(client, "1.12b-owner@example.com")
    org_a = await create_org(client, token_a, "Org A")
    org_b = await create_org(client, token_b, "Org B")
    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    set_org_context(session, org_b_id)
    brand = Brand(id=uuid.uuid4(), org_id=org_b_id, name="DeleteMe", status="draft")
    session.add(brand)
    await session.commit()
    await session.refresh(brand)
    assert brand.org_id == org_b_id

    set_org_context(session, org_a_id)
    await session.delete(brand)
    with pytest.raises(MissingTenantContextError):
        await session.flush()
    await session.rollback()


async def test_1_13_rate_limiter_returns_429_with_retry_after(engine):
    # The limiter is a process-wide singleton (by design - no Redis dependency). Clear
    # its state so this test is not order-dependent on whatever else shares the process.
    from app.rate_limit import _limiter

    _limiter._events.clear()

    settings = make_settings(
        rate_limit_enabled=True,
        rate_limit_max_requests=2,
        rate_limit_window_seconds=60,
        allow_open_registration=True,
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"email": "1.13@example.com", "password": "wrong"}
        for _ in range(2):
            r = await client.post("/api/v1/auth/login", json=payload)
            assert r.status_code == 401, r.text

        r = await client.post("/api/v1/auth/login", json=payload)
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers
        # C7: standard {"error": {...}} envelope, not FastAPI's bare {"detail": ...}.
        assert r.json()["error"]["code"] == "rate_limited"


# ----------------------------------------------------------------------------------
# C3: the rate limiter must key on the RIGHTMOST X-Forwarded-For entry (nginx-appended,
# real) never the leftmost (caller-controlled) one, under --forwarded-allow-ips "*".
# ----------------------------------------------------------------------------------
async def test_c3_rate_limit_uses_rightmost_xff_entry(engine):
    from app.rate_limit import _limiter

    _limiter._events.clear()

    settings = make_settings(
        rate_limit_enabled=True,
        rate_limit_max_requests=2,
        rate_limit_window_seconds=60,
        allow_open_registration=True,
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Different emails (so the per-identifier bucket never repeats on its own) but
        # the SAME rightmost XFF entry behind different, caller-controlled leftmost
        # values - must be treated as the SAME client for IP-based rate-limiting.
        r1 = await client.post(
            "/api/v1/auth/login",
            json={"email": "c3-a@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"},
        )
        assert r1.status_code == 401, r1.text
        r2 = await client.post(
            "/api/v1/auth/login",
            json={"email": "c3-b@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"},
        )
        assert r2.status_code == 401, r2.text

        r3 = await client.post(
            "/api/v1/auth/login",
            json={"email": "c3-c@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "3.3.3.3, 9.9.9.9"},
        )
        assert r3.status_code == 429, r3.text


# ----------------------------------------------------------------------------------
# C5: the limiter's key table must never grow unbounded.
# ----------------------------------------------------------------------------------
def test_c5_rate_limiter_key_table_is_capped():
    from app.rate_limit import _MAX_KEYS, SlidingWindowLimiter

    limiter = SlidingWindowLimiter()
    for i in range(_MAX_KEYS + 50):
        limiter.allow(f"key-{i}", max_requests=100, window_seconds=60)

    assert len(limiter._events) <= _MAX_KEYS


def test_1_14_plivo_verify_rejects_empty_auth_token():
    from app.providers.plivo import webhooks as plivo

    headers = {
        "X-Plivo-Signature-V3": "signature",
        "X-Plivo-Signature-V3-Nonce": "nonce",
    }
    assert plivo.verify(headers, "", "https://example.com/webhook") is False


def test_1_15_twilio_replay_protection():
    from app.providers.twilio import webhooks as twilio

    url = f"https://example.com/twilio/{uuid.uuid4()}"
    auth_token = "token"
    body = b"MessageSid=SM123&MessageStatus=delivered"
    params = dict(parse_qsl(body.decode()))
    sig = twilio.expected_signature(url, params, auth_token)
    headers = {"X-Twilio-Signature": sig}

    assert twilio.verify(headers, auth_token, body, url) is True
    assert twilio.verify(headers, auth_token, body, url) is False


def test_1_15_signalwire_replay_protection():
    from app.providers.signalwire import webhooks as signalwire

    url = f"https://example.com/signalwire/{uuid.uuid4()}"
    auth_token = "token"
    body = b"MessageSid=SM123&MessageStatus=delivered"
    params = dict(parse_qsl(body.decode()))
    sig = signalwire.expected_signature(url, params, auth_token)
    headers = {"X-Signalwire-Signature": sig}

    assert signalwire.verify(headers, auth_token, url, body) is True
    assert signalwire.verify(headers, auth_token, url, body) is False


# ----------------------------------------------------------------------------------
# C4: the webhook-replay cache must never grow unbounded within the TTL window.
# ----------------------------------------------------------------------------------
def test_c4_webhook_replay_cache_is_capped():
    from app.providers import webhook_replay

    for i in range(10_050):
        # A large ttl_seconds so nothing here expires mid-test - this exercises the
        # "still over the cap after the expired sweep" eviction path, not the sweep.
        assert webhook_replay.check_and_record(f"sig-{i}", ttl_seconds=3600) is True

    assert len(webhook_replay._SEEN) <= webhook_replay._MAX_ENTRIES


def test_8_11_credentials_master_key_required_and_weak_in_prod():
    with pytest.raises(ConfigurationError):
        _valid_prod_settings(credentials_master_key="")
    with pytest.raises(ConfigurationError):
        _valid_prod_settings(credentials_master_key="short")


def test_8_12_public_web_url_required_in_prod():
    with pytest.raises(ConfigurationError):
        _valid_prod_settings(public_web_url="http://insecure")


def test_1_16_cors_star_with_credentials_rejected_in_prod():
    with pytest.raises(ConfigurationError):
        _valid_prod_settings(cors_origins="*")


async def test_1_17_disabled_account_is_uniform_401(client, session):
    token = await register_and_login(client, "1.17@example.com")
    user = await users_repo.get_by_email(session, "1.17@example.com")
    user.is_active = False
    await session.commit()

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 401, r.text


async def test_1_18_docs_and_openapi_disabled_in_production(engine):
    settings = _valid_prod_settings()
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/docs")
        assert r.status_code == 404, r.text
        r = await client.get("/openapi.json")
        assert r.status_code == 404, r.text


async def test_frontend_support_me_returns_permissions_and_totp_enabled(client):
    token = await register_and_login(client, "1.me@example.com")
    org = await create_org(client, token, "Org Me")
    org_id = uuid.UUID(org["id"])

    r = await client.get(
        "/api/v1/auth/me", headers={**auth_headers(token), "X-Org-Id": str(org_id)}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totp_enabled"] is False
    # The registering user is the owner - owner's role carries the "*" wildcard, which
    # must expand to the full permission catalogue rather than leaking "*" itself.
    assert "*" not in body["permissions"]
    assert "org:read" in body["permissions"]
    assert "members:remove" in body["permissions"]

    # C8: no X-Org-Id, but this user has exactly ONE membership - that org's
    # permissions are returned rather than an uninformative [].
    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    assert "org:read" in r.json()["permissions"]
    assert "members:remove" in r.json()["permissions"]


async def test_c8_me_without_org_id_stays_empty_with_multiple_memberships(client):
    """C8's default-org convenience only applies with EXACTLY one membership - with two
    or more, there is no single correct default, so permissions must stay []."""
    token = await register_and_login(client, "c8-multi@example.com")
    await create_org(client, token, "Org C8 One")
    await create_org(client, token, "Org C8 Two")

    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["permissions"] == []
