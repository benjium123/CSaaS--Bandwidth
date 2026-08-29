"""P6: LiveKit media-plane wiring - start_room_call/end_room_call unit tests, and the
softphone/answer API routes end-to-end with a MockTransport-backed LiveKitApi.

Reuses FakeVoiceCarrier/install_voice_carrier (test_voice_webhooks.py) for the bandwidth
side of a room call - via="room" still needs an ACTIVE org number to dial FROM, resolved
through the exact same `_resolve_outbound` as a carrier call; LiveKit itself is what's
mocked here, via a real LiveKitApi pointed at an httpx.MockTransport (never a fake stand-in
for LiveKitApi, so the Twirp request bodies it builds are genuinely exercised).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable

import httpx
import jwt
import pytest

from app.auth.security import decode_access_token
from app.db.base import set_org_context
from app.events.bus import EventBus
from app.main import create_app
from app.models.voice import Call, CallLeg
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import LiveKitApi
from tests.conftest import (
    TEST_JWT_SECRET,
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier


@pytest.fixture(autouse=True)
async def _no_leaked_dial_tasks():
    """A dial task that outlives its test mutates rows underneath whichever test runs
    next (observed once as a full-suite-only flake on the cross-org 404 assertion).
    Draining after EVERY test makes leakage impossible instead of merely unlikely."""
    yield
    await voice_service.wait_for_pending_dial_tasks()

OUR = "+12145550100"
THEIRS = "+19725550199"

LK_KEY = "lk-test-key"
LK_SECRET = "lk-test-secret-value-padded-to-32-bytes-plus"


def make_livekit_settings(**overrides):
    base = {
        "livekit_url": "ws://127.0.0.1:7880",
        "livekit_api_key": LK_KEY,
        "livekit_api_secret": LK_SECRET,
        "livekit_sip_outbound_trunk_id": "trunk-out-1",
    }
    base.update(overrides)
    return make_settings(**base)


def mock_livekit_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def default_lk_handler(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    return handler


async def _make_org(client) -> dict:
    token = await register_and_login(client, f"room-{uuid.uuid4()}@example.com")
    return token, await create_org(client, token, "Org Room")


async def make_org_with_room_number(
    client: httpx.AsyncClient, email: str, org_name: str, e164: str = OUR
) -> tuple[str, dict, dict]:
    """Same shape as conftest's ``make_org_with_number``, but the number lands on the ROOM
    trunk's carrier. Findings 10+11: via="room" number resolution never touches the carrier
    adapter registry - auto-pick only ever considers a number on carrier "telnyx" (the
    single trunk today), and POST /numbers accepts any carrier string regardless of what is
    actually registered, which is exactly the LiveKit-only-deploy shape this models."""
    token = await register_and_login(client, email)
    org = await create_org(client, token, org_name)
    r = await client.post(
        "/api/v1/numbers",
        json={"e164": e164, "carrier": "telnyx"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    return token, org, r.json()


# ==================================================================================
# make_api
# ==================================================================================
def test_make_api_none_when_url_unset():
    assert voice_service.make_api(make_settings()) is None


def test_make_api_none_when_secret_unset():
    assert voice_service.make_api(make_settings(livekit_url="ws://127.0.0.1:7880")) is None


def test_make_api_builds_client_when_configured():
    api = voice_service.make_api(make_livekit_settings())
    assert isinstance(api, LiveKitApi)


# ==================================================================================
# start_room_call (unit)
# ==================================================================================
async def test_start_room_call_creates_rows_and_dials_the_right_trunk(client, session):
    """B2: CreateSIPParticipant now runs in a BACKGROUND task (wait_until_answered=True),
    so the request path only ever synchronously creates the room and lands the leg on
    "dialing" - the dial itself, and the leg's move to "answered", only show up once
    ``wait_for_pending_dial_tasks`` has been awaited."""
    token, org = await _make_org(client)
    org_id = uuid.UUID(org["id"])

    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    api = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    settings = make_livekit_settings()
    bus = EventBus()

    set_org_context(session, org_id)
    try:
        call, leg, room, token_str = await voice_service.start_room_call(
            session,
            api,
            settings,
            bus,
            org_id=org_id,
            to=THEIRS,
            from_e164=OUR,
            identity="user-agent-1",
            name="agent@example.com",
            tag="lead-1",
        )

        assert call.direction == "outbound"
        assert call.status == "initiated"
        assert leg.status == "dialing"

        await voice_service.wait_for_pending_dial_tasks()
    finally:
        await lk_client.aclose()

    assert call.tag == "lead-1"
    assert call.extra == {"via": "livekit", "room": room}
    assert room == f"call-{call.id}"
    assert leg.provider_call_id == f"lk-{call.id}"

    await session.refresh(leg)
    await session.refresh(call)
    assert leg.status == "answered"
    assert call.status == "answered"

    bodies = [json.loads(r.content) for r in requests]
    assert bodies[0] == {"name": room, "empty_timeout": 300, "metadata": ""}
    assert bodies[1]["sip_trunk_id"] == "trunk-out-1"
    assert bodies[1]["sip_call_to"] == THEIRS
    assert bodies[1]["room_name"] == room
    assert bodies[1]["sip_number"] == OUR
    assert bodies[1]["participant_identity"] == f"sip-{call.id}"
    assert bodies[1]["wait_until_answered"] is True

    claims = jwt.decode(token_str, LK_SECRET, algorithms=["HS256"])
    assert claims["video"] == {
        "room": room,
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        "roomAdmin": False,
    }
    assert claims["sub"] == "user-agent-1"
    assert claims["name"] == "agent@example.com"


async def test_start_room_call_dial_task_no_answer_marks_call_no_answer(client, session):
    """B2's cause mapping: a wait_until_answered dial that comes back with a timeout/ring-
    no-answer flavored error lands the leg at "hungup" (not "failed") so the call derives
    to "no_answer", not "failed"."""
    token, org = await _make_org(client)
    org_id = uuid.UUID(org["id"])

    def handler(request: httpx.Request) -> httpx.Response:
        if "CreateSIPParticipant" in request.url.path:
            return httpx.Response(504, text="ringing timeout: no answer from callee")
        return httpx.Response(200, json={})

    lk_client = mock_livekit_client(handler)
    api = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    settings = make_livekit_settings()
    bus = EventBus()

    set_org_context(session, org_id)
    try:
        call, leg, _room, _token_str = await voice_service.start_room_call(
            session,
            api,
            settings,
            bus,
            org_id=org_id,
            to=THEIRS,
            from_e164=OUR,
            identity="user-agent-2",
        )
        await voice_service.wait_for_pending_dial_tasks()
    finally:
        await lk_client.aclose()

    await session.refresh(leg)
    await session.refresh(call)
    assert leg.status == "hungup"
    assert leg.hangup_cause == "no_answer"
    assert call.status == "no_answer"


async def test_start_room_call_livekit_error_marks_call_and_leg_failed_but_still_mints_a_token(
    client, session
):
    token, org = await _make_org(client)
    org_id = uuid.UUID(org["id"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="trunk unreachable")

    lk_client = mock_livekit_client(handler)
    api = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    settings = make_livekit_settings()
    bus = EventBus()

    set_org_context(session, org_id)
    try:
        call, leg, _room, token_str = await voice_service.start_room_call(
            session,
            api,
            settings,
            bus,
            org_id=org_id,
            to=THEIRS,
            from_e164=OUR,
            identity="agent@example.com",
        )
    finally:
        await lk_client.aclose()

    assert leg.status == "failed"
    assert call.status == "failed"
    assert "trunk unreachable" in leg.extra["error_detail"]
    # Token minting is offline JWT signing - it must not be gated on the API call's outcome.
    assert token_str


# ==================================================================================
# end_room_call (unit, no DB needed)
# ==================================================================================
def _fake_call(room: str | None) -> Call:
    extra = {"via": "livekit", "room": room} if room else {}
    return Call(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        direction="outbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status="completed",
        extra=extra,
    )


async def test_end_room_call_deletes_the_room():
    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    api = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    try:
        await voice_service.end_room_call(api, _fake_call("call-abc"))
    finally:
        await lk_client.aclose()
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/DeleteRoom")


async def test_end_room_call_never_raises_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    lk_client = mock_livekit_client(handler)
    api = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    try:
        await voice_service.end_room_call(api, _fake_call("call-abc"))  # must not raise
    finally:
        await lk_client.aclose()


async def test_end_room_call_is_a_noop_with_no_api_or_no_room():
    await voice_service.end_room_call(None, _fake_call("call-abc"))  # must not raise
    api = LiveKitApi(url="ws://x", api_key=LK_KEY, api_secret=LK_SECRET)
    await voice_service.end_room_call(api, _fake_call(None))  # no room recorded -> no-op
    await api.aclose()


# ==================================================================================
# API: POST /api/v1/calls via="room", POST /api/v1/softphone/token, .../answer
# ==================================================================================
@pytest.fixture
async def app_with_room_calls(engine):
    """Bandwidth (FakeVoiceCarrier, for `from` resolution) + LiveKit (MockTransport)."""
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake_voice = FakeVoiceCarrier()
    install_voice_carrier(application, fake_voice)

    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, application, fake_voice, requests
    await lk_client.aclose()


@pytest.fixture
async def app_with_voice_carrier_no_livekit(engine):
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


async def test_create_call_via_room_returns_call_detail_plus_room_and_token(app_with_room_calls):
    client, _application, fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_room_number(client, "viaroom1@example.com", "Org VR", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["direction"] == "outbound"
    assert body["status"] == "initiated"
    assert body["contact_e164"] == THEIRS
    assert body["room"].startswith("call-")
    assert body["token"]
    assert body["url"] == "ws://127.0.0.1:7880"
    assert len(body["legs"]) == 1
    assert body["legs"][0]["status"] == "dialing"

    # The room path never touches the carrier adapter at all.
    assert fake_voice.create_calls == []

    # Let the background dial (B2) resolve before the fixture tears its client down.
    await voice_service.wait_for_pending_dial_tasks()


async def test_create_call_via_room_when_livekit_unconfigured_is_503(
    app_with_voice_carrier_no_livekit,
):
    client, _fake, _application = app_with_voice_carrier_no_livekit
    token, org, _ = await make_org_with_number(client, "viaroom2@example.com", "Org VR2", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert r.status_code == 503


async def test_create_call_bad_via_value_is_422(app_with_room_calls):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "viaroom3@example.com", "Org VR3", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "sip"}, headers=h)
    assert r.status_code == 422


async def test_create_call_via_room_livekit_rejection_returns_201_with_failed_status(engine):
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    lk_client = mock_livekit_client(handler)
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_room_number(
            client, "viaroomfail@example.com", "Org F", OUR
        )
        h = auth_headers(token, org["id"])
        r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "failed"
    await lk_client.aclose()


async def test_softphone_token_for_known_room_returns_room_scoped_token(app_with_room_calls):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_room_number(client, "sp1@example.com", "Org S1", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert created.status_code == 201, created.text
    room = created.json()["room"]

    r = await client.post("/api/v1/softphone/token", json={"room": room}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"] == room
    assert body["url"] == "ws://127.0.0.1:7880"
    user_id = decode_access_token(token, TEST_JWT_SECRET)
    claims = jwt.decode(body["token"], LK_SECRET, algorithms=["HS256"])
    assert claims["video"]["room"] == room
    assert claims["sub"] == f"user-{user_id}"
    assert claims["name"] == "sp1@example.com"

    await voice_service.wait_for_pending_dial_tasks()


async def test_softphone_token_unknown_room_is_404(app_with_room_calls):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "sp2@example.com", "Org S2", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/softphone/token", json={"room": f"call-{uuid.uuid4()}"}, headers=h
    )
    assert r.status_code == 404


async def test_softphone_token_cross_org_room_is_404(app_with_room_calls):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token_a, org_a, _ = await make_org_with_room_number(client, "spA@example.com", "Org SA", OUR)
    h_a = auth_headers(token_a, org_a["id"])
    created = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h_a)
    assert created.status_code == 201, created.text
    room = created.json()["room"]
    # The known C1 flake, finally reproducible ~50% in isolation: org A's background dial
    # task commits on its own session while org B's register/login runs below, and on
    # SQLite's single StaticPool connection a concurrent commit can corrupt the login
    # SELECT's open cursor ("Incorrect email or password" for a user created 80ms ago).
    # Draining the dial task here removes the concurrency window deterministically.
    await voice_service.wait_for_pending_dial_tasks()

    token_b, org_b, _ = await make_org_with_number(
        client, "spB@example.com", "Org SB", "+12145550101"
    )
    h_b = auth_headers(token_b, org_b["id"])

    r = await client.post("/api/v1/softphone/token", json={"room": room}, headers=h_b)
    assert r.status_code == 404

    await voice_service.wait_for_pending_dial_tasks()


async def test_softphone_token_unconfigured_livekit_is_503(app_with_voice_carrier_no_livekit):
    client, _fake, _application = app_with_voice_carrier_no_livekit
    token, org, _ = await make_org_with_number(client, "sp3@example.com", "Org S3", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/softphone/token", json={"room": "call-anything"}, headers=h)
    assert r.status_code == 503


async def test_answer_call_returns_room_scoped_token_for_inbound_room_call(
    app_with_room_calls, session
):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "answer1@example.com", "Org AN1", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status="ringing",
        extra={"via": "livekit", "room": "call-sip-xyz"},
    )
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id="sip-xyz",
        to_e164=OUR,
        from_e164=THEIRS,
        status="ringing",
        reason="original",
    )
    session.add(call)
    session.add(leg)
    await session.commit()

    r = await client.post(f"/api/v1/calls/{call.id}/answer", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"] == "call-sip-xyz"
    user_id = decode_access_token(token, TEST_JWT_SECRET)
    claims = jwt.decode(body["token"], LK_SECRET, algorithms=["HS256"])
    assert claims["video"]["room"] == "call-sip-xyz"
    assert claims["sub"] == f"user-{user_id}"
    assert claims["name"] == "answer1@example.com"


async def test_answer_call_publishes_handoff_claimed_for_the_org(app_with_room_calls, session):
    """F9: answering a room call must tell every OTHER operator's softphone this
    ring/handoff card is already claimed, so it disappears from their incoming list
    too instead of staying visible for a call someone else already picked up."""
    client, application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "answerclaim@example.com", "Org ANCL", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status="ringing",
        extra={"via": "livekit", "room": "call-sip-claim"},
    )
    session.add(call)
    await session.commit()

    bus = application.state.event_bus
    async with bus.subscribe(org_id) as queue:
        r = await client.post(f"/api/v1/calls/{call.id}/answer", headers=h)
        assert r.status_code == 200, r.text
        event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event == {"type": "call.handoff.claimed", "call_id": str(call.id)}


async def test_answer_call_cross_org_is_404(app_with_room_calls, session):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token_a, org_a, _ = await make_org_with_number(client, "answerA@example.com", "Org ANA", OUR)
    org_id_a = uuid.UUID(org_a["id"])

    set_org_context(session, org_id_a)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id_a,
        direction="inbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status="ringing",
        extra={"via": "livekit", "room": "call-sip-xorg"},
    )
    session.add(call)
    await session.commit()

    token_b, org_b, _ = await make_org_with_number(
        client, "answerB@example.com", "Org ANB", "+12145550101"
    )
    h_b = auth_headers(token_b, org_b["id"])

    r = await client.post(f"/api/v1/calls/{call.id}/answer", headers=h_b)
    assert r.status_code == 404


async def test_answer_call_terminal_is_409(app_with_room_calls, session):
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "answerterm@example.com", "Org ANT", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status="completed",
        extra={"via": "livekit", "room": "call-sip-done"},
    )
    session.add(call)
    await session.commit()

    r = await client.post(f"/api/v1/calls/{call.id}/answer", headers=h)
    assert r.status_code == 409


async def test_answer_call_not_a_room_call_is_409(app_with_room_calls):
    client, _application, fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "answercarrier@example.com", "Org ANC", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)  # via=carrier
    assert created.status_code == 201, created.text
    call_id = created.json()["id"]

    r = await client.post(f"/api/v1/calls/{call_id}/answer", headers=h)
    assert r.status_code == 409


# ==================================================================================
# Findings 10, 11, 12: via="room" number resolution and trunk-config gating
# ==================================================================================
async def test_create_call_via_room_explicit_from_wrong_carrier_is_422(app_with_room_calls):
    """Finding 10: the SIP trunk dials out via telnyx only - an explicit `from` on any
    other carrier is refused outright (caller-id spoofing the trunk would reject anyway)."""
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _number = await make_org_with_number(
        client, "wrongcarrier1@example.com", "Org WC1", OUR
    )  # defaults to carrier "bandwidth"
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS, "via": "room", "from": OUR}, headers=h
    )
    assert r.status_code == 422
    assert "telnyx" in r.json()["error"]["message"]


async def test_create_call_via_room_no_telnyx_number_available_is_422(app_with_room_calls):
    """Finding 11: auto-pick for a room call only ever considers a carrier="telnyx"
    number - a bandwidth-only org has nothing the trunk can dial out on."""
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_number(client, "notelnyx1@example.com", "Org NTX1", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert r.status_code == 422
    assert "telnyx" in r.json()["error"]["message"]


async def test_create_call_via_room_no_outbound_trunk_is_503(engine):
    """Finding 12: LiveKit configured but no outbound SIP trunk id yet - inbound-only
    deploys are valid, but this route can never succeed without one."""
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
        livekit_sip_outbound_trunk_id="",
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_room_number(
            client, "notrunk1@example.com", "Org NT1", OUR
        )
        h = auth_headers(token, org["id"])
        r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
        assert r.status_code == 503


async def test_softphone_token_no_outbound_trunk_is_503(engine):
    """Finding 12: same gate as POST /calls via="room"."""
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
        livekit_sip_outbound_trunk_id="",
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_number(client, "notrunk2@example.com", "Org NT2", OUR)
        h = auth_headers(token, org["id"])
        r = await client.post(
            "/api/v1/softphone/token", json={"room": "call-anything"}, headers=h
        )
        assert r.status_code == 503


# ==================================================================================
# B1: room-call hangup / transfer / gather
# ==================================================================================
async def test_hangup_room_call_removes_participant_and_completes(app_with_room_calls):
    client, _application, _fake_voice, requests = app_with_room_calls
    token, org, _ = await make_org_with_room_number(client, "hangup1@example.com", "Org H1", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert created.status_code == 201, created.text
    call_id = created.json()["id"]

    # Let the background dial (B2) resolve first so hangup has an ANSWERED call to end.
    await voice_service.wait_for_pending_dial_tasks()

    r = await client.post(f"/api/v1/calls/{call_id}/hangup", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert all(leg["status"] == "hungup" for leg in body["legs"])

    remove_calls = [req for req in requests if "RemoveParticipant" in req.url.path]
    assert len(remove_calls) == 1
    payload = json.loads(remove_calls[0].content)
    assert payload["identity"] == f"sip-{call_id}"


async def test_transfer_room_call_success_ends_the_call(app_with_room_calls):
    client, _application, _fake_voice, requests = app_with_room_calls
    token, org, _ = await make_org_with_room_number(client, "xfer1@example.com", "Org X1", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert created.status_code == 201, created.text
    call_id = created.json()["id"]
    await voice_service.wait_for_pending_dial_tasks()

    r = await client.post(
        f"/api/v1/calls/{call_id}/transfer", json={"to": "+19725550111"}, headers=h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    # Unlike a carrier blind transfer, TransferSIPParticipant REFERs the PSTN leg away
    # entirely - no second leg is created.
    assert len(body["legs"]) == 1

    xfer_calls = [req for req in requests if "TransferSIPParticipant" in req.url.path]
    assert len(xfer_calls) == 1
    payload = json.loads(xfer_calls[0].content)
    assert payload["participant_identity"] == f"sip-{call_id}"
    assert payload["transfer_to"] == "+19725550111"


async def test_transfer_room_call_livekit_error_is_502(engine):
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())

    def handler(request: httpx.Request) -> httpx.Response:
        if "TransferSIPParticipant" in request.url.path:
            return httpx.Response(500, text="trunk refused transfer")
        return httpx.Response(200, json={})

    lk_client = mock_livekit_client(handler)
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_room_number(
            client, "xferfail@example.com", "Org XF", OUR
        )
        h = auth_headers(token, org["id"])
        created = await client.post(
            "/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h
        )
        assert created.status_code == 201, created.text
        call_id = created.json()["id"]
        await voice_service.wait_for_pending_dial_tasks()

        r = await client.post(
            f"/api/v1/calls/{call_id}/transfer", json={"to": "+19725550111"}, headers=h
        )
        assert r.status_code == 502
    await lk_client.aclose()


async def test_gather_room_call_is_409(app_with_room_calls):
    """B1: room-call DTMF is sent by the browser directly - server-side gather is
    carrier-path only."""
    client, _application, _fake_voice, _requests = app_with_room_calls
    token, org, _ = await make_org_with_room_number(client, "gather1@example.com", "Org G1", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert created.status_code == 201, created.text
    call_id = created.json()["id"]

    r = await client.post(f"/api/v1/calls/{call_id}/gather", json={}, headers=h)
    assert r.status_code == 409

    await voice_service.wait_for_pending_dial_tasks()
