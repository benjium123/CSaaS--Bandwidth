"""P6: the org-scoped event bus, and the /api/v1/events/ws auth + forwarding logic.

httpx's ASGITransport (the transport every other test in this suite drives the app through)
implements the "http" ASGI scope only - it cannot perform a websocket handshake at all. So
the websocket ROUTE's auth/forward logic is exercised directly here through a small
FakeWebSocket double that implements exactly the subset of Starlette's WebSocket interface
`events_ws` calls (query_params/app/accept/close/send_json/receive) - the same "fake the
boundary, test the real logic" approach the rest of this suite uses for carriers
(FakeVoiceCarrier) rather than hitting a real transport it doesn't have.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.api.routes.softphone import events_ws
from app.auth.security import create_access_token
from app.events.bus import EventBus
from tests.conftest import create_org, make_settings, register_and_login

TEST_SECRET = "test-jwt-secret-not-a-real-one-padded-to-32+bytes"


# ==================================================================================
# EventBus
# ==================================================================================
async def test_publish_reaches_a_subscriber():
    bus = EventBus()
    org_id = uuid.uuid4()
    async with bus.subscribe(org_id) as queue:
        bus.publish(org_id, {"type": "call.ring"})
        event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event == {"type": "call.ring"}


async def test_publish_to_org_with_no_subscribers_does_not_raise():
    bus = EventBus()
    bus.publish(uuid.uuid4(), {"type": "call.ring"})  # must not raise


async def test_org_isolation_org_b_never_sees_org_as_events():
    bus = EventBus()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    async with bus.subscribe(org_a) as queue_a, bus.subscribe(org_b) as queue_b:
        bus.publish(org_a, {"type": "call.ring", "call_id": "a"})
        got_a = await asyncio.wait_for(queue_a.get(), timeout=1)
        assert queue_b.empty()
    assert got_a["call_id"] == "a"


async def test_slow_consumer_overflow_drops_oldest_without_blocking_publish():
    bus = EventBus()
    org_id = uuid.uuid4()
    async with bus.subscribe(org_id) as queue:
        # Fill the queue past its cap - publish is sync/non-blocking, so this must return
        # immediately even though nothing is draining the queue.
        from app.events.bus import QUEUE_MAXSIZE

        for i in range(QUEUE_MAXSIZE + 5):
            bus.publish(org_id, {"type": "seq", "i": i})

        assert queue.full()
        first_kept = queue.get_nowait()
        # The OLDEST events were dropped to make room for the newest, never the other way.
        assert first_kept["i"] == 5


async def test_unsubscribe_on_context_exit_stops_delivery():
    bus = EventBus()
    org_id = uuid.uuid4()
    async with bus.subscribe(org_id) as queue:
        pass
    bus.publish(org_id, {"type": "call.ring"})  # nobody subscribed anymore
    assert queue.empty()
    assert org_id not in bus._subscribers  # the org's empty subscriber set is cleaned up


# ==================================================================================
# /api/v1/events/ws - auth + forwarding, via a FakeWebSocket double
# ==================================================================================
class _FakeApp:
    def __init__(self, settings, event_bus):
        self.state = type("State", (), {"settings": settings, "event_bus": event_bus})()


class FakeWebSocket:
    def __init__(self, app, *, token: str | None, org_id: str | None):
        self.app = app
        self.query_params = {}
        if token is not None:
            self.query_params["token"] = token
        if org_id is not None:
            self.query_params["org_id"] = org_id
        self.accepted = False
        self.closed_code: int | None = None
        self.sent: list[dict] = []
        self._disconnect = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive(self) -> dict:
        await self._disconnect.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    def disconnect(self) -> None:
        self._disconnect.set()


@pytest.fixture
async def ws_org(client) -> tuple:
    """A real user + org (via the HTTP API, so auth/membership are genuine), returned as
    (settings, event_bus, token, org_id)."""
    settings = make_settings()
    token = await register_and_login(client, f"ws-{uuid.uuid4()}@example.com")
    org = await create_org(client, token, "Org WS")
    return settings, token, uuid.UUID(org["id"])


async def test_ws_valid_token_and_org_receives_a_published_event(engine, ws_org):
    settings, token, org_id = ws_org
    bus = EventBus()
    app = _FakeApp(settings, bus)
    ws = FakeWebSocket(app, token=token, org_id=str(org_id))

    task = asyncio.ensure_future(events_ws(ws))
    try:
        # Give the route a moment to authenticate, accept, and subscribe.
        for _ in range(50):
            if ws.accepted:
                break
            await asyncio.sleep(0.01)
        assert ws.accepted
        assert ws.closed_code is None

        bus.publish(org_id, {"type": "call.ring", "call_id": "c1"})
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        assert ws.sent == [{"type": "call.ring", "call_id": "c1"}]
    finally:
        ws.disconnect()
        await asyncio.wait_for(task, timeout=2)


async def test_ws_invalid_token_is_closed_4401_without_accepting_any_events(engine, ws_org):
    settings, _token, org_id = ws_org
    bus = EventBus()
    app = _FakeApp(settings, bus)
    ws = FakeWebSocket(app, token="not-a-real-token", org_id=str(org_id))

    await events_ws(ws)

    assert ws.closed_code == 4401
    assert ws.sent == []


async def test_ws_missing_org_id_is_closed_4401(engine, ws_org):
    settings, token, _org_id = ws_org
    bus = EventBus()
    app = _FakeApp(settings, bus)
    ws = FakeWebSocket(app, token=token, org_id=None)

    await events_ws(ws)

    assert ws.closed_code == 4401
    assert ws.sent == []


async def test_ws_member_of_a_different_org_is_rejected(engine, ws_org, client):
    settings, token, _own_org_id = ws_org

    # A second, genuinely separate user/org - `token` above belongs to neither of them.
    other_token = await register_and_login(client, f"otherws-{uuid.uuid4()}@example.com")
    other_org = await create_org(client, other_token, "Org WS Other")
    other_org_id = uuid.UUID(other_org["id"])

    app = _FakeApp(settings, EventBus())
    ws = FakeWebSocket(app, token=token, org_id=str(other_org_id))

    await events_ws(ws)

    assert ws.closed_code == 4401
    assert ws.sent == []


async def test_ws_expired_or_garbage_token_string_is_rejected(engine, ws_org):
    settings, _token, org_id = ws_org
    app = _FakeApp(settings, EventBus())
    garbage = create_access_token(uuid.uuid4(), "a-different-secret-entirely", expire_hours=1)
    ws = FakeWebSocket(app, token=garbage, org_id=str(org_id))

    await events_ws(ws)

    assert ws.closed_code == 4401
