"""P43 slice 0: regression tests for the auth and verification bugs found by the P41/P42 debug pass.

One test (or a small group) per finding, named after what the attacker could do before.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pyotp
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.auth.security import encrypt_credential
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.main import create_app
from app.models import (
    AccountLockout,
    ApiKey,
    KycPerson,
    KycStepUp,
    Org,
    OrgMembership,
    Role,
    User,
)
from app.models import Session as IdentitySession
from tests.conftest import (
    auth_headers,
    create_org,
    make_settings,
    mark_recent_2fa,
    register_and_login,
)

FERNET = Fernet.generate_key().decode()


@pytest.fixture
def fix_settings():
    return make_settings(credentials_master_key=FERNET, credential_encryption_key=FERNET)


@pytest.fixture
async def api(engine, fix_settings):
    application = create_app(fix_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _unscoped(stmt):
    return stmt.execution_options(**{ALLOW_UNSCOPED_KEY: True})


async def _user(session, email: str) -> User:
    return (
        await session.execute(_unscoped(sa.select(User).where(User.email == email)))
    ).scalar_one()


async def _role(session, org_id: uuid.UUID, name: str) -> Role:
    set_org_context(session, org_id)
    return (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == name))
    ).scalar_one()


async def _add_member(api, session, org_id: uuid.UUID, role_name: str) -> tuple[str, str]:
    email = f"{role_name}-{uuid.uuid4().hex[:8]}@example.com"
    token = await register_and_login(api, email)
    user = await _user(session, email)
    role = await _role(session, org_id, role_name)
    session.add(OrgMembership(org_id=org_id, user_id=user.id, role_id=role.id))
    await session.commit()
    return email, token


async def _owner_org(api, session) -> tuple[str, str, uuid.UUID]:
    email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    token = await register_and_login(api, email)
    org = await create_org(api, token, f"Org {uuid.uuid4().hex[:6]}")
    return email, token, uuid.UUID(org["id"])


SSO = {
    "issuer": "https://idp.example.com",
    "client_id": "c",
    "client_secret": "s",
    "domain": "example.com",
}


# --- 1. critical: SSO takeover by an admin ---------------------------------------------


async def test_admin_cannot_repoint_sso(api, session):
    _owner_email, _owner_token, org_id = await _owner_org(api, session)
    admin_email, admin_token = await _add_member(api, session, org_id, "admin")
    await mark_recent_2fa(session, admin_email)
    r = await api.patch(
        "/api/v1/orgs/current/security",
        json={"sso": SSO},
        headers=auth_headers(admin_token, org_id),
    )
    assert r.status_code == 403
    r = await api.patch(
        "/api/v1/orgs/current/security",
        json={"trust_idp_mfa": True},
        headers=auth_headers(admin_token, org_id),
    )
    assert r.status_code == 403


async def test_owner_needs_fresh_second_factor_to_change_sso(api, session):
    owner_email, owner_token, org_id = await _owner_org(api, session)
    h = auth_headers(owner_token, org_id)
    r = await api.patch("/api/v1/orgs/current/security", json={"sso": SSO}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "step_up_required"
    await mark_recent_2fa(session, owner_email)
    r = await api.patch("/api/v1/orgs/current/security", json={"sso": SSO}, headers=h)
    assert r.status_code == 200, r.text


async def test_api_key_cannot_change_sso(api, session):
    owner_email, owner_token, org_id = await _owner_org(api, session)
    r = await api.post(
        "/api/v1/api-keys",
        json={"name": "settings", "scopes": ["settings:read", "settings:write"]},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    r = await api.patch(
        "/api/v1/orgs/current/security",
        json={"sso": SSO},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403


# --- 2. owner role as the SSO default ----------------------------------------------------


async def test_sso_default_role_cannot_be_owner(api, session):
    owner_email, owner_token, org_id = await _owner_org(api, session)
    await mark_recent_2fa(session, owner_email)
    owner_role = await _role(session, org_id, "owner")
    await session.commit()
    r = await api.patch(
        "/api/v1/orgs/current/security",
        json={"sso": {**SSO, "default_role_id": str(owner_role.id)}},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "sso_owner_role"


async def test_provisioning_ignores_an_owner_default_role(api, session):
    from app.services.sso_provisioning import _role_for_new_member

    _e, _t, org_id = await _owner_org(api, session)
    owner_role = await _role(session, org_id, "owner")
    org = await session.get(Org, org_id)
    org.sso = {"default_role_id": str(owner_role.id), "group_roles": {"admins": "owner"}}
    await session.commit()
    role = await _role_for_new_member(session, org, ["admins"])
    assert "*" not in (role.permissions or [])


# --- 3. passkey add/remove refreshing the second factor ---------------------------------


async def test_adding_or_removing_a_passkey_needs_fresh_2fa(api, session):
    email = f"pk-{uuid.uuid4().hex[:8]}@example.com"
    token = await register_and_login(api, email)
    user = await _user(session, email)
    user.totp_enabled = True
    user.totp_secret = encrypt_credential(pyotp.random_base32(), FERNET)
    await session.commit()
    h = {"Authorization": f"Bearer {token}"}
    r = await api.post("/api/v1/auth/passkeys/register/options", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"
    r = await api.delete(f"/api/v1/auth/passkeys/{uuid.uuid4()}", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"
    await mark_recent_2fa(session, email)
    r = await api.post("/api/v1/auth/passkeys/register/options", headers=h)
    assert r.status_code == 200, r.text


# --- 5. re-verification by a different person -------------------------------------------


async def test_reverification_with_someone_elses_id_is_refused():
    from app.services import kyc as kyc_svc

    person = KycPerson(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        role="owner",
        full_name="Jane Smith",
        status="pending",
        identity_hash=kyc_svc.identity_hash("Jane", "Smith", "1980-04-02"),
    )
    ok = await kyc_svc.apply_person_outcome(
        None,
        person,
        {"status": "verified", "first_name": "Evil", "last_name": "Buyer", "dob": "1990-01-01"},
    )
    assert ok is False
    assert person.status == "requires_input"
    assert person.identity_hash == kyc_svc.identity_hash("Jane", "Smith", "1980-04-02")

    ok = await kyc_svc.apply_person_outcome(
        None,
        person,
        {"status": "verified", "first_name": "Jane", "last_name": "Smith", "dob": "1980-04-02"},
    )
    assert ok is True and person.status == "verified"


# --- 6. rotating a more powerful key ----------------------------------------------------


async def test_admin_cannot_rotate_or_revoke_a_key_with_more_powers(api, session):
    _e, owner_token, org_id = await _owner_org(api, session)
    r = await api.post(
        "/api/v1/api-keys",
        json={"name": "billing", "scopes": ["org:read", "org:billing"]},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 201, r.text
    key_id = r.json()["id"]
    _admin_email, admin_token = await _add_member(api, session, org_id, "admin")
    r = await api.post(
        f"/api/v1/api-keys/{key_id}/rotate", headers=auth_headers(admin_token, org_id)
    )
    assert r.status_code == 422, r.text
    r = await api.post(
        f"/api/v1/api-keys/{key_id}/revoke", headers=auth_headers(admin_token, org_id)
    )
    assert r.status_code == 422, r.text
    row = await session.get(ApiKey, uuid.UUID(key_id))
    assert row.status == "active"


# --- 7. event socket after removal ------------------------------------------------------


async def test_event_socket_closes_when_member_is_removed(api, session, monkeypatch):
    from app.api.routes import softphone
    from app.events.bus import EventBus
    from tests.test_events_ws import FakeWebSocket, _FakeApp

    monkeypatch.setattr(softphone, "ACCESS_TTL_SECONDS", 0)
    monkeypatch.setattr(softphone, "PING_INTERVAL_SECONDS", 0.05)
    _e, _t, org_id = await _owner_org(api, session)
    member_email, member_token = await _add_member(api, session, org_id, "agent")
    app = _FakeApp(make_settings(), EventBus())
    ws = FakeWebSocket(app, token=member_token, org_id=str(org_id))
    task = asyncio.ensure_future(softphone.events_ws(ws))
    try:
        for _ in range(100):
            if ws.accepted:
                break
            await asyncio.sleep(0.01)
        assert ws.accepted and ws.closed_code is None
        user = await _user(session, member_email)
        set_org_context(session, org_id)
        await session.execute(
            sa.delete(OrgMembership).where(
                OrgMembership.org_id == org_id, OrgMembership.user_id == user.id
            )
        )
        await session.commit()
        for _ in range(200):
            if ws.closed_code is not None:
                break
            await asyncio.sleep(0.02)
        assert ws.closed_code == 4401
    finally:
        ws.disconnect()
        await asyncio.wait_for(task, timeout=3)


# --- 8. lockout as a password oracle ----------------------------------------------------


async def test_locked_account_answers_the_same_for_right_and_wrong_password(api, session):
    email = f"lock-{uuid.uuid4().hex[:8]}@example.com"
    await register_and_login(api, email)
    user = await _user(session, email)
    session.add(
        AccountLockout(
            id=uuid.uuid4(),
            user_id=user.id,
            level=1,
            locked_until=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
    )
    await session.commit()
    right = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    wrong = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": "nope-nope-nope"}
    )
    assert right.status_code == wrong.status_code == 423
    # Same wording either way. The countdown minutes are masked: they tick between the two
    # requests, and the point of the test is that the password itself changes nothing.
    def shape(response):
        return re.sub(r"\d+", "N", response.json()["error"]["message"])

    assert shape(right) == shape(wrong)


async def test_login_rate_limit_ignores_email_case(fix_settings, engine, monkeypatch):
    from app.api.routes import auth as auth_routes

    seen: list[str] = []

    async def capture(request, identifier):
        seen.append(identifier)

    monkeypatch.setattr(auth_routes, "enforce_rate_limit", capture)
    application = create_app(fix_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        for variant in ("Victim@Example.com", "VICTIM@example.com"):
            await c.post("/api/v1/auth/login", json={"email": variant, "password": "x" * 12})
    assert seen[0] == seen[1] == "login:victim@example.com"


# --- 9. trust_idp_mfa from another workspace --------------------------------------------


def test_sso_session_from_another_workspace_does_not_satisfy_passkey_rule():
    from app.services.passkey_policy import session_satisfies

    org = Org(id=uuid.uuid4(), name="B", slug="b", trust_idp_mfa=True)
    other = IdentitySession(auth_method="sso", org_id=uuid.uuid4())
    same = IdentitySession(auth_method="sso", org_id=org.id)
    assert session_satisfies(other, org) is False
    assert session_satisfies(same, org) is True


# --- 10. global password-reset bucket ----------------------------------------------------


async def test_password_reset_rate_limit_is_per_token(fix_settings, engine, monkeypatch):
    from app.api.routes import account as account_routes

    seen: list[str] = []

    async def capture(request, identifier):
        seen.append(identifier)

    monkeypatch.setattr(account_routes, "enforce_rate_limit", capture)
    application = create_app(fix_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        for token in ("token-one-" + "x" * 20, "token-two-" + "y" * 20):
            await c.post(
                "/api/v1/auth/password/reset",
                json={"token": token, "new_password": "a-long-new-password"},
            )
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(s.startswith("password-reset:") for s in seen)


# --- 11. TOTP step-up brute force ---------------------------------------------------------


async def test_wrong_step_up_codes_lock_the_account(api, session, fix_settings):
    email = f"stepup-{uuid.uuid4().hex[:8]}@example.com"
    token = await register_and_login(api, email)
    user = await _user(session, email)
    user.totp_enabled = True
    user.totp_secret = encrypt_credential(pyotp.random_base32(), FERNET)
    await session.commit()
    h = {"Authorization": f"Bearer {token}"}
    codes = []
    for _ in range(fix_settings.lockout_threshold + 1):
        r = await api.post("/api/v1/auth/2fa/step-up", json={"code": "000000"}, headers=h)
        codes.append(r.status_code)
    assert codes[-1] == 423, codes


# --- 12. one selfie spent twice ------------------------------------------------------------


async def test_a_selfie_step_up_can_only_be_spent_once(api, session, fix_settings):
    from app.services import kyc_step_up

    email = f"selfie-{uuid.uuid4().hex[:8]}@example.com"
    await register_and_login(api, email)
    user = await _user(session, email)
    _e, _t, org_id = await _owner_org(api, session)
    set_org_context(session, org_id)
    ih = "a" * 64
    session.add(
        KycPerson(
            id=uuid.uuid4(),
            org_id=org_id,
            role="owner",
            full_name="Pat",
            status="verified",
            identity_hash=ih,
            user_id=user.id,
        )
    )
    session.add(
        KycStepUp(
            id=uuid.uuid4(),
            user_id=user.id,
            action="api_key_create",
            status="verified",
            identity_hash=ih,
            verified_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    enforced = make_settings(kyc_enforced=True)
    first = await kyc_step_up.has_fresh_selfie(
        session, enforced, user, action="api_key_create", consume=True
    )
    second = await kyc_step_up.has_fresh_selfie(
        session, enforced, user, action="api_key_create", consume=True
    )
    assert first is True and second is False


# --- 13. operators in the browser ---------------------------------------------------------


async def test_operator_cookie_session_reaches_legacy_ops_routes(engine, session):
    from app.services import operators as operators_svc

    settings = make_settings(auth_bearer_compat=False, session_cookie_secure=False)
    application = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as browser:
        email = f"op-{uuid.uuid4().hex[:8]}@example.com"
        password = "correct-horse-battery-staple"
        r = await browser.post("/api/v1/auth/register", json={"email": email, "password": password})
        assert r.status_code == 201
        r = await browser.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200
        user = await _user(session, email)
        user.totp_enabled = True
        user.has_passkey = True
        await operators_svc.grant(session, email=email, role="admin")
        live = (
            await session.execute(
                sa.select(IdentitySession).where(IdentitySession.user_id == user.id)
            )
        ).scalar_one()
        live.second_factor_at = datetime.now(timezone.utc)
        live.auth_method = "passkey"
        await session.commit()
        r = await browser.get(f"/api/v1/platform/billing/orgs/{uuid.uuid4()}")
        assert r.status_code == 404, r.text  # authenticated; the org just doesn't exist


# --- 4. approval on stale screening -------------------------------------------------------

from tests.test_p41_kyc import (  # noqa: E402 - reuse the P41 verification harness
    _complete_application,
    _make_operator,
    _write_sanctions,
    kyc_app,  # noqa: F401 - fixture
    kyc_settings,  # noqa: F401 - fixture
)


async def test_approval_rescreens_against_current_lists(kyc_app, session, kyc_settings):  # noqa: F811
    client, _app, _carrier, created, outcomes = kyc_app
    _write_sanctions(kyc_settings, ["IVAN BADGUY"])
    token = await register_and_login(client, "jane@acme-plumbing.example")
    org = await create_org(client, token, "Acme")
    h = auth_headers(token, org["id"])
    await _complete_application(client, created, outcomes, token, org["id"])
    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 200, r.text

    oh = auth_headers(await _make_operator(client, session, "reviewer@platform.example"))
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/registry",
        json={"result": "pass", "link": "https://sos.example/acme", "note": "active"},
        headers=oh,
    )
    assert r.status_code == 200, r.text

    # The owner lands on the sanctions list AFTER the application was screened.
    _write_sanctions(kyc_settings, ["IVAN BADGUY", "JANE SMITH"])
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/approve", json={"note": "ok"}, headers=oh
    )
    assert r.status_code == 409, r.text
    assert "sanctions" in r.json()["error"]["message"].lower()
