"""P6: POST /api/v1/webhooks/livekit - signature verification, the leg/call state-machine
walk driven by LiveKit room/participant events, dedupe, and inbound room creation.

Outbound rooms are created through the real API (via="room") against a MockTransport-backed
LiveKitApi (see test_voice_plane.py's fixture for why a real LiveKitApi beats a fake here);
this file's own fixture reuses that same shape so the webhook events fired at the resulting
room exercise ``handle_livekit_event`` for real, the same way test_voice_webhooks.py drives
the Bandwidth/Telnyx webhook path through the real route rather than calling the service
function directly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid

import httpx
import jwt
import pytest

from app.main import create_app
from app.models import Call, CallLeg
from app.models.voice import VoiceEvent as VoiceEventRow
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import LiveKitApi
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
)
from tests.test_voice_plane import (
    LK_KEY,
    LK_SECRET,
    make_livekit_settings,
    make_org_with_room_number,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, _unscoped, install_voice_carrier

OUR = "+12145550100"
THEIRS = "+19725550199"

WEBHOOK_URL = "/api/v1/webhooks/livekit"


def sign_livekit_event(
    body: dict, *, api_key: str = LK_KEY, api_secret: str = LK_SECRET
) -> tuple[bytes, dict]:
    # (finding 15e) livekit_api.verify_webhook now requires an `exp` claim and only ever
    # matches a standard-base64 sha256 digest - both are exercised for real here, never a
    # hand-waved signature.
    raw = json.dumps(body).encode()
    digest = hashlib.sha256(raw).digest()
    now = int(time.time())
    claims = {
        "iss": api_key,
        "exp": now + 300,
        "sha256": base64.b64encode(digest).decode(),
    }
    token = jwt.encode(claims, api_secret, algorithm="HS256")
    return raw, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def lk_event(
    event_type: str,
    room: str,
    *,
    event_id: str,
    identity: str | None = None,
    attributes: dict | None = None,
) -> dict:
    body: dict = {"id": event_id, "event": event_type, "room": {"name": room}}
    if identity is not None:
        body["participant"] = {"identity": identity, "attributes": attributes or {}}
    return body


def sip_event(
    event_type: str,
    room: str,
    *,
    event_id: str,
    identity: str,
    sip_call_id: str,
    attributes: dict | None = None,
) -> dict:
    """A PSTN-participant event: classification is attribute-based only (B3+7) - the
    ``sip.callID`` attribute is what marks a participant as the SIP leg, never its
    identity string."""
    return lk_event(
        event_type,
        room,
        event_id=event_id,
        identity=identity,
        attributes={"sip.callID": sip_call_id, **(attributes or {})},
    )


async def post_lk(
    client: httpx.AsyncClient, body: dict, *, secret: str = LK_SECRET
) -> httpx.Response:
    raw, headers = sign_livekit_event(body, api_secret=secret)
    return await client.post(WEBHOOK_URL, content=raw, headers=headers)


@pytest.fixture
async def app_with_livekit(engine):
    """Bandwidth (FakeVoiceCarrier) + LiveKit (MockTransport), same shape as
    test_voice_plane.py's app_with_room_calls - kept as its own local copy per this
    repo's convention (see test_voice_api.py's comment) of not sharing fixtures with
    ambiguous names across test modules.

    B2: CreateSIPParticipant is a BACKGROUND dial now, so its mock response is GATED
    behind ``application.state.livekit_dial_gate`` rather than answering immediately -
    otherwise the dial task racing a test's own webhook posts would be nondeterministic.
    A test that wants the dial to resolve calls ``.set()`` on the gate, then
    ``voice_service.wait_for_pending_dial_tasks()``; one that does not care simply never
    touches it (the fixture releases it at teardown so nothing is left dangling)."""
    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake_voice = FakeVoiceCarrier()
    install_voice_carrier(application, fake_voice)

    dial_gate = asyncio.Event()
    application.state.livekit_dial_gate = dial_gate

    async def handler(request: httpx.Request) -> httpx.Response:
        if "CreateSIPParticipant" in request.url.path:
            await dial_gate.wait()
        return httpx.Response(200, json={})

    lk_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, application
    dial_gate.set()
    await voice_service.wait_for_pending_dial_tasks()
    await lk_client.aclose()


async def _make_outbound_room_call(client: httpx.AsyncClient, email: str, org_name: str) -> dict:
    token, org, _ = await make_org_with_room_number(client, email, org_name, OUR)
    h = auth_headers(token, org["id"])
    r = await client.post("/api/v1/calls", json={"to": THEIRS, "via": "room"}, headers=h)
    assert r.status_code == 201, r.text
    return {**r.json(), "_token": token, "_org": org, "_headers": h}


# ---------------------------------------------------------------------------------
# Signature / config gate
# ---------------------------------------------------------------------------------
async def test_missing_signature_is_401(app_with_livekit):
    client, _application = app_with_livekit
    r = await client.post(
        WEBHOOK_URL,
        content=b'{"event":"room_started"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 401


async def test_bad_signature_is_401(app_with_livekit):
    client, _application = app_with_livekit
    r = await post_lk(client, {"id": "e1", "event": "room_started"}, secret="wrong-secret")
    assert r.status_code == 401


async def test_unconfigured_livekit_is_404(engine):
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            WEBHOOK_URL, content=b"{}", headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 404


async def test_event_for_a_non_call_room_is_ignored(app_with_livekit, session):
    client, _application = app_with_livekit
    before = len(await _unscoped(session, VoiceEventRow))

    r = await post_lk(client, lk_event("room_started", "lobby", event_id="evt-ignore-1"))
    assert r.status_code == 200
    assert len(await _unscoped(session, VoiceEventRow)) == before


# ---------------------------------------------------------------------------------
# Outbound room lifecycle
# ---------------------------------------------------------------------------------
async def test_outbound_lifecycle_ringing_answered_hungup(app_with_livekit, session):
    """B2's ruling, end to end: ringing/hangup still come off participant presence
    (attribute-classified, B3+7), but "answered" comes EXCLUSIVELY from the background
    dial task resolving - a `track_published` webhook event must change nothing, even
    though the PSTN participant is legitimately present when it arrives."""
    client, application = app_with_livekit
    created = await _make_outbound_room_call(client, "lkout1@example.com", "Org O1")
    room, call_id = created["room"], created["id"]
    sip_identity = f"sip-{call_id}"

    r = await post_lk(
        client,
        sip_event(
            "participant_joined", room, event_id="e-ring", identity=sip_identity,
            sip_call_id=sip_identity,
        ),
    )
    assert r.status_code == 200
    session.expire_all()  # the row was changed by the request's OWN session, not this one
    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == uuid.UUID(call_id))
    assert leg.status == "ringing"

    # B2: track_published is NOT an answer signal for anybody, ever - livekit-sip
    # publishes the PSTN participant's audio track before the call is actually answered.
    r = await post_lk(
        client,
        sip_event(
            "track_published", room, event_id="e-track", identity=sip_identity,
            sip_call_id=sip_identity,
        ),
    )
    assert r.status_code == 200
    session.expire_all()
    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == uuid.UUID(call_id))
    calls = await _unscoped(session, Call)
    call = next(c for c in calls if str(c.id) == call_id)
    assert leg.status == "ringing", "track_published must never itself move the leg"
    assert call.status != "answered", "track_published must never itself answer a call"

    # The dial task's own wait_until_answered success IS the answer signal.
    application.state.livekit_dial_gate.set()
    await voice_service.wait_for_pending_dial_tasks()
    session.expire_all()
    calls = await _unscoped(session, Call)
    call = next(c for c in calls if str(c.id) == call_id)
    assert call.status == "answered"

    r = await post_lk(
        client,
        sip_event(
            "participant_left", room, event_id="e-hangup", identity=sip_identity,
            sip_call_id=sip_identity,
        ),
    )
    assert r.status_code == 200
    session.expire_all()
    calls = await _unscoped(session, Call)
    call = next(c for c in calls if str(c.id) == call_id)
    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == uuid.UUID(call_id))
    assert leg.status == "hungup"
    assert call.status == "completed"


async def test_room_finished_finalizes_a_leg_that_never_answered(app_with_livekit, session):
    client, _application = app_with_livekit
    created = await _make_outbound_room_call(client, "lkout2@example.com", "Org O2")
    room, call_id = created["room"], created["id"]

    r = await post_lk(client, lk_event("room_finished", room, event_id="e-finished"))
    assert r.status_code == 200

    calls = await _unscoped(session, Call)
    call = next(c for c in calls if str(c.id) == call_id)
    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == uuid.UUID(call_id))
    assert leg.status == "hungup"
    assert call.status == "no_answer"


async def test_duplicate_event_id_applies_once(app_with_livekit, session):
    client, _application = app_with_livekit
    created = await _make_outbound_room_call(client, "lkdup1@example.com", "Org D1")
    room, call_id = created["room"], created["id"]
    sip_identity = f"sip-{call_id}"

    event = sip_event(
        "participant_joined", room, event_id="dup-evt-1", identity=sip_identity,
        sip_call_id=sip_identity,
    )
    first = await post_lk(client, event)
    second = await post_lk(client, event)  # byte-identical replay
    assert first.status_code == 200
    assert second.status_code == 200

    events = [
        e for e in await _unscoped(session, VoiceEventRow) if e.provider_event_id == "dup-evt-1"
    ]
    assert len(events) == 1

    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == uuid.UUID(call_id))
    assert leg.status == "ringing", "the event must still have applied exactly once"


# ---------------------------------------------------------------------------------
# Inbound room creation
# ---------------------------------------------------------------------------------
async def test_inbound_participant_joined_creates_call_and_publishes_ring(
    app_with_livekit, session
):
    """Ground truth: an inbound dispatch-rule room is named
    ``call-_<callerNumber>_<random12>`` - the SIP call id is NEVER in the room name, so
    resolution/creation must key off the PSTN participant's real ``sip.callID`` attribute
    instead (B3+7)."""
    client, application = app_with_livekit
    token, org, _ = await make_org_with_number(client, "lkin1@example.com", "Org I1", OUR)
    org_id = uuid.UUID(org["id"])

    sip_call_id = f"SCL_{uuid.uuid4()}"
    room = f"call-_{THEIRS}_abcdef012345"
    event = sip_event(
        "participant_joined",
        room,
        event_id="in-evt-1",
        identity=f"sip_{THEIRS}",
        sip_call_id=sip_call_id,
        attributes={"sip.trunkPhoneNumber": OUR, "sip.phoneNumber": THEIRS},
    )

    bus = application.state.event_bus
    async with bus.subscribe(org_id) as queue:
        r = await post_lk(client, event)
        assert r.status_code == 200
        received = await asyncio.wait_for(queue.get(), timeout=1)

    assert received["type"] == "call.ring"
    assert received["room"] == room
    assert received["from"] == THEIRS
    assert received["to"] == OUR

    calls = await _unscoped(session, Call)
    inbound = [c for c in calls if c.direction == "inbound" and c.contact_e164 == THEIRS]
    assert len(inbound) == 1
    call = inbound[0]
    assert call.extra == {"via": "livekit", "room": room}
    assert str(call.id) == received["call_id"]

    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == call.id)
    assert leg.provider_call_id == sip_call_id
    assert leg.status == "ringing"
    assert leg.extra == {"sip_identity": f"sip_{THEIRS}"}


async def test_inbound_participant_joined_with_unmatched_trunk_number_is_a_noop(
    app_with_livekit, session
):
    client, _application = app_with_livekit
    before_calls = len(await _unscoped(session, Call))

    event = sip_event(
        "participant_joined",
        "call-_unmatched_random1234ab",
        event_id="in-evt-2",
        identity=f"sip_{THEIRS}",
        sip_call_id=f"SCL_{uuid.uuid4()}",
        attributes={"sip.trunkPhoneNumber": "+19995550000", "sip.phoneNumber": THEIRS},
    )
    r = await post_lk(client, event)
    assert r.status_code == 200
    assert len(await _unscoped(session, Call)) == before_calls


async def test_inbound_participant_without_sip_call_id_is_ignored(app_with_livekit, session):
    """B3+7: a SIP participant with no ``sip.callID`` attribute is refused/ignored rather
    than used to key a new inbound call - there is nothing safe to resolve future events
    against."""
    client, _application = app_with_livekit
    before_calls = len(await _unscoped(session, Call))

    event = lk_event(
        "participant_joined",
        "call-_unclassified_random12ab",
        event_id="in-evt-noid",
        identity=f"sip_{THEIRS}",
        attributes={"sip.trunkPhoneNumber": OUR, "sip.phoneNumber": THEIRS},
    )
    r = await post_lk(client, event)
    assert r.status_code == 200
    assert len(await _unscoped(session, Call)) == before_calls


async def test_inbound_room_finished_resolves_by_room_label(app_with_livekit, session):
    """A ``room_finished`` event carries no participant at all, so there is no
    ``sip.callID`` to key off - for an INBOUND room this falls back to the room name
    itself, the opaque label stored in ``Call.extra["room"]`` at creation time."""
    client, application = app_with_livekit
    _token, org, _number = await make_org_with_number(client, "lkinrf1@example.com", "Org RF1", OUR)
    org_id = uuid.UUID(org["id"])

    sip_call_id = f"SCL_{uuid.uuid4()}"
    room = f"call-_{THEIRS}_roomfinish01"
    joined = sip_event(
        "participant_joined",
        room,
        event_id="in-evt-rf-1",
        identity=f"sip_{THEIRS}",
        sip_call_id=sip_call_id,
        attributes={"sip.trunkPhoneNumber": OUR, "sip.phoneNumber": THEIRS},
    )
    bus = application.state.event_bus
    async with bus.subscribe(org_id) as queue:
        r = await post_lk(client, joined)
        assert r.status_code == 200
        await asyncio.wait_for(queue.get(), timeout=1)

    calls = await _unscoped(session, Call)
    call = next(c for c in calls if c.direction == "inbound" and c.contact_e164 == THEIRS)

    finished = lk_event("room_finished", room, event_id="in-evt-rf-2")
    r = await post_lk(client, finished)
    assert r.status_code == 200

    await session.refresh(call)
    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == call.id)
    assert leg.status == "hungup"
    assert call.status == "no_answer"


@pytest.mark.pg_only
async def test_concurrent_inbound_participant_joined_creates_one_call_pg(
    app_with_livekit, session
):
    """Finding 14: two redelivered ``participant_joined`` events racing to create the SAME
    new inbound call must still land exactly ONE Call/CallLeg. The IntegrityError handler in
    ``_create_inbound_room_call`` uses a SAVEPOINT (``begin_nested``, mirroring
    ``_ledger_livekit_event``) rather than a whole-transaction rollback, so the loser
    recovers within the same session instead of discarding unrelated pending work.

    SQLite serializes writes, so only Postgres actually exercises the race.
    """
    client, _application = app_with_livekit
    await make_org_with_number(client, "lkconc1@example.com", "Org LKC1", OUR)

    sip_call_id = f"SCL_{uuid.uuid4()}"
    room = f"call-_{THEIRS}_concurrent01"
    event = sip_event(
        "participant_joined",
        room,
        event_id="in-evt-conc-1",
        identity=f"sip_{THEIRS}",
        sip_call_id=sip_call_id,
        attributes={"sip.trunkPhoneNumber": OUR, "sip.phoneNumber": THEIRS},
    )

    results = await asyncio.gather(
        post_lk(client, event),
        post_lk(client, {**event, "id": "in-evt-conc-2"}),
        return_exceptions=True,
    )
    codes = [r.status_code for r in results if hasattr(r, "status_code")]
    assert all(c == 200 for c in codes), codes

    calls = await _unscoped(session, Call)
    inbound = [c for c in calls if c.direction == "inbound" and c.contact_e164 == THEIRS]
    assert len(inbound) == 1

    legs = await _unscoped(session, CallLeg)
    matching_legs = [leg for leg in legs if leg.provider_call_id == sip_call_id]
    assert len(matching_legs) == 1


async def test_non_sip_participant_joined_does_not_advance_the_leg(app_with_livekit, session):
    """Only the SIP (PSTN) participant's presence is a ring/answer/hangup signal - the
    softphone or AI agent joining the same room must not itself move the leg state."""
    client, _application = app_with_livekit
    created = await _make_outbound_room_call(client, "lknonsip1@example.com", "Org NS1")
    room, call_id = created["room"], created["id"]

    r = await post_lk(
        client,
        lk_event("participant_joined", room, event_id="e-human", identity="user-abc"),
    )
    assert r.status_code == 200

    legs = await _unscoped(session, CallLeg)
    leg = next(leg_row for leg_row in legs if leg_row.call_id == uuid.UUID(call_id))
    assert leg.status == "dialing", "a non-SIP participant must not move the leg"
