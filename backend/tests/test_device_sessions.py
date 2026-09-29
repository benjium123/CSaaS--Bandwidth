"""Ringlite apps P1: device sessions.

Covers sign-in for the Android/desktop apps (device_auth.py + device_sessions.py + the
device branch of login_flow.complete_login / auth/deps._authenticate_device): device
login with and without a second factor, Bearer access tokens, refresh rotation/reuse/
expiry, revocation from the web console and by member removal, device logout, and QR
device linking.

These are cookie-less device calls, so every device request is made from an httpx client
on a SEPARATE app instance (``app_client``) that never carries the browser's session
cookie; the web user signs in on ``browser`` exactly like test_p42_sessions.py does.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pyotp
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.auth.security import ALGORITHM
from app.models import DeviceLinkCode, User
from app.models import Session as IdentitySession
from app.services import device_sessions
from tests.conftest import auth_headers, latest_email_code, make_settings

PASSWORD = "correct-horse-battery-staple"
FERNET_KEY = Fernet.generate_key().decode()

DEVICE = {
    "kind": "android",
    "name": "Pixel 8",
    "os": "Android 15",
    "app_version": "0.1.0 (1)",
}


# ----------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def device_settings():
    return make_settings(
        auth_bearer_compat=False,
        session_cookie_secure=False,
        credential_encryption_key=FERNET_KEY,
        # Pin the hard device lifetime so the ~14 day assertion is deterministic.
        device_session_days=14,
    )


@pytest.fixture
async def app(engine, device_settings):
    from app.main import create_app

    return create_app(device_settings)


@pytest.fixture
async def browser(app):
    """Cookie-based web client (the Ringlite console)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def app_client(app):
    """A second client on the SAME app with NO cookies: device calls carry only a Bearer."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
def _csrf(client: httpx.AsyncClient) -> dict:
    return {"X-CSRF-Token": client.cookies.get("csaas_csrf", "")}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _session_items(body):
    """``/api/v1/me/sessions`` bodies are either a bare list or wrapped in a key."""
    if isinstance(body, dict):
        for key in ("sessions", "items", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return value
        return []
    return body or []


def _current_session(body):
    for item in _session_items(body):
        if item.get("current"):
            return item
    return None


async def _device_login(client: httpx.AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/api/v1/auth/device/login",
        json={"email": email, "password": password, "device": DEVICE},
    )


async def _device_refresh(client: httpx.AsyncClient, refresh_token: str):
    return await client.post(
        "/api/v1/auth/device/refresh", json={"refresh_token": refresh_token}
    )


async def _register_confirm_and_login(client: httpx.AsyncClient, email: str) -> None:
    """Register + confirm the address the way a browser does, then sign in once.

    ``auth_bearer_compat=False`` means conftest's ``confirm_registered_email`` (which
    needs a bearer token) cannot be used, so this mirrors test_p42_sessions._login:
    register, log in to get a cookie, confirm with CSRF, log out, log back in.
    """
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": email.split("@")[0]},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    csrf = _csrf(client)
    r = await client.post(
        "/api/v1/auth/confirm-email", json={"code": latest_email_code(email)}, headers=csrf
    )
    assert r.status_code == 200, r.text
    await client.post("/api/v1/auth/logout", headers=csrf)
    client.cookies.clear()
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert "csaas_session" in client.cookies


async def _enroll_and_activate(client: httpx.AsyncClient, password: str = PASSWORD) -> str:
    """Enrol TOTP using the cookie session (compat is off, so no bearer here)."""
    r = await client.post(
        "/api/v1/auth/2fa/enroll", json={"password": password}, headers=_csrf(client)
    )
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    code = pyotp.TOTP(secret).now()
    r = await client.post(
        "/api/v1/auth/2fa/activate", json={"code": code}, headers=_csrf(client)
    )
    assert r.status_code == 200, r.text
    assert r.json()["totp_enabled"] is True
    return secret


def _next_totp_step(monkeypatch) -> None:
    """Jump to the next 30 s TOTP step so a freshly-generated code is not a replay of
    the activation code (same trick as tests/test_twofa.py)."""
    before = time.time
    offset = 31 - (int(before()) % 30)
    monkeypatch.setattr(time, "time", lambda: before() + offset)


async def _user(session, email: str) -> User:
    return (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def _web_row(session, email: str) -> IdentitySession:
    user = await _user(session, email)
    return (
        await session.execute(
            sa.select(IdentitySession)
            .where(
                IdentitySession.user_id == user.id,
                IdentitySession.token_hash.isnot(None),
                IdentitySession.revoked_at.is_(None),
            )
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()


async def _device_row(session) -> IdentitySession:
    return (
        await session.execute(
            sa.select(IdentitySession)
            .where(IdentitySession.device_kind.isnot(None))
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()


# ----------------------------------------------------------------------------------
# 1. device login without a second factor
# ----------------------------------------------------------------------------------
async def test_device_login_without_2fa(browser, app_client, session):
    email = "dev-login@example.com"
    await _register_confirm_and_login(browser, email)

    r = await _device_login(app_client, email)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["refresh_token"].startswith("rt1.")
    assert body["session_expires_at"]
    assert body["session_id"]
    assert body["user"]["email"] == email
    # A device login must never hand back a browser session cookie.
    assert "csaas_session" not in app_client.cookies

    session.expire_all()
    row = await _device_row(session)
    assert row.device_kind == "android"
    assert row.device_name == "Pixel 8"
    assert row.token_hash is None
    delta = (_aware(row.expires_at) - _aware(row.created_at)).total_seconds()
    assert abs(delta - 14 * 86400) < 300


# ----------------------------------------------------------------------------------
# 2. the Bearer access token works and reports the device
# ----------------------------------------------------------------------------------
async def test_device_access_token_lists_sessions(browser, app_client, session):
    email = "dev-access@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    r = await app_client.get(
        "/api/v1/me/sessions", headers=auth_headers(login["access_token"])
    )
    assert r.status_code == 200, r.text
    current = _current_session(r.json())
    assert current is not None
    assert current["device_kind"] == "android"
    assert current["device_name"] == "Pixel 8"


# ----------------------------------------------------------------------------------
# 3. with compat off a WEB session id is still not a valid Bearer
# ----------------------------------------------------------------------------------
async def test_web_session_bearer_refused(
    browser, app_client, session, device_settings
):
    email = "dev-webbearer@example.com"
    await _register_confirm_and_login(browser, email)

    session.expire_all()
    web_row = await _web_row(session, email)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(web_row.user_id),
        "sid": str(web_row.id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    token = jwt.encode(
        payload, device_settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM
    )

    r = await app_client.get("/api/v1/me/sessions", headers=auth_headers(token))
    assert r.status_code == 401, r.text


# ----------------------------------------------------------------------------------
# 4. refresh rotates the secret but keeps the session expiry
# ----------------------------------------------------------------------------------
async def test_refresh_rotates_and_keeps_expiry(browser, app_client, session):
    email = "dev-refresh@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    r = await _device_refresh(app_client, login["refresh_token"])
    assert r.status_code == 200, r.text
    rotated = r.json()
    assert rotated["refresh_token"] != login["refresh_token"]
    assert rotated["refresh_token"].startswith("rt1.")
    assert rotated["session_expires_at"] == login["session_expires_at"]

    me = await app_client.get(
        "/api/v1/me/sessions", headers=auth_headers(rotated["access_token"])
    )
    assert me.status_code == 200, me.text


# ----------------------------------------------------------------------------------
# 5. reusing a rotated refresh token kills the whole session
# ----------------------------------------------------------------------------------
async def test_refresh_reuse_revokes(browser, app_client, session):
    """A rotated refresh token replayed AFTER the race grace window means it leaked."""
    email = "dev-reuse@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()
    token_a = login["refresh_token"]

    r = await _device_refresh(app_client, token_a)
    assert r.status_code == 200, r.text
    token_b = r.json()["refresh_token"]
    access_b = r.json()["access_token"]

    session.expire_all()
    row = await _device_row(session)
    row.refresh_rotated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await session.commit()

    replay = await _device_refresh(app_client, token_a)
    assert replay.status_code == 401, replay.text
    assert "refresh_invalid" in replay.text

    after = await _device_refresh(app_client, token_b)
    assert after.status_code == 401, after.text
    assert "refresh_invalid" in after.text

    me = await app_client.get("/api/v1/me/sessions", headers=auth_headers(access_b))
    assert me.status_code == 401, me.text


async def test_refresh_race_inside_grace_is_not_theft(browser, app_client, session):
    email = "dev-race@example.com"
    await _register_confirm_and_login(browser, email)
    token_a = (await _device_login(app_client, email)).json()["refresh_token"]

    first = await _device_refresh(app_client, token_a)
    assert first.status_code == 200, first.text

    racer = await _device_refresh(app_client, token_a)
    assert racer.status_code == 409, racer.text
    assert "refresh_in_progress" in racer.text

    # The winner's tokens still work.
    again = await _device_refresh(app_client, first.json()["refresh_token"])
    assert again.status_code == 200, again.text


async def test_unknown_refresh_secret_revokes_nothing(browser, app_client, session):
    email = "dev-forged@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    forged = f"rt1.{login['session_id']}.not-the-secret"
    r = await _device_refresh(app_client, forged)
    assert r.status_code == 401, r.text

    ok = await _device_refresh(app_client, login["refresh_token"])
    assert ok.status_code == 200, ok.text


async def test_linked_device_gets_no_fresh_step_up(browser, app_client, session):
    email = "dev-link-stepup@example.com"
    await _register_confirm_and_login(browser, email)
    await _enroll_and_activate(browser)
    session.expire_all()
    web = await _web_row(session, email)
    web.second_factor_at = datetime.now(timezone.utc)
    await session.commit()

    code = (await browser.post("/api/v1/auth/device/link-codes", headers=_csrf(browser))).json()
    r = await app_client.post(
        "/api/v1/auth/device/link", json={"code": code["code"], "device": DEVICE}
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    row = await _device_row(session)
    assert row.second_factor_at is not None
    assert datetime.now(timezone.utc) - _aware(row.second_factor_at) > timedelta(minutes=10)


async def test_link_qr_host_comes_from_config(browser, session):
    email = "dev-link-host@example.com"
    await _register_confirm_and_login(browser, email)
    r = await browser.post(
        "/api/v1/auth/device/link-codes",
        headers={**_csrf(browser), "Host": "evil.example"},
    )
    assert r.status_code == 200, r.text
    assert "evil.example" not in r.json()["qr_payload"]


# ----------------------------------------------------------------------------------
# 6. refresh cannot extend a session past its hard expiry
# ----------------------------------------------------------------------------------
async def test_refresh_cannot_extend(browser, app_client, session):
    email = "dev-expired@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    session.expire_all()
    row = await session.get(IdentitySession, uuid.UUID(login["session_id"]))
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    r = await _device_refresh(app_client, login["refresh_token"])
    assert r.status_code == 401, r.text
    assert "refresh_invalid" in r.text

    me = await app_client.get(
        "/api/v1/me/sessions", headers=auth_headers(login["access_token"])
    )
    assert me.status_code == 401, me.text


# ----------------------------------------------------------------------------------
# 7. an access token's exp never passes the session expiry
# ----------------------------------------------------------------------------------
async def test_access_token_exp_capped_by_session_expiry(
    browser, app_client, session, device_settings
):
    email = "dev-cap@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()
    sid = uuid.UUID(login["session_id"])

    session.expire_all()
    row = await session.get(IdentitySession, sid)
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    await session.commit()
    session.expire_all()

    row = await session.get(IdentitySession, sid)
    user = await session.get(User, row.user_id)
    pair = device_sessions.token_pair(row, user, device_settings, "x")
    payload = jwt.decode(
        pair["access_token"],
        device_settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
    )
    assert payload["exp"] <= int(_aware(row.expires_at).timestamp())


# ----------------------------------------------------------------------------------
# 8. revoking a device from the web console signs the device out
# ----------------------------------------------------------------------------------
async def test_web_revoke_signs_device_out(browser, app_client, session):
    email = "dev-revoke@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    r = await browser.delete(
        f"/api/v1/me/sessions/{login['session_id']}", headers=_csrf(browser)
    )
    assert r.status_code == 204, r.text

    me = await app_client.get(
        "/api/v1/me/sessions", headers=auth_headers(login["access_token"])
    )
    assert me.status_code == 401, me.text
    refresh = await _device_refresh(app_client, login["refresh_token"])
    assert refresh.status_code == 401, refresh.text


# ----------------------------------------------------------------------------------
# 9. removing a member revokes their device sessions
# ----------------------------------------------------------------------------------
async def test_member_removal_revokes_device(browser, app_client, session, paid_seats):
    owner = "owner@dev9.example"
    await _register_confirm_and_login(browser, owner)
    r = await browser.post(
        "/api/v1/orgs", json={"name": "Dev9 Co"}, headers=_csrf(browser)
    )
    assert r.status_code == 201, r.text
    org_id = r.json()["id"]

    member_email = "member@dev9.example"
    member_password = "violet-harbor-lantern-42"
    r = await browser.post(
        "/api/v1/orgs/current/members",
        json={
            "email": member_email,
            "full_name": "Member Nine",
            "password": member_password,
            "role_name": "agent",
        },
        headers={"X-Org-Id": org_id, **_csrf(browser)},
    )
    assert r.status_code in (200, 201), r.text

    session.expire_all()
    member = await _user(session, member_email)
    member_id = member.id

    login = await _device_login(app_client, member_email, member_password)
    assert login.status_code == 200, login.text
    access = login.json()["access_token"]

    before = await app_client.get("/api/v1/me/sessions", headers=auth_headers(access))
    assert before.status_code == 200, before.text

    r = await browser.delete(
        f"/api/v1/orgs/current/members/{member_id}",
        headers={"X-Org-Id": org_id, **_csrf(browser)},
    )
    assert r.status_code == 204, r.text

    after = await app_client.get("/api/v1/me/sessions", headers=auth_headers(access))
    assert after.status_code == 401, after.text


# ----------------------------------------------------------------------------------
# 10. device logout revokes the session
# ----------------------------------------------------------------------------------
async def test_device_logout(browser, app_client, session):
    email = "dev-logout@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    r = await app_client.post(
        "/api/v1/auth/device/logout", headers=auth_headers(login["access_token"])
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    me = await app_client.get(
        "/api/v1/me/sessions", headers=auth_headers(login["access_token"])
    )
    assert me.status_code == 401, me.text
    refresh = await _device_refresh(app_client, login["refresh_token"])
    assert refresh.status_code == 401, refresh.text


# ----------------------------------------------------------------------------------
# 11. a TOTP user must finish the second factor before getting device tokens
# ----------------------------------------------------------------------------------
async def test_device_login_with_2fa(browser, app_client, session, monkeypatch):
    email = "dev-2fa@example.com"
    await _register_confirm_and_login(browser, email)
    secret = await _enroll_and_activate(browser)

    r = await _device_login(app_client, email)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_2fa"] is True
    assert body["pending_token"]
    assert not body.get("access_token")

    _next_totp_step(monkeypatch)
    code = pyotp.TOTP(secret).at(int(time.time()))
    r = await app_client.post(
        "/api/v1/auth/device/2fa/verify",
        json={"pending_token": body["pending_token"], "code": code, "device": DEVICE},
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["access_token"]
    assert tokens["refresh_token"].startswith("rt1.")

    session.expire_all()
    row = await _device_row(session)
    assert row.second_factor_at is not None


# ----------------------------------------------------------------------------------
# 12. QR link code: happy path
# ----------------------------------------------------------------------------------
async def test_link_code_happy_path(browser, app_client, session):
    email = "dev-link@example.com"
    await _register_confirm_and_login(browser, email)

    r = await browser.post("/api/v1/auth/device/link-codes", headers=_csrf(browser))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"]
    assert body["qr_payload"].startswith("ringlite://link?c=")
    assert body["expires_at"]

    r = await app_client.post(
        "/api/v1/auth/device/link", json={"code": body["code"], "device": DEVICE}
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["user"]["email"] == email
    assert tokens["refresh_token"].startswith("rt1.")

    session.expire_all()
    row = await _device_row(session)
    assert row.auth_method == "device_link"


# ----------------------------------------------------------------------------------
# 13. a link code can only be redeemed once
# ----------------------------------------------------------------------------------
async def test_link_code_is_single_use(browser, app_client, session):
    email = "dev-link2@example.com"
    await _register_confirm_and_login(browser, email)
    r = await browser.post("/api/v1/auth/device/link-codes", headers=_csrf(browser))
    code = r.json()["code"]

    first = await app_client.post(
        "/api/v1/auth/device/link", json={"code": code, "device": DEVICE}
    )
    assert first.status_code == 200, first.text

    second = await app_client.post(
        "/api/v1/auth/device/link", json={"code": code, "device": DEVICE}
    )
    assert second.status_code in (400, 422), second.text
    assert "link_code_invalid" in second.text


# ----------------------------------------------------------------------------------
# 14. an expired link code is refused
# ----------------------------------------------------------------------------------
async def test_link_code_expires(browser, app_client, session):
    email = "dev-link3@example.com"
    await _register_confirm_and_login(browser, email)
    r = await browser.post("/api/v1/auth/device/link-codes", headers=_csrf(browser))
    code = r.json()["code"]

    session.expire_all()
    link = (await session.execute(sa.select(DeviceLinkCode))).scalar_one()
    link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()

    r = await app_client.post(
        "/api/v1/auth/device/link", json={"code": code, "device": DEVICE}
    )
    assert r.status_code in (400, 422), r.text
    assert "link_code_invalid" in r.text


# ----------------------------------------------------------------------------------
# 15. link codes can only be minted from a web (cookie) session
# ----------------------------------------------------------------------------------
async def test_link_codes_need_a_web_session(browser, app_client, session):
    email = "dev-link4@example.com"
    await _register_confirm_and_login(browser, email)
    login = (await _device_login(app_client, email)).json()

    r = await app_client.post(
        "/api/v1/auth/device/link-codes", headers=auth_headers(login["access_token"])
    )
    assert r.status_code == 403, r.text
    assert "web_session_required" in r.text


# ----------------------------------------------------------------------------------
# 16. link codes honour the account's second factor
# ----------------------------------------------------------------------------------
async def test_link_codes_require_second_factor(browser, app_client, session):
    email = "dev-link2fa@example.com"
    await _register_confirm_and_login(browser, email)
    await _enroll_and_activate(browser)

    # Pretend the web session predates the second factor: linking must be refused.
    session.expire_all()
    row = await _web_row(session, email)
    row.second_factor_at = None
    await session.commit()

    r = await browser.post("/api/v1/auth/device/link-codes", headers=_csrf(browser))
    assert r.status_code == 403, r.text
    assert "two_factor_required" in r.text


# ----------------------------------------------------------------------------------
# 17. the min-supported-app header only appears for a recognised app header
# ----------------------------------------------------------------------------------
async def test_min_app_header_version_gate(engine):
    from app.main import create_app

    settings = make_settings(app_min_android_code=5)
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/app/version", headers={"X-Ringlite-App": "android/1"})
        assert r.status_code == 200, r.text
        assert r.headers.get("X-Ringlite-Min-App") == "5"
        assert r.json()["android"]["min_supported_code"] == 5

        bare = await c.get("/api/v1/app/version")
        assert bare.status_code == 200, bare.text
        assert "x-ringlite-min-app" not in {k.lower() for k in bare.headers.keys()}


# ----------------------------------------------------------------------------------
# 18. plain web cookie login is untouched by the device feature
# ----------------------------------------------------------------------------------
async def test_web_login_unchanged(browser, session):
    email = "dev-web@example.com"
    await _register_confirm_and_login(browser, email)
    assert "csaas_session" in browser.cookies

    r = await browser.get("/api/v1/me/sessions")
    assert r.status_code == 200, r.text
    current = _current_session(r.json())
    assert current is not None
    assert current["device_kind"] == "web"
