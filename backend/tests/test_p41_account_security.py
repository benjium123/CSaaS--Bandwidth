"""P41a account security: mandatory 2FA, passkeys, risky logins, step-up, operators.

WebAuthn cryptography is py_webauthn's job and is tested there. Here the two verify
functions are replaced with fakes so these tests exercise OUR rules: single-use challenges,
the last-factor guard, the login flow, risk flags, alerts and operator access.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.models import LoginDevice, SecurityAlert, User, WebauthnChallenge
from app.models import Session as IdentitySession
from tests.conftest import auth_headers, create_org, make_settings

PASSWORD = "correct-horse-battery"
DEVICE_A = "device-a-0123456789abcdef"
DEVICE_B = "device-b-0123456789abcdef"


@pytest.fixture
def sec_settings():
    return make_settings(
        require_2fa_privileged_users=True,
        credential_encryption_key=Fernet.generate_key().decode(),
        public_web_url="https://console.example.test",
        app_env="test",
    )


@pytest.fixture
async def sec_client(engine, sec_settings):
    from app.main import create_app

    application = create_app(sec_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def fake_webauthn(monkeypatch):
    """Registration returns a credential derived from the posted id; authentication
    succeeds when the posted credential says ok=True."""
    import webauthn

    def fake_register(*, credential, expected_challenge, **_):
        if credential.get("fail"):
            raise ValueError("bad attestation")
        from webauthn.helpers import base64url_to_bytes

        return SimpleNamespace(
            credential_id=base64url_to_bytes(credential["id"]),
            credential_public_key=b"public-key",
            sign_count=0,
        )

    def fake_authenticate(*, credential, expected_challenge, credential_current_sign_count, **_):
        if not credential.get("ok"):
            raise ValueError("bad signature")
        return SimpleNamespace(new_sign_count=credential_current_sign_count + 1)

    monkeypatch.setattr(webauthn, "verify_registration_response", fake_register)
    monkeypatch.setattr(webauthn, "verify_authentication_response", fake_authenticate)


@pytest.fixture(autouse=True)
def clear_outbox():
    from app.services import mailer

    mailer.outbox.clear()
    yield
    mailer.outbox.clear()


def _cred_id(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


async def _register(client, email: str) -> str:
    r = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "full_name": "X"}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Device-Id": DEVICE_A},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_2fa_enrollment"] is True
    return body["access_token"]


async def _add_passkey(client, token: str, raw_id: str = "cred-1") -> dict:
    opts = await client.post("/api/v1/auth/passkeys/register/options", headers=auth_headers(token))
    assert opts.status_code == 200, opts.text
    r = await client.post(
        "/api/v1/auth/passkeys/register",
        json={
            "challenge_id": opts.json()["challenge_id"],
            "credential": {"id": _cred_id(raw_id), "response": {"transports": ["internal"]}},
            "name": "Laptop",
        },
        headers=auth_headers(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _passkey_login(client, email: str, raw_id: str = "cred-1", device: str = DEVICE_A):
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Device-Id": device},
    )
    assert r.status_code == 200, r.text
    assert r.json()["requires_2fa"] is True
    assert "passkey" in r.json()["methods"]
    pending = r.json()["pending_token"]
    opts = await client.post("/api/v1/auth/passkeys/login/options", json={"pending_token": pending})
    assert opts.status_code == 200, opts.text
    r = await client.post(
        "/api/v1/auth/passkeys/login/verify",
        json={
            "pending_token": pending,
            "challenge_id": opts.json()["challenge_id"],
            "credential": {"id": _cred_id(raw_id), "ok": True},
        },
        headers={"X-Device-Id": device},
    )
    return r


# --------------------------------------------------------------------------------------
# Mandatory 2FA
# --------------------------------------------------------------------------------------
async def test_user_without_second_factor_is_confined_to_enrolment(sec_client):
    token = await _register(sec_client, "nofactor@example.com")
    # Creating an org (or anything else) is refused until a factor exists.
    r = await sec_client.post("/api/v1/orgs", json={"name": "Acme"}, headers=auth_headers(token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "two_factor_required"
    # Enrolment surface and own sessions stay reachable.
    assert (await sec_client.get("/api/v1/auth/me", headers=auth_headers(token))).status_code == 200
    assert (
        await sec_client.post(
            "/api/v1/auth/passkeys/register/options", headers=auth_headers(token)
        )
    ).status_code == 200
    sessions = await sec_client.get("/api/v1/me/sessions", headers=auth_headers(token))
    assert sessions.status_code == 200


async def test_adding_a_passkey_unlocks_the_account(sec_client):
    token = await _register(sec_client, "unlock@example.com")
    await _add_passkey(sec_client, token)
    r = await sec_client.post("/api/v1/orgs", json={"name": "Acme"}, headers=auth_headers(token))
    assert r.status_code == 201, r.text
    me = (await sec_client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert me["has_passkey"] is True


async def test_gate_is_off_when_setting_disabled(client):
    # The default test settings keep pre-P41 behaviour (password-only login works).
    r = await client.post(
        "/api/v1/auth/register", json={"email": "legacy@example.com", "password": PASSWORD}
    )
    assert r.status_code == 201
    r = await client.post(
        "/api/v1/auth/login", json={"email": "legacy@example.com", "password": PASSWORD}
    )
    token = r.json()["access_token"]
    await create_org(client, token, "Legacy Co")


def test_production_refuses_optional_2fa():
    from app.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="REQUIRE_2FA_ALL_USERS"):
        make_settings(app_env="production", require_2fa_privileged_users=False)


# --------------------------------------------------------------------------------------
# Passkeys
# --------------------------------------------------------------------------------------
async def test_passkey_second_factor_login(sec_client, session):
    token = await _register(sec_client, "pk@example.com")
    await _add_passkey(sec_client, token)
    r = await _passkey_login(sec_client, "pk@example.com")
    assert r.status_code == 200, r.text
    new_token = r.json()["access_token"]
    me = await sec_client.get("/api/v1/auth/me", headers=auth_headers(new_token))
    assert me.status_code == 200

    row = (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()
    assert row.second_factor_at is not None


async def test_passkey_challenge_is_single_use(sec_client, session):
    token = await _register(sec_client, "single@example.com")
    await _add_passkey(sec_client, token)
    login = await sec_client.post(
        "/api/v1/auth/login", json={"email": "single@example.com", "password": PASSWORD}
    )
    pending = login.json()["pending_token"]
    opts = await sec_client.post(
        "/api/v1/auth/passkeys/login/options", json={"pending_token": pending}
    )
    challenge_id = opts.json()["challenge_id"]
    bad = await sec_client.post(
        "/api/v1/auth/passkeys/login/verify",
        json={
            "pending_token": pending,
            "challenge_id": challenge_id,
            "credential": {"id": _cred_id("cred-1"), "ok": False},
        },
    )
    assert bad.status_code == 401
    # The failed attempt burned the challenge: a correct assertion cannot reuse it.
    retry = await sec_client.post(
        "/api/v1/auth/passkeys/login/verify",
        json={
            "pending_token": pending,
            "challenge_id": challenge_id,
            "credential": {"id": _cred_id("cred-1"), "ok": True},
        },
    )
    assert retry.status_code == 401
    row = await session.get(WebauthnChallenge, uuid.UUID(challenge_id))
    assert row.consumed_at is not None


async def test_passkey_of_another_user_is_rejected(sec_client):
    t1 = await _register(sec_client, "owner1@example.com")
    await _add_passkey(sec_client, t1, "cred-owner1")
    t2 = await _register(sec_client, "owner2@example.com")
    await _add_passkey(sec_client, t2, "cred-owner2")
    r = await _passkey_login(sec_client, "owner2@example.com", raw_id="cred-owner1")
    assert r.status_code == 401


async def test_cannot_remove_last_second_factor(sec_client):
    token = await _register(sec_client, "last@example.com")
    pk = await _add_passkey(sec_client, token)
    r = await sec_client.delete(f"/api/v1/auth/passkeys/{pk['id']}", headers=auth_headers(token))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "last_second_factor"
    second = await _add_passkey(sec_client, token, "cred-2")
    r = await sec_client.delete(f"/api/v1/auth/passkeys/{pk['id']}", headers=auth_headers(token))
    assert r.status_code == 204
    listed = (await sec_client.get("/api/v1/auth/passkeys", headers=auth_headers(token))).json()
    assert [p["id"] for p in listed] == [second["id"]]


# --------------------------------------------------------------------------------------
# Risky logins
# --------------------------------------------------------------------------------------
async def test_known_device_in_allowed_country_is_not_flagged(sec_client, session, monkeypatch):
    from app.services import login_risk

    monkeypatch.setattr(login_risk, "lookup_country", lambda s, ip: "US")
    token = await _register(sec_client, "clean@example.com")
    await _add_passkey(sec_client, token)
    r = await _passkey_login(sec_client, "clean@example.com")  # remembers DEVICE_A
    assert r.status_code == 200
    r = await _passkey_login(sec_client, "clean@example.com")
    assert r.status_code == 200
    alerts = (await session.execute(sa.select(SecurityAlert))).scalars().all()
    assert alerts == []
    devices = (await session.execute(sa.select(LoginDevice))).scalars().all()
    assert len(devices) == 1


async def test_foreign_country_new_device_and_vpn_are_flagged(sec_client, session, monkeypatch):
    from app.services import login_risk, mailer

    country = {"value": "US"}
    monkeypatch.setattr(login_risk, "lookup_country", lambda s, ip: country["value"])
    token = await _register(sec_client, "traveller@example.com")
    await _add_passkey(sec_client, token)
    assert (await _passkey_login(sec_client, "traveller@example.com")).status_code == 200

    country["value"] = "RU"
    monkeypatch.setattr(login_risk, "lookup_asn_org", lambda s, ip: "M247 Europe SRL")
    r = await _passkey_login(sec_client, "traveller@example.com", device=DEVICE_B)
    # Flagged, but allowed - the second factor was just proven.
    assert r.status_code == 200, r.text

    alert = (await session.execute(sa.select(SecurityAlert))).scalar_one()
    assert alert.kind == "flagged_login"
    assert set(alert.detail["flags"]) == {
        "country_not_allowed",
        "datacenter",
        "new_device",
        "country_changed",
    }
    assert alert.status == "open"
    live = (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()
    assert "country_not_allowed" in live.risk_flags
    alerts = [m for m in mailer.outbox if "unusual sign-in" in m["Subject"]]
    assert len(alerts) == 1
    assert "traveller@example.com" in alerts[0]["To"]


async def test_org_owner_is_emailed_about_a_members_flagged_login(sec_client, session, monkeypatch):
    from app.services import login_risk, mailer

    owner_token = await _register(sec_client, "boss@example.com")
    await _add_passkey(sec_client, owner_token, "cred-boss")
    org = await create_org(sec_client, owner_token, "Boss Co")
    invite = await sec_client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "staff@example.com", "role_name": "agent"},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert invite.status_code in (200, 201), invite.text
    staff_token = await _register(sec_client, "staff@example.com")
    await _add_passkey(sec_client, staff_token, "cred-staff")
    accept_token = invite.json()["accept_url"].split("token=")[1]
    r = await sec_client.post(
        "/api/v1/auth/invites/accept",
        json={"token": accept_token},
        headers=auth_headers(staff_token),
    )
    assert r.status_code == 200, r.text

    monkeypatch.setattr(login_risk, "is_tor_exit", lambda s, ip: True)
    r = await _passkey_login(sec_client, "staff@example.com", raw_id="cred-staff")
    assert r.status_code == 200
    recipients = mailer.outbox[-1]["To"]
    assert "boss@example.com" in recipients and "staff@example.com" in recipients


def test_datacenter_markers():
    from app.services.login_risk import is_datacenter_asn

    assert is_datacenter_asn("DIGITALOCEAN-ASN")
    assert is_datacenter_asn("Amazon.com, Inc.")
    assert not is_datacenter_asn("Comcast Cable Communications, LLC")
    assert not is_datacenter_asn(None)


def test_private_addresses_never_hit_network_signals(sec_settings):
    from app.services import login_risk

    assert login_risk.lookup_country(sec_settings, "10.0.0.5") is None
    assert login_risk.is_tor_exit(sec_settings, "127.0.0.1") is False


# --------------------------------------------------------------------------------------
# Step-up
# --------------------------------------------------------------------------------------
async def test_recent_2fa_step_up(sec_client, session, sec_settings):
    from fastapi import Request

    from app.auth.deps import check_step_up
    from app.errors import StepUpRequiredError

    token = await _register(sec_client, "stepup@example.com")
    await _add_passkey(sec_client, token)
    r = await _passkey_login(sec_client, "stepup@example.com")
    fresh = r.json()["access_token"]
    live = (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()
    user = await session.get(User, live.user_id)
    user_id = user.id

    app_stub = SimpleNamespace(state=SimpleNamespace(settings=sec_settings))
    scope = {"type": "http", "headers": [], "app": app_stub}
    request = Request(scope)
    request.state.session_id = live.id
    await check_step_up(request, session, user, kind="recent_2fa", action="test")

    from datetime import datetime, timedelta, timezone

    live.second_factor_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    await session.commit()
    with pytest.raises(StepUpRequiredError):
        await check_step_up(request, session, user, kind="recent_2fa", action="test")

    # Re-proving with a passkey refreshes it.
    opts = await sec_client.post(
        "/api/v1/auth/passkeys/step-up/options", headers=auth_headers(fresh)
    )
    r = await sec_client.post(
        "/api/v1/auth/passkeys/step-up/verify",
        json={
            "challenge_id": opts.json()["challenge_id"],
            "credential": {"id": _cred_id("cred-1"), "ok": True},
        },
        headers=auth_headers(fresh),
    )
    assert r.status_code == 200, r.text
    session.expire_all()
    user = await session.get(User, user_id)
    await check_step_up(request, session, user, kind="recent_2fa", action="test")


# --------------------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------------------
async def test_operator_access_needs_a_named_operator_with_second_factor(sec_client, session):
    from app.auth.deps import require_operator
    from app.main import create_app  # noqa: F401 - app already built by fixture
    from app.services import operators as operators_svc

    token = await _register(sec_client, "ops@example.com")
    await _add_passkey(sec_client, token)
    r = await _passkey_login(sec_client, "ops@example.com")
    ops_token = r.json()["access_token"]

    me = (await sec_client.get("/api/v1/auth/me", headers=auth_headers(ops_token))).json()
    assert me["is_platform_operator"] is False

    await operators_svc.grant(session, email="ops@example.com", role="reviewer")
    await session.commit()
    me = (await sec_client.get("/api/v1/auth/me", headers=auth_headers(ops_token))).json()
    assert me["is_platform_operator"] is True
    assert callable(require_operator("admin"))


async def test_legacy_ops_routes_accept_an_admin_operator_session(sec_client, session):
    from app.services import operators as operators_svc

    owner = await _register(sec_client, "tenant@example.com")
    await _add_passkey(sec_client, owner, "cred-tenant")
    org = await create_org(sec_client, owner, "Tenant Co")

    token = await _register(sec_client, "admin-op@example.com")
    await _add_passkey(sec_client, token, "cred-admin")
    login = await _passkey_login(sec_client, "admin-op@example.com", raw_id="cred-admin")
    ops_token = login.json()["access_token"]
    path = f"/api/v1/platform/billing/orgs/{org['id']}"
    assert (await sec_client.get(path, headers=auth_headers(ops_token))).status_code == 403

    await operators_svc.grant(session, email="admin-op@example.com", role="reviewer")
    await session.commit()
    # A reviewer is not enough for billing knobs.
    assert (await sec_client.get(path, headers=auth_headers(ops_token))).status_code == 403

    await operators_svc.grant(session, email="admin-op@example.com", role="admin")
    await session.commit()
    assert (await sec_client.get(path, headers=auth_headers(ops_token))).status_code == 200
    # The shared token still works for scripts.
    from tests.conftest import TEST_PLATFORM_OPS_TOKEN

    r = await sec_client.get(path, headers={"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN})
    assert r.status_code == 200
