"""P42 slice 1: password policy/change/reset, recovery codes, lockout, identity recovery,
admin and operator factor resets."""

from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.models import (
    AccountAuditEntry,
    KycPerson,
    KycStepUp,
    PasswordResetToken,
    SecurityAlert,
    User,
)
from app.models import Session as IdentitySession
from tests.conftest import auth_headers, create_org, make_settings

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "violet-harbour-lantern-92"


@pytest.fixture
def p42_settings():
    return make_settings(
        require_2fa_all_users=True,
        credential_encryption_key=Fernet.generate_key().decode(),
        public_web_url="https://console.example.test",
        password_min_length=12,
        lockout_threshold=3,
    )


@pytest.fixture
async def client42(engine, p42_settings):
    from app.main import create_app

    application = create_app(p42_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def fake_webauthn(monkeypatch):
    import webauthn
    from webauthn.helpers import base64url_to_bytes

    monkeypatch.setattr(
        webauthn,
        "verify_registration_response",
        lambda *, credential, **_: SimpleNamespace(
            credential_id=base64url_to_bytes(credential["id"]),
            credential_public_key=b"pk",
            sign_count=0,
        ),
    )

    def fake_auth(*, credential, credential_current_sign_count, **_):
        if not credential.get("ok"):
            raise ValueError("bad")
        return SimpleNamespace(new_sign_count=credential_current_sign_count + 1)

    monkeypatch.setattr(webauthn, "verify_authentication_response", fake_auth)


@pytest.fixture(autouse=True)
def outbox():
    from app.services import mailer

    mailer.outbox.clear()
    yield mailer.outbox
    mailer.outbox.clear()


def _cred(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


async def _signup_with_passkey(client, email: str, raw: str = "cred-1") -> str:
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    token = r.json()["access_token"]
    opts = await client.post("/api/v1/auth/passkeys/register/options", headers=auth_headers(token))
    r = await client.post(
        "/api/v1/auth/passkeys/register",
        json={"challenge_id": opts.json()["challenge_id"], "credential": {"id": _cred(raw)}},
        headers=auth_headers(token),
    )
    assert r.status_code == 201, r.text
    return token


async def _pending(client, email: str, password: str = PASSWORD) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["pending_token"]


async def _passkey_login(client, email: str, raw: str = "cred-1", password: str = PASSWORD):
    pending = await _pending(client, email, password)
    opts = await client.post("/api/v1/auth/passkeys/login/options", json={"pending_token": pending})
    return await client.post(
        "/api/v1/auth/passkeys/login/verify",
        json={
            "pending_token": pending,
            "challenge_id": opts.json()["challenge_id"],
            "credential": {"id": _cred(raw), "ok": True},
        },
    )


# --------------------------------------------------------------------------------------
# Password policy
# --------------------------------------------------------------------------------------
async def test_policy_rejects_short_email_based_and_breached(monkeypatch):
    from app.errors import ValidationFailedError
    from app.services import password_policy

    settings = make_settings(password_min_length=12, hibp_enabled=True)

    async def breached(s, pw, client=None):
        return 5 if pw == "summer-holiday-2024" else 0

    monkeypatch.setattr(password_policy, "breach_count", breached)
    for bad in ("short-pass", "janedoe-rocks-1234", "summer-holiday-2024", "aaaaaaaaaaaaaaa"):
        with pytest.raises(ValidationFailedError):
            await password_policy.check(settings, bad, email="janedoe@example.com")
    await password_policy.check(settings, NEW_PASSWORD, email="janedoe@example.com")


async def test_hibp_range_lookup_only_sends_prefix():
    from app.services import password_policy

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="0018A45C4D1DEF81644B54AB7F969B88D65:3\nABCDEF:1\n")

    settings = make_settings(hibp_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        count = await password_policy.breach_count(settings, "password1", client=c)
    # sha1("password1") = E38AD214943DAAD1D64C102FAEC29DE4AFE9DA3D
    assert seen[0].endswith("/E38AD")
    assert "password1" not in seen[0]
    assert count == 0


async def test_register_enforces_policy(client42):
    r = await client42.post(
        "/api/v1/auth/register", json={"email": "weak@example.com", "password": "tooshort12"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "weak_password"


# --------------------------------------------------------------------------------------
# Change / forgot / reset
# --------------------------------------------------------------------------------------
async def test_password_reset_never_bypasses_2fa(client42, session, outbox):
    old_token = await _signup_with_passkey(client42, "reset@example.com")
    r = await client42.post("/api/v1/auth/password/forgot", json={"email": "reset@example.com"})
    assert r.status_code == 202
    unknown = await client42.post(
        "/api/v1/auth/password/forgot", json={"email": "nobody@example.com"}
    )
    assert unknown.status_code == 202 and unknown.json() == r.json()

    body = outbox[-1].get_content()
    token = re.search(r"token=([A-Za-z0-9_\-]+)", body).group(1)
    r = await client42.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 204, r.text
    # Link is single-use.
    again = await client42.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD + "x"}
    )
    assert again.status_code == 422
    # Old session ended.
    assert (
        await client42.get("/api/v1/auth/me", headers=auth_headers(old_token))
    ).status_code == 401
    # New password gets only a pending token: the passkey is still required.
    r = await client42.post(
        "/api/v1/auth/login", json={"email": "reset@example.com", "password": NEW_PASSWORD}
    )
    assert r.json()["requires_2fa"] is True and r.json()["access_token"] is None
    actions = (await session.execute(sa.select(AccountAuditEntry.action))).scalars().all()
    assert "password.reset" in actions


async def test_expired_reset_token_is_refused(client42, session, outbox):
    await _signup_with_passkey(client42, "expired@example.com")
    await client42.post("/api/v1/auth/password/forgot", json={"email": "expired@example.com"})
    token = re.search(r"token=([A-Za-z0-9_\-]+)", outbox[-1].get_content()).group(1)
    row = (await session.execute(sa.select(PasswordResetToken))).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    r = await client42.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "reset_invalid"


async def test_change_password_needs_fresh_factor_and_ends_other_sessions(client42, session):
    first = await _signup_with_passkey(client42, "change@example.com")
    r = await _passkey_login(client42, "change@example.com")
    current = r.json()["access_token"]
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}

    # Session proven 30 minutes ago: step-up required.
    live = (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()
    live.second_factor_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    await session.commit()
    r = await client42.post(
        "/api/v1/auth/password/change", json=body, headers=auth_headers(current)
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"

    live.second_factor_at = datetime.now(timezone.utc)
    await session.commit()
    r = await client42.post(
        "/api/v1/auth/password/change", json=body, headers=auth_headers(current)
    )
    assert r.status_code == 204, r.text
    assert (await client42.get("/api/v1/auth/me", headers=auth_headers(current))).status_code == 200
    assert (await client42.get("/api/v1/auth/me", headers=auth_headers(first))).status_code == 401


# --------------------------------------------------------------------------------------
# Recovery codes
# --------------------------------------------------------------------------------------
async def test_recovery_codes_are_single_use_and_flag_the_login(client42, session, outbox):
    token = await _signup_with_passkey(client42, "codes@example.com")
    r = await client42.post("/api/v1/auth/recovery-codes", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    codes = r.json()["codes"]
    assert len(codes) == 10 and len(set(codes)) == 10

    pending = await _pending(client42, "codes@example.com")
    r = await client42.post(
        "/api/v1/auth/2fa/recovery", json={"pending_token": pending, "code": codes[0].upper()}
    )
    assert r.status_code == 200, r.text
    assert r.json()["recovery_codes_remaining"] == 9

    pending = await _pending(client42, "codes@example.com")
    r = await client42.post(
        "/api/v1/auth/2fa/recovery", json={"pending_token": pending, "code": codes[0]}
    )
    assert r.status_code == 401

    alert = (
        await session.execute(
            sa.select(SecurityAlert).where(SecurityAlert.kind == "recovery_code_used")
        )
    ).scalar_one()
    assert alert.detail["remaining"] == 9
    live = (
        (
            await session.execute(
                sa.select(IdentitySession)
                .where(IdentitySession.risk_flags.is_not(None))
                .order_by(IdentitySession.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert "recovery_code" in live.risk_flags
    assert any("recovery code" in m["Subject"].lower() for m in outbox)


# --------------------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------------------
async def test_lockout_after_repeated_failures(client42, session, outbox):
    await _signup_with_passkey(client42, "lock@example.com")
    for _ in range(3):
        r = await client42.post(
            "/api/v1/auth/login",
            json={"email": "lock@example.com", "password": "wrong-password-123"},
        )
        assert r.status_code == 401
    # Still the generic answer for a wrong password...
    r = await client42.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "wrong-password-123"}
    )
    assert r.status_code == 401
    # ...but the right password is told about the lock.
    r = await client42.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": PASSWORD}
    )
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "account_locked"
    assert any("locked" in m["Subject"].lower() for m in outbox)

    from app.services import lockout

    user = (
        await session.execute(sa.select(User).where(User.email == "lock@example.com"))
    ).scalar_one()
    await lockout.unlock(session, user.id, actor_user_id=user.id, request=None)
    await session.commit()
    r = await client42.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": PASSWORD}
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------------------
# Identity recovery
# --------------------------------------------------------------------------------------
async def test_identity_recovery_clears_factors_and_blocks_sensitive_actions(
    client42, session, monkeypatch
):
    from app.services import kyc, kyc_step_up, stripe_client

    await _signup_with_passkey(client42, "lost@example.com")
    user = (
        await session.execute(sa.select(User).where(User.email == "lost@example.com"))
    ).scalar_one()
    org_token_user = user.id
    # A verified person record from business verification.
    from app.models import Org

    org = Org(id=uuid.uuid4(), name="Lost Co", slug=f"lost-{uuid.uuid4().hex[:6]}")
    session.add(org)
    await session.flush()
    from app.db.base import set_org_context

    set_org_context(session, org.id)
    session.add(
        KycPerson(
            id=uuid.uuid4(),
            org_id=org.id,
            role="owner",
            full_name="Lost Owner",
            user_id=org_token_user,
            status="verified",
            identity_hash=kyc.identity_hash("Lost", "Owner", "1985-01-01"),
        )
    )
    await session.commit()

    async def fake_create(settings, *, metadata, return_url=None):
        return {
            "id": "vs_recover",
            "url": "https://verify.stripe.test/x",
            "client_secret": "s",
            "status": "requires_input",
        }

    monkeypatch.setattr(stripe_client, "create_verification_session", fake_create)

    pending = await _pending(client42, "lost@example.com")
    r = await client42.post("/api/v1/auth/recovery/identity/start", json={"pending_token": pending})
    assert r.status_code == 200, r.text
    step_up_id = r.json()["step_up_id"]

    # Not verified yet -> refused.
    r = await client42.post(
        "/api/v1/auth/recovery/identity/complete",
        json={"pending_token": pending, "step_up_id": step_up_id},
    )
    assert r.status_code == 422

    await kyc_step_up.apply_outcome(
        session,
        "vs_recover",
        {"status": "verified", "first_name": "Lost", "last_name": "Owner", "dob": "1985-01-01"},
    )
    await session.commit()
    r = await client42.post(
        "/api/v1/auth/recovery/identity/complete",
        json={"pending_token": pending, "step_up_id": step_up_id},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    session.expire_all()
    user = (
        await session.execute(sa.select(User).where(User.email == "lost@example.com"))
    ).scalar_one()
    assert user.has_passkey is False and user.step_up_blocked_until is not None
    row = await session.get(KycStepUp, uuid.UUID(step_up_id))
    assert row.consumed_at is not None
    # Enroll-only now (no factor), and sensitive actions wait out the cool-down.
    me = (await client42.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert me["second_factor_required"] is True
    assert (
        await session.execute(
            sa.select(SecurityAlert).where(SecurityAlert.kind == "account_recovery")
        )
    ).scalar_one()


async def test_identity_recovery_rejects_someone_elses_id(client42, session, monkeypatch):
    from app.services import kyc, kyc_step_up, stripe_client

    await _signup_with_passkey(client42, "victim@example.com")
    user = (
        await session.execute(sa.select(User).where(User.email == "victim@example.com"))
    ).scalar_one()
    victim_id = user.id
    from app.db.base import set_org_context
    from app.models import Org

    org = Org(id=uuid.uuid4(), name="V", slug=f"v-{uuid.uuid4().hex[:6]}")
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)
    session.add(
        KycPerson(
            id=uuid.uuid4(),
            org_id=org.id,
            role="owner",
            full_name="Real Owner",
            user_id=user.id,
            status="verified",
            identity_hash=kyc.identity_hash("Real", "Owner", "1970-02-02"),
        )
    )
    await session.commit()

    async def fake_create(settings, *, metadata, return_url=None):
        return {"id": "vs_attacker", "url": "u", "client_secret": "s", "status": "requires_input"}

    monkeypatch.setattr(stripe_client, "create_verification_session", fake_create)
    pending = await _pending(client42, "victim@example.com")
    step_up_id = (
        await client42.post("/api/v1/auth/recovery/identity/start", json={"pending_token": pending})
    ).json()["step_up_id"]
    await kyc_step_up.apply_outcome(
        session,
        "vs_attacker",
        {"status": "verified", "first_name": "Mallory", "last_name": "X", "dob": "1990-03-03"},
    )
    await session.commit()
    r = await client42.post(
        "/api/v1/auth/recovery/identity/complete",
        json={"pending_token": pending, "step_up_id": step_up_id},
    )
    assert r.status_code == 422
    session.expire_all()
    assert (
        await session.execute(sa.select(User.has_passkey).where(User.id == victim_id))
    ).scalar_one() is True


# --------------------------------------------------------------------------------------
# Admin reset
# --------------------------------------------------------------------------------------
async def _add_member(session, org_id, email: str, role_name: str):
    from app.db.base import set_org_context
    from app.models import OrgMembership, Role

    set_org_context(session, uuid.UUID(org_id))
    role = (await session.execute(sa.select(Role).where(Role.name == role_name))).scalar_one()
    user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=uuid.UUID(org_id), user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return user


async def test_admin_can_reset_an_agent_but_not_an_admin(client42, session, outbox):
    await _signup_with_passkey(client42, "boss@reset.example", "cred-boss")
    boss = (await _passkey_login(client42, "boss@reset.example", "cred-boss")).json()[
        "access_token"
    ]
    org = await create_org(client42, boss, "Reset Co")
    await _signup_with_passkey(client42, "agent@reset.example", "cred-agent")
    agent = await _add_member(session, org["id"], "agent@reset.example", "agent")
    await _signup_with_passkey(client42, "admin@reset.example", "cred-admin")
    admin = await _add_member(session, org["id"], "admin@reset.example", "admin")
    h = auth_headers(boss, org["id"])

    r = await client42.post(f"/api/v1/orgs/current/members/{admin.id}/reset-2fa", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "privileged_member"

    agent_id = agent.id
    r = await client42.post(f"/api/v1/orgs/current/members/{agent.id}/reset-2fa", headers=h)
    assert r.status_code == 204, r.text
    session.expire_all()
    assert (
        await session.execute(sa.select(User.has_passkey).where(User.id == agent_id))
    ).scalar_one() is False
    assert "agent@reset.example" in outbox[-1]["To"]
