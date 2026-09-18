"""P40: calls through a SignalWire SIP trunk into LiveKit.

A room call dials out through the trunk of the carrier that owns its caller-id number, and
an inbound room call on a SignalWire number is recorded against SignalWire. The Telnyx
trunk keeps working exactly as before (test_voice_plane.py covers it).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.db.base import set_org_context
from app.events.bus import EventBus
from app.main import create_app
from app.models import Call
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import LiveKitApi
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    create_org,
    register_and_login,
)
from tests.test_livekit_webhooks import post_lk, sip_event
from tests.test_voice_plane import (
    LK_KEY,
    LK_SECRET,
    default_lk_handler,
    make_livekit_settings,
    mock_livekit_client,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, _unscoped, install_voice_carrier

SW_NUMBER = "+16824231003"
TX_NUMBER = "+12145550100"
THEIRS = "+19725550199"


@pytest.fixture(autouse=True)
async def _no_leaked_dial_tasks():
    yield
    await voice_service.wait_for_pending_dial_tasks()


async def _org_with_numbers(client, email: str, numbers: dict[str, str]) -> tuple[str, dict]:
    token = await register_and_login(client, email)
    org = await create_org(client, token, f"Org {email}")
    for e164, carrier in numbers.items():
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": e164, "carrier": carrier},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 201, r.text
    return token, org


def _sip_dials(requests: list[httpx.Request]) -> list[dict]:
    return [
        json.loads(r.content) for r in requests if r.url.path.endswith("CreateSIPParticipant")
    ]


@pytest.fixture
async def room_app(engine, request):
    overrides = getattr(request, "param", {})
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
        **overrides,
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, requests
    await lk_client.aclose()


# ------------------------------------------------------------------------------------
# Trunk selection
# ------------------------------------------------------------------------------------
def test_room_trunks_lists_only_configured_trunks():
    both = make_livekit_settings(livekit_sip_signalwire_trunk_id="trunk-sw")
    assert voice_service.room_trunks(both) == {"telnyx": "trunk-out-1", "signalwire": "trunk-sw"}
    sw_only = make_livekit_settings(
        livekit_sip_outbound_trunk_id="", livekit_sip_signalwire_trunk_id="trunk-sw"
    )
    assert voice_service.room_trunks(sw_only) == {"signalwire": "trunk-sw"}
    assert voice_service.room_trunks(make_livekit_settings(livekit_sip_outbound_trunk_id="")) == {}


def test_trunk_for_carrier_falls_back_to_the_default_trunk():
    both = make_livekit_settings(livekit_sip_signalwire_trunk_id="trunk-sw")
    assert voice_service.trunk_for_carrier(both, "signalwire") == ("signalwire", "trunk-sw")
    assert voice_service.trunk_for_carrier(both, "telnyx") == ("telnyx", "trunk-out-1")
    assert voice_service.trunk_for_carrier(both, "bandwidth") == ("telnyx", "trunk-out-1")
    sw_only = make_livekit_settings(
        livekit_sip_outbound_trunk_id="", livekit_sip_signalwire_trunk_id="trunk-sw"
    )
    assert voice_service.trunk_for_carrier(sw_only, "bandwidth") is None


async def test_start_room_call_from_a_signalwire_number_dials_the_signalwire_trunk(
    client, session
):
    _token, org = await _org_with_numbers(
        client, "p40-unit@example.com", {SW_NUMBER: "signalwire", TX_NUMBER: "telnyx"}
    )
    org_id = uuid.UUID(org["id"])
    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    api = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    settings = make_livekit_settings(livekit_sip_signalwire_trunk_id="trunk-sw")

    set_org_context(session, org_id)
    try:
        sw_call, *_ = await voice_service.start_room_call(
            session, api, settings, EventBus(),
            org_id=org_id, to=THEIRS, from_e164=SW_NUMBER, identity="user-1",
        )
        tx_call, *_ = await voice_service.start_room_call(
            session, api, settings, EventBus(),
            org_id=org_id, to=THEIRS, from_e164=TX_NUMBER, identity="user-1",
        )
        await voice_service.wait_for_pending_dial_tasks()
    finally:
        await lk_client.aclose()

    assert sw_call.carrier == "signalwire"
    assert tx_call.carrier == "telnyx"
    dials = {d["sip_number"]: d["sip_trunk_id"] for d in _sip_dials(requests)}
    assert dials == {SW_NUMBER: "trunk-sw", TX_NUMBER: "trunk-out-1"}


# ------------------------------------------------------------------------------------
# POST /calls via="room"
# ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "room_app",
    [{"livekit_sip_outbound_trunk_id": "", "livekit_sip_signalwire_trunk_id": "trunk-sw"}],
    indirect=True,
)
async def test_a_signalwire_only_deploy_can_place_a_room_call(room_app):
    client, requests = room_app
    token, org = await _org_with_numbers(
        client, "p40-route1@example.com", {TX_NUMBER: "bandwidth", SW_NUMBER: "signalwire"}
    )
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["our_e164"] == SW_NUMBER
    await voice_service.wait_for_pending_dial_tasks()

    [dial] = _sip_dials(requests)
    assert dial["sip_trunk_id"] == "trunk-sw"
    assert dial["sip_number"] == SW_NUMBER
    assert dial["sip_call_to"] == THEIRS


@pytest.mark.parametrize(
    "room_app",
    [{"livekit_sip_outbound_trunk_id": "", "livekit_sip_signalwire_trunk_id": "trunk-sw"}],
    indirect=True,
)
async def test_softphone_token_is_available_with_only_the_signalwire_trunk(room_app):
    client, _requests = room_app
    token, org = await _org_with_numbers(
        client, "p40-route2@example.com", {SW_NUMBER: "signalwire"}
    )
    h = auth_headers(token, org["id"])
    r = await client.post("/api/v1/softphone/token", json={"room": "call-anything"}, headers=h)
    # Past the trunk gate: an unknown room is 404, not the 503 "no trunk" refusal.
    assert r.status_code == 404


@pytest.mark.parametrize(
    "room_app", [{"livekit_sip_signalwire_trunk_id": "trunk-sw"}], indirect=True
)
async def test_explicit_from_on_a_carrier_without_a_trunk_is_refused(room_app):
    client, requests = room_app
    token, org = await _org_with_numbers(
        client, "p40-route3@example.com", {TX_NUMBER: "bandwidth"}
    )
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS, "via": "room", "from": TX_NUMBER}, headers=h
    )
    assert r.status_code == 422
    message = r.json()["error"]["message"]
    assert "telnyx" in message and "signalwire" in message
    assert _sip_dials(requests) == []


# ------------------------------------------------------------------------------------
# Inbound
# ------------------------------------------------------------------------------------
async def test_inbound_room_call_on_a_signalwire_number_is_recorded_as_signalwire(
    room_app, session
):
    client, _requests = room_app
    await _org_with_numbers(client, "p40-in@example.com", {SW_NUMBER: "signalwire"})

    event = sip_event(
        "participant_joined",
        f"call-_{THEIRS}_abcdef012345",
        event_id="p40-in-1",
        identity=f"sip_{THEIRS}",
        sip_call_id=f"SCL_{uuid.uuid4()}",
        attributes={"sip.trunkPhoneNumber": SW_NUMBER, "sip.phoneNumber": THEIRS},
    )
    r = await post_lk(client, event)
    assert r.status_code == 200

    calls = [c for c in await _unscoped(session, Call) if c.our_e164 == SW_NUMBER]
    assert [c.carrier for c in calls] == ["signalwire"]
