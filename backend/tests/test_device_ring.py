"""Ringlite apps P3: phone-app ringing driven by the EventBus tap in services/device_ring.

``device_ring.make_tap`` turns ``call.ring`` / ``call.handoff.claimed`` / ``call.status``
events into FCM data pushes for exactly the members the web console would ring
(``softphone._event_visible``). These tests publish onto ``app.state.event_bus`` and await
the fire-and-forget tasks the tap schedules in ``device_push._pending``, reusing the FCM
mock, device-login and push-token patterns of tests/test_device_push.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.models import DevicePushToken, User
from app.services import call_prefs, device_push, device_ring, fcm
from tests.conftest import auth_headers, make_org_with_number, make_settings

PASSWORD = "correct-horse-battery"
AGENT_PASSWORD = "violet-harbor-lantern-42"
NUM = "+15125550100"
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

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "fcm.googleapis.com":
            self.sends.append(json.loads(request.content.decode()))
            return httpx.Response(self.send_status, json=self.send_body, request=request)
        self.token_calls += 1
        return httpx.Response(
            200, json={"access_token": "ya29.test", "expires_in": 3600}, request=request
        )


@pytest.fixture
def push_service_account(tmp_path):
    return _write_service_account(tmp_path)


@pytest.fixture
def push_settings(push_service_account):
    # auth_bearer_compat stays at the make_settings default (True) so the cookie helpers in
    # conftest (register_and_login / create_org / make_org_with_number) can act as the owner.
    return make_settings(
        app_env="test",
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
    """A second client on the SAME app with NO cookies: device calls carry a Bearer only."""
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


@pytest.fixture(autouse=True)
def _clean_ring_state():
    """Every test starts with no tracked rings and no cached DND verdicts."""
    device_ring.reset()
    call_prefs._cache.clear()
    yield
    device_ring.reset()
    call_prefs._cache.clear()


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
def _csrf(client: httpx.AsyncClient) -> dict:
    return {"X-CSRF-Token": client.cookies.get("csaas_csrf", "")}


async def _device_login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> dict:
    r = await client.post(
        "/api/v1/auth/device/login",
        json={"email": email, "password": password, "device": DEVICE},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _register_token(client, login: dict, token: str, platform: str = "android") -> None:
    r = await client.put(
        "/api/v1/me/device/push-token",
        json={"token": token, "platform": platform},
        headers=auth_headers(login["access_token"]),
    )
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
    return (await session.execute(sa.select(DevicePushToken))).scalars().all()


async def _drain_pushes() -> None:
    """Await every fire-and-forget task the tap scheduled, until none remain."""
    for _ in range(100):
        pending = list(device_push._pending)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def _publish(application, org_id: uuid.UUID, event: dict) -> None:
    application.state.event_bus.publish(org_id, event)
    await _drain_pushes()


async def _add_agent(browser: httpx.AsyncClient, org_id: uuid.UUID, email: str) -> None:
    r = await browser.post(
        "/api/v1/orgs/current/members",
        json={
            "email": email,
            "full_name": email.split("@")[0],
            "password": AGENT_PASSWORD,
            "role_name": "agent",
        },
        headers={**_csrf(browser), "X-Org-Id": str(org_id)},
    )
    assert r.status_code in (200, 201), r.text


async def _grant_member_access(session, org_id: uuid.UUID, e164: str, user_id: uuid.UUID) -> None:
    """Give a member access to a number by placing them on the number's inbox."""
    from app.models import Inbox, InboxGrant, OrgNumber
    from app.db.base import set_org_context

    set_org_context(session, org_id)

    number = (
        await session.execute(
            sa.select(OrgNumber)
            .where(OrgNumber.org_id == org_id, OrgNumber.e164 == e164)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    inbox_id = (
        await session.execute(
            sa.select(Inbox.id)
            .where(Inbox.number_id == number.id)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox_id,
            grantee_type="user",
            grantee_id=user_id,
            role="member",
        )
    )
    await session.commit()


async def _setup_owner(browser, app_client, email: str, org_name: str, e164: str):
    """Owner web session (cookies) + org + number + a device signed in as tok-owner."""
    _token, org, _number = await make_org_with_number(browser, email, org_name, e164)
    org_id = uuid.UUID(org["id"])
    login = await _device_login(app_client, email)
    await _register_token(app_client, login, "tok-owner")
    return org_id, login


async def _setup_agent(browser, app_client, session, org_id, email, e164=None):
    """Add an agent; optionally grant number access; sign them in as tok-agent."""
    await _add_agent(browser, org_id, email)
    agent = await _user(session, email)
    if e164 is not None:
        await _grant_member_access(session, org_id, e164, agent.id)
    login = await _device_login(app_client, email, AGENT_PASSWORD)
    await _register_token(app_client, login, "tok-agent")
    return agent.id


# ----------------------------------------------------------------------------------
# 1. call.ring pushes a high-priority, data-only incoming_call to the owner
# ----------------------------------------------------------------------------------
async def test_call_ring_pushes_incoming_call_to_owner(browser, app_client, app, fake_fcm):
    org_id, _login = await _setup_owner(
        browser, app_client, "ring-owner@example.com", "Ring One", NUM
    )

    call_id = str(uuid.uuid4())
    await _publish(
        app,
        org_id,
        {
            "type": "call.ring",
            "call_id": call_id,
            "from": "+15550001111",
            "to": NUM,
            "room": "r1",
        },
    )

    assert len(fake_fcm.sends) == 1
    message = fake_fcm.sends[0]["message"]
    assert message["token"] == "tok-owner"
    assert message["data"]["kind"] == "incoming_call"
    assert message["data"]["call_id"] == call_id
    assert message["data"]["from"] == "+15550001111"
    assert message["data"]["to"] == NUM
    assert message["data"]["room"] == "r1"
    assert message["android"]["priority"] == "HIGH"
    # Ringing is driven from the data payload: never an Android notification block.
    assert "notification" not in message["android"]


# ----------------------------------------------------------------------------------
# 2. ring_user_ids restricts the ring to the members the console's gate yields
# ----------------------------------------------------------------------------------
async def test_ring_user_ids_rings_only_the_gates_recipients(
    browser, app_client, app, session, fake_fcm
):
    org_email = "ring-owner-2@example.com"
    org_id, _login = await _setup_owner(browser, app_client, org_email, "Ring Two", NUM)
    owner = await _user(session, org_email)
    agent_id = await _setup_agent(
        browser, app_client, session, org_id, "ring-agent-2@example.com", e164=NUM
    )

    call_id = str(uuid.uuid4())
    event = {
        "type": "call.ring",
        "call_id": call_id,
        "from": "+15550001111",
        "to": NUM,
        "room": "r1",
        "ring_user_ids": [str(agent_id)],
    }
    # device_ring can only reach the members softphone._event_visible approves - mirror it so
    # the assertion tracks the gate (an admin owner may still be approved by it).
    recipients = await device_ring.ring_recipients(org_id, event)
    assert agent_id in recipients

    await _publish(app, org_id, event)

    tokens = {m["message"]["token"] for m in fake_fcm.sends}
    expected = {"tok-agent"}
    if owner.id in recipients:  # documented: admins bypass the ring_user_ids restriction
        expected.add("tok-owner")
    assert tokens == expected


# ----------------------------------------------------------------------------------
# 3. call.handoff.claimed cancels every rung device once, with reason answered_elsewhere
# ----------------------------------------------------------------------------------
async def test_handoff_claimed_cancels_rung_devices_once(browser, app_client, app, fake_fcm):
    org_id, _login = await _setup_owner(
        browser, app_client, "ring-owner-3@example.com", "Ring Three", NUM
    )

    call_id = str(uuid.uuid4())
    await _publish(
        app,
        org_id,
        {
            "type": "call.ring",
            "call_id": call_id,
            "from": "+15550001111",
            "to": NUM,
            "room": "r1",
        },
    )
    assert len(fake_fcm.sends) == 1
    fake_fcm.sends.clear()

    await _publish(app, org_id, {"type": "call.handoff.claimed", "call_id": call_id})

    assert len(fake_fcm.sends) == 1
    message = fake_fcm.sends[0]["message"]
    assert message["token"] == "tok-owner"
    assert message["data"]["kind"] == "call_cancel"
    assert message["data"]["call_id"] == call_id
    assert message["data"]["reason"] == "answered_elsewhere"
    assert message["android"]["priority"] == "HIGH"
    assert "notification" not in message["android"]

    # A second claim for the same call has nothing left to cancel.
    fake_fcm.sends.clear()
    await _publish(app, org_id, {"type": "call.handoff.claimed", "call_id": call_id})
    assert fake_fcm.sends == []


# ----------------------------------------------------------------------------------
# 4. call.status: still-ringing is a no-op, completed cancels with its reason
# ----------------------------------------------------------------------------------
async def test_call_status_ringing_then_completed(browser, app_client, app, fake_fcm):
    org_id, _login = await _setup_owner(
        browser, app_client, "ring-owner-4@example.com", "Ring Four", NUM
    )

    call_id = str(uuid.uuid4())
    await _publish(
        app,
        org_id,
        {
            "type": "call.ring",
            "call_id": call_id,
            "from": "+15550001111",
            "to": NUM,
            "room": "r1",
        },
    )
    fake_fcm.sends.clear()

    await _publish(app, org_id, {"type": "call.status", "call_id": call_id, "status": "ringing"})
    assert fake_fcm.sends == []

    await _publish(
        app, org_id, {"type": "call.status", "call_id": call_id, "status": "completed"}
    )
    assert len(fake_fcm.sends) == 1
    message = fake_fcm.sends[0]["message"]
    assert message["token"] == "tok-owner"
    assert message["data"]["kind"] == "call_cancel"
    assert message["data"]["call_id"] == call_id
    assert message["data"]["reason"] == "completed"


# ----------------------------------------------------------------------------------
# 5. a member with no access to the number is never pushed
# ----------------------------------------------------------------------------------
async def test_agent_without_number_access_is_not_rung(
    browser, app_client, app, session, fake_fcm
):
    org_id, _login = await _setup_owner(
        browser, app_client, "ring-owner-5@example.com", "Ring Five", NUM
    )
    # The agent is an org member but has no inbox grant for NUM.
    await _setup_agent(browser, app_client, session, org_id, "ring-agent-5@example.com")

    call_id = str(uuid.uuid4())
    await _publish(
        app,
        org_id,
        {
            "type": "call.ring",
            "call_id": call_id,
            "from": "+15550001111",
            "to": NUM,
            "room": "r1",
        },
    )

    tokens = {m["message"]["token"] for m in fake_fcm.sends}
    assert "tok-agent" not in tokens
    assert "tok-owner" in tokens


# ----------------------------------------------------------------------------------
# 6. a DND member with access is silenced
# ----------------------------------------------------------------------------------
async def test_dnd_agent_is_not_rung(
    browser, app_client, app, session, fake_fcm, monkeypatch
):
    org_id, _login = await _setup_owner(
        browser, app_client, "ring-owner-6@example.com", "Ring Six", NUM
    )
    agent_id = await _setup_agent(
        browser, app_client, session, org_id, "ring-agent-6@example.com", e164=NUM
    )

    async def _dnd(org_id_arg):
        return {str(agent_id): None}

    # the console's gate reads DND through this cached lookup (same seam test_call_prefs uses)
    monkeypatch.setattr(call_prefs, "dnd_users_cached", _dnd)

    call_id = str(uuid.uuid4())
    await _publish(
        app,
        org_id,
        {
            "type": "call.ring",
            "call_id": call_id,
            "from": "+15550001111",
            "to": NUM,
            "room": "r1",
        },
    )

    tokens = {m["message"]["token"] for m in fake_fcm.sends}
    assert "tok-agent" not in tokens
    assert "tok-owner" in tokens


# ----------------------------------------------------------------------------------
# 7. FCM disabled: publishing a ring makes no HTTP call at all
# ----------------------------------------------------------------------------------
async def test_disabled_fcm_makes_no_http_calls(engine, fake_fcm):
    from app.main import create_app

    settings = make_settings(
        app_env="test",
        session_cookie_secure=False,
        credential_encryption_key=FERNET_KEY,
        device_session_days=14,
        fcm_service_account_file="",
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as browser:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as app_client:
            org_id, _login = await _setup_owner(
                browser, app_client, "ring-off@example.com", "Ring Off", NUM
            )
            call_id = str(uuid.uuid4())
            await _publish(
                application,
                org_id,
                {
                    "type": "call.ring",
                    "call_id": call_id,
                    "from": "+15550001111",
                    "to": NUM,
                    "room": "r1",
                },
            )

    assert fake_fcm.token_calls == 0
    assert fake_fcm.sends == []


# ----------------------------------------------------------------------------------
# 8. a raising tap does not stop publish from reaching subscribers
# ----------------------------------------------------------------------------------
async def test_raising_tap_does_not_stop_publish():
    from app.events.bus import EventBus

    bus = EventBus()

    def _boom(org_id, event):
        raise RuntimeError("tap exploded")

    bus.add_tap(_boom)

    org_id = uuid.uuid4()
    event = {"type": "call.ring", "call_id": str(uuid.uuid4())}
    async with bus.subscribe(org_id) as queue:
        bus.publish(org_id, event)
        assert queue.get_nowait() == event


# ----------------------------------------------------------------------------------
# 9. revoking a device session pushes a logout and drops its token
# ----------------------------------------------------------------------------------
async def test_session_revocation_pushes_logout_and_drops_token(
    browser, app_client, app, session, fake_fcm
):
    _org_id, login = await _setup_owner(
        browser, app_client, "ring-owner-9@example.com", "Ring Nine", NUM
    )
    sid = login["session_id"]

    r = await browser.delete(f"/api/v1/me/sessions/{sid}", headers=_csrf(browser))
    assert r.status_code == 204, r.text
    await _drain_pushes()

    assert len(fake_fcm.sends) == 1
    message = fake_fcm.sends[0]["message"]
    assert message["token"] == "tok-owner"
    assert message["data"] == {"kind": "logout"}
    assert message["android"]["priority"] == "HIGH"
    assert "notification" not in message["android"]

    session.expire_all()
    assert await _rows(session) == []
