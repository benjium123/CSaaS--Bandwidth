"""Ringlite apps P2: native push (FCM) registration and fan-out.

Covers the /api/v1/me/device/push-token routes, the FCM HTTP v1 sender (with a mocked
transport) and the device_push fan-out service: token registration/upsert, web-session
refusal, moving a token between sessions, pref and liveness filtering, FCM
UNREGISTERED/RETRY handling, OAuth token caching, logout pushes and the disabled path.

Device calls go from ``app_client`` (no cookies, Bearer only) on the same app instance;
the web user signs in on ``browser`` exactly like tests/test_device_sessions.py.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.models import DevicePushToken, User
from app.services import device_push, fcm
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
def _write_service_account(tmp_path) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "service-account.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "ringlite-test",
                "client_email": "pusher@ringlite-test.iam.gserviceaccount.com",
                "private_key": pem,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        encoding="utf-8",
    )
    return str(path)


class _FakeFcm:
    """MockTransport backing the OAuth + FCM endpoints; records what was sent."""

    def __init__(self) -> None:
        self.token_calls = 0
        self.sends: list[dict] = []
        self.send_status = 200
        self.send_body: dict = {}
        self.token_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "fcm.googleapis.com":
            self.sends.append(json.loads(request.content.decode()))
            return httpx.Response(self.send_status, json=self.send_body, request=request)
        self.token_calls += 1
        return httpx.Response(
            self.token_status,
            json={"access_token": "ya29.test", "expires_in": 3600},
            request=request,
        )


@pytest.fixture
def push_service_account(tmp_path):
    return _write_service_account(tmp_path)


@pytest.fixture
def push_settings(push_service_account):
    return make_settings(
        app_env="test",
        auth_bearer_compat=False,
        session_cookie_secure=False,
        credential_encryption_key=FERNET_KEY,
        device_session_days=14,
        fcm_service_account_file=push_service_account,
    )


@pytest.fixture
async def app(engine, push_settings):
    from app.main import create_app

    return create_app(push_settings)


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


@pytest.fixture
def fake_fcm(monkeypatch):
    fake = _FakeFcm()
    monkeypatch.setattr(fcm, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(fcm, "_token_cache", {})
    yield fake
    fcm._transport = None
    fcm._token_cache.clear()


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
def _csrf(client: httpx.AsyncClient) -> dict:
    return {"X-CSRF-Token": client.cookies.get("csaas_csrf", "")}


async def _register_confirm_and_login(client: httpx.AsyncClient, email: str) -> None:
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


async def _device_login(client: httpx.AsyncClient, email: str) -> dict:
    r = await client.post(
        "/api/v1/auth/device/login",
        json={"email": email, "password": PASSWORD, "device": DEVICE},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _put_token(client, login: dict, token: str, platform: str = "android"):
    return await client.put(
        "/api/v1/me/device/push-token",
        json={"token": token, "platform": platform},
        headers=auth_headers(login["access_token"]),
    )


async def _register_token(client, login: dict, token: str, platform: str = "android") -> None:
    r = await _put_token(client, login, token, platform)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


async def _user(session, email: str) -> User:
    return (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def _rows(session) -> list[DevicePushToken]:
    return (
        await session.execute(sa.select(DevicePushToken))
    ).scalars().all()


# ----------------------------------------------------------------------------------
# 1. registration upserts the token for the device session
# ----------------------------------------------------------------------------------
async def test_register_and_update_token(browser, app_client, session):
    email = "push-reg@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    sid = uuid.UUID(login["session_id"])

    await _register_token(app_client, login, "tok-aaa")

    session.expire_all()
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].session_id == sid
    assert rows[0].token == "tok-aaa"
    assert rows[0].platform == "android"

    # A refreshed FCM token updates the same row instead of adding one.
    await _register_token(app_client, login, "tok-bbb")

    session.expire_all()
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].session_id == sid
    assert rows[0].token == "tok-bbb"


# ----------------------------------------------------------------------------------
# 2. a web cookie session cannot register a native push token
# ----------------------------------------------------------------------------------
async def test_web_session_cannot_register(browser):
    email = "push-web@example.com"
    await _register_confirm_and_login(browser, email)
    r = await browser.put(
        "/api/v1/me/device/push-token",
        json={"token": "tok-web", "platform": "android"},
        headers=_csrf(browser),
    )
    assert r.status_code == 403, r.text
    assert "device_session_required" in r.text


# ----------------------------------------------------------------------------------
# 3. a token re-registered from a second device login moves to the new session
# ----------------------------------------------------------------------------------
async def test_token_moves_to_the_new_session(browser, app_client, session):
    email = "push-move@example.com"
    await _register_confirm_and_login(browser, email)
    login1 = await _device_login(app_client, email)
    login2 = await _device_login(app_client, email)
    assert login1["session_id"] != login2["session_id"]

    await _register_token(app_client, login1, "tok-shared")
    await _register_token(app_client, login2, "tok-shared")

    session.expire_all()
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].session_id == uuid.UUID(login2["session_id"])


# ----------------------------------------------------------------------------------
# 4. DELETE removes this session's row
# ----------------------------------------------------------------------------------
async def test_delete_token(browser, app_client, session):
    email = "push-del@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-del")

    r = await app_client.delete(
        "/api/v1/me/device/push-token", headers=auth_headers(login["access_token"])
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    session.expire_all()
    assert await _rows(session) == []


# ----------------------------------------------------------------------------------
# 5. fan-out sends one request per live token with the right payload
# ----------------------------------------------------------------------------------
async def test_push_to_users_sends_one_request_per_live_token(
    browser, app_client, session, push_settings, fake_fcm
):
    email = "push-send@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-live")
    user_id = uuid.UUID(login["user"]["id"])

    await device_push.push_to_users(
        push_settings,
        [user_id],
        "mention",
        {"ticket_id": "t1"},
        title="You were mentioned",
        body="Ticket t1",
        high_priority=True,
    )

    assert len(fake_fcm.sends) == 1
    message = fake_fcm.sends[0]["message"]
    assert message["token"] == "tok-live"
    assert message["data"]["kind"] == "mention"
    assert message["data"]["ticket_id"] == "t1"
    assert message["android"]["priority"] == "HIGH"
    assert message["android"]["notification"] == {
        "title": "You were mentioned",
        "body": "Ticket t1",
        "channel_id": "mention",
    }

    session.expire_all()
    rows = await _rows(session)
    assert rows[0].last_used_at is not None


# ----------------------------------------------------------------------------------
# 6. a revoked device session is not pushed to
# ----------------------------------------------------------------------------------
async def test_revoked_session_is_not_pushed(
    browser, app_client, session, push_settings, fake_fcm
):
    email = "push-rev@example.com"
    await _register_confirm_and_login(browser, email)
    login1 = await _device_login(app_client, email)
    login2 = await _device_login(app_client, email)
    await _register_token(app_client, login1, "tok-one")
    await _register_token(app_client, login2, "tok-two")

    r = await browser.delete(
        f"/api/v1/me/sessions/{login1['session_id']}", headers=_csrf(browser)
    )
    assert r.status_code == 204, r.text

    await device_push.push_to_users(
        push_settings,
        [uuid.UUID(login1["user"]["id"])],
        "incoming_call",
        {"call_id": "c1"},
    )

    assert {m["message"]["token"] for m in fake_fcm.sends} == {"tok-two"}
    # incoming_call is data-only: no Android notification block.
    assert "notification" not in fake_fcm.sends[0]["message"]["android"]


# ----------------------------------------------------------------------------------
# 7. preferences gate mention but never incoming_call
# ----------------------------------------------------------------------------------
async def test_prefs_suppress_mention_but_not_incoming_call(
    browser, app_client, session, push_settings, fake_fcm
):
    email = "push-prefs@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-pref")
    user_id = uuid.UUID(login["user"]["id"])

    session.expire_all()
    user = await _user(session, email)
    user.notification_prefs = {"mention": False}
    await session.commit()

    await device_push.push_to_users(push_settings, [user_id], "mention", {"ticket_id": "t9"})
    assert fake_fcm.sends == []

    await device_push.push_to_users(push_settings, [user_id], "incoming_call", {"call_id": "c9"})
    assert len(fake_fcm.sends) == 1
    assert fake_fcm.sends[0]["message"]["data"]["kind"] == "incoming_call"


# ----------------------------------------------------------------------------------
# 8. UNREGISTERED (404) prunes the row; 503 (RETRY) keeps it
# ----------------------------------------------------------------------------------
async def test_unregistered_token_is_deleted(
    browser, app_client, session, push_settings, fake_fcm
):
    email = "push-unreg@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-dead")
    user_id = uuid.UUID(login["user"]["id"])

    fake_fcm.send_status = 404
    fake_fcm.send_body = {"error": {"status": "NOT_FOUND"}}
    await device_push.push_to_users(push_settings, [user_id], "new_inbound", {"a": "1"})

    session.expire_all()
    assert await _rows(session) == []


async def test_transient_error_keeps_token(
    browser, app_client, session, push_settings, fake_fcm
):
    email = "push-retry@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-flaky")
    user_id = uuid.UUID(login["user"]["id"])

    fake_fcm.send_status = 503
    await device_push.push_to_users(push_settings, [user_id], "new_inbound", {"a": "1"})

    session.expire_all()
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].token == "tok-flaky"
    assert rows[0].last_used_at is None


# ----------------------------------------------------------------------------------
# 9. the OAuth access token is fetched once across sends
# ----------------------------------------------------------------------------------
async def test_oauth_token_is_cached(browser, app_client, session, push_settings, fake_fcm):
    email = "push-cache@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-cache")
    user_id = uuid.UUID(login["user"]["id"])

    await device_push.push_to_users(push_settings, [user_id], "new_inbound", {"a": "1"})
    await device_push.push_to_users(push_settings, [user_id], "new_inbound", {"a": "2"})

    assert fake_fcm.token_calls == 1
    assert len(fake_fcm.sends) == 2


# ----------------------------------------------------------------------------------
# 10. logout sends a data-only logout then deletes the row
# ----------------------------------------------------------------------------------
async def test_push_logout_sends_then_deletes(
    browser, app_client, session, push_settings, fake_fcm
):
    email = "push-logout@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-bye")
    sid = uuid.UUID(login["session_id"])

    await device_push.push_logout(push_settings, sid)

    assert len(fake_fcm.sends) == 1
    message = fake_fcm.sends[0]["message"]
    assert message["data"] == {"kind": "logout"}
    assert message["android"]["priority"] == "HIGH"
    assert "notification" not in message["android"]

    session.expire_all()
    assert await _rows(session) == []


# ----------------------------------------------------------------------------------
# 11. FCM disabled (empty setting) makes no HTTP calls at all
# ----------------------------------------------------------------------------------
async def test_disabled_fcm_makes_no_calls(browser, app_client, fake_fcm):
    email = "push-off@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-off")
    user_id = uuid.UUID(login["user"]["id"])

    disabled = make_settings(fcm_service_account_file="")
    await device_push.push_to_users(disabled, [user_id], "new_inbound", {"a": "1"})
    await device_push.push_logout(disabled, uuid.UUID(login["session_id"]))

    assert fake_fcm.token_calls == 0
    assert fake_fcm.sends == []


# ----------------------------------------------------------------------------------
# 12. schedule() returns an awaitable task under app_env == "test"
# ----------------------------------------------------------------------------------
async def test_scheduled_push_runs(browser, app_client, session, push_settings, fake_fcm):
    email = "push-sched@example.com"
    await _register_confirm_and_login(browser, email)
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-sched")
    user_id = uuid.UUID(login["user"]["id"])

    task = device_push.schedule(
        push_settings,
        device_push.push_to_users(push_settings, [user_id], "new_inbound", {"a": "1"}),
    )
    assert task is not None
    await task

    assert len(fake_fcm.sends) == 1
    assert fake_fcm.sends[0]["message"]["data"]["kind"] == "new_inbound"
