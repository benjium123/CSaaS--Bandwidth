"""Phase 5: call recordings - carrier-authenticated fetch, our own storage, our own auth.

F6/F7/F16 code-review round: the fetch moved OUT of the webhook path (which must stay
DB-only, ARCHITECTURE D6) and onto the sweeper. `on_recording_ready` now only upserts a
`pending` CallRecording row; `recordings_svc.fetch_pending_recordings` (driven by
`sweeper.run_once`) is what actually downloads and stores the bytes, re-deriving the
carrier's URL from the `voice_events` row the webhook already ledgered rather than adding a
column to CallRecording for it (that would put a carrier-authenticated link one query away
from the API/UI - exactly the leak this module exists to prevent).

Same discipline as test_media_pipeline.py's inbound-MMS re-hosting tests: credentials go
ONLY to the carrier's own host, and the carrier's URL must never reach anything the API
serves.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.main import create_app
from app.models import Call, CallLeg, CallRecording
from app.models.voice import VoiceEvent as VoiceEventRow
from app.providers.voice import VoiceEvent
from app.services import recordings as recordings_svc
from app.storage.base import InMemoryObjectStore
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
    webhook_auth_headers,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

OUR = "+12145550100"
THEIRS = "+19725550199"
AUDIO = b"ID3" + b"x" * 200

ANSWER_URL = "/api/v1/webhooks/bandwidth/voice/answer"


@pytest.fixture
async def app_with_voice_carrier(engine):
    """App wired with a FakeVoiceCarrier named 'bandwidth' - defined locally (not imported
    as a fixture) so ruff does not mistake the test parameter for shadowing an import."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


async def _make_call(client, token, org):
    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _fire_recording_ready(client, fake, event: VoiceEvent) -> httpx.Response:
    """Drive `recording_ready` through the REAL webhook route so both the VoiceEvent ledger
    row AND the pending CallRecording row are created exactly the way production does it -
    the raw JSON body is irrelevant since FakeVoiceCarrier.parse_voice_webhook ignores it and
    returns whatever `events_to_return` was set to."""
    fake.events_to_return = [event]
    return await client.post(ANSWER_URL, content=b"{}", headers=webhook_auth_headers())


async def _seed_pending_recording(session, call, leg, event: VoiceEvent) -> CallRecording:
    """Ledger the VoiceEvent + upsert the pending CallRecording the way apply_voice_event ->
    on_recording_ready does via the webhook path, WITHOUT going over HTTP - used by the
    fetch-mechanics tests below, which are about fetch_pending_recordings' own behaviour,
    not the webhook route."""
    set_org_context(session, call.org_id)
    session.add(
        VoiceEventRow(
            id=uuid.uuid4(),
            org_id=call.org_id,
            call_id=call.id,
            leg_id=leg.id,
            carrier=call.carrier,
            provider_event_id=event.provider_event_id,
            event_type="recording_ready",
            payload=dict(event.raw or {}),
        )
    )
    recording = await recordings_svc.on_recording_ready(session, event, call, leg)
    await session.commit()
    return recording


async def _get_recording(session, provider_recording_id: str) -> CallRecording:
    stmt = sa.select(CallRecording).where(
        CallRecording.provider_recording_id == provider_recording_id
    ).execution_options(**{ALLOW_UNSCOPED_KEY: True})
    return (await session.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------------
# Credentials go only to the carrier's own host
# ---------------------------------------------------------------------------------
def test_bandwidth_recording_auth_scopes_credentials_to_carrier_hosts():
    """Credentials go ONLY to *.bandwidth.com. A webhook payload naming any other host -
    including a bandwidth.com lookalike - gets nothing. Exercised against the REAL adapter
    so an attribute drift between adapter and mixin (the original P5 bug: the mixin read
    self.api_username while the adapter stores self._auth) fails loudly here."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    carrier = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id="app"
    )
    assert carrier.recording_auth("https://media.bandwidth.com/rec.mp3") == ("u", "p")
    assert carrier.recording_auth("https://evil.example.com/x.mp3") is None
    assert carrier.recording_auth("https://notbandwidth.com/x.mp3") is None
    assert carrier.recording_auth("not a url at all") is None


def test_telnyx_recording_auth_is_always_none():
    """Telnyx hosts recordings publicly - attaching credentials because a webhook payload
    named a host would be a credential-leak primitive regardless."""
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    carrier = TelnyxMessagingCarrier(api_key="k")
    assert carrier.recording_auth("https://recordings.telnyx.com/x.mp3") is None


# ---------------------------------------------------------------------------------
# F6: the webhook path does NO network I/O - it only upserts a pending row
# ---------------------------------------------------------------------------------
async def test_webhook_upserts_pending_recording_with_zero_http_calls(
    app_with_voice_carrier, session
):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "rec1@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])

    call_out = await _make_call(client, token, org)
    provider_call_id = call_out["legs"][0]["provider_call_id"]

    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=provider_call_id,
        provider_event_id="rec-evt-1",
        recording_url="https://voice.bandwidth.com/recordings/abc.mp3",
        provider_recording_id="rec-abc",
        duration_seconds=12,
        raw={
            "eventType": "recordingAvailable",
            "callId": provider_call_id,
            "recordingId": "rec-abc",
            "mediaUrl": "https://voice.bandwidth.com/recordings/abc.mp3",
            "duration": "PT12S",
            "to": THEIRS,
            "from": OUR,
        },
    )
    # `handler` above would fail loudly (pragma: no cover) if the webhook path ever made
    # an HTTP request of its own - it must not, so nothing here should call it yet.
    r = await _fire_recording_ready(client, fake, event)
    assert r.status_code == 200
    assert http_calls == 0, "the webhook path must never fetch the recording itself"

    set_org_context(session, org_id)
    recording = await _get_recording(session, "rec-abc")
    assert recording.status == "pending"
    assert recording.size_bytes is None

    # Now the sweeper actually fetches it.
    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert n == 1
    assert http_calls == 1
    await session.refresh(recording)
    assert recording.status == "stored"
    assert recording.duration_seconds == 12


# ---------------------------------------------------------------------------------
# Fetch -> store -> serve (via the sweeper)
# ---------------------------------------------------------------------------------
async def test_sweeper_fetch_stores_and_the_api_never_exposes_the_carrier_url(
    app_with_voice_carrier, session
):
    client, fake, application = app_with_voice_carrier
    fake.recording_auth_result = ("bw-user", "bw-pass")
    token, org, _ = await make_org_with_number(client, "rec1b@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])

    call_out = await _make_call(client, token, org)
    call_id = uuid.UUID(call_out["id"])
    leg_id = uuid.UUID(call_out["legs"][0]["id"])

    set_org_context(session, org_id)
    call = await session.get(Call, call_id)
    leg = await session.get(CallLeg, leg_id)

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=leg.provider_call_id,
        provider_event_id="rec-evt-1b",
        recording_url="https://voice.bandwidth.com/recordings/abc.mp3",
        provider_recording_id="rec-abc-b",
        duration_seconds=12,
    )
    await _seed_pending_recording(session, call, leg, event)

    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    fake.events_to_return = [event]
    store = application.state.media_store
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, store, application.state.carriers, client=http
        )
    assert n == 1

    rec = await _get_recording(session, "rec-abc-b")
    assert rec.status == "stored"
    assert seen_auth and seen_auth[0].startswith("Basic "), "credentials must reach the fetch"
    assert await store.get(rec.storage_key) == AUDIO
    assert rec.duration_seconds == 12

    detail = await client.get(
        f"/api/v1/calls/{call_id}", headers=auth_headers(token, org["id"])
    )
    body = detail.json()
    assert len(body["recordings"]) == 1
    assert body["recordings"][0]["url"].endswith(
        f"/api/v1/calls/{call_id}/recordings/{rec.id}"
    )
    assert "bandwidth.com" not in str(body), "the carrier's own URL must never reach the API"

    got = await client.get(
        f"/api/v1/calls/{call_id}/recordings/{rec.id}",
        headers=auth_headers(token, org["id"]),
    )
    assert got.status_code == 200
    assert got.content == AUDIO
    assert got.headers["content-type"].startswith("audio/mpeg")


async def test_recording_fetch_sends_no_credentials_when_carrier_declines(
    app_with_voice_carrier, session
):
    """Telnyx-shaped: recording_auth() returns None, so the fetch must carry no auth."""
    client, fake, application = app_with_voice_carrier
    fake.recording_auth_result = None
    token, org, _ = await make_org_with_number(client, "rec2@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])

    call_out = await _make_call(client, token, org)
    set_org_context(session, org_id)
    call = await session.get(Call, uuid.UUID(call_out["id"]))
    leg = await session.get(CallLeg, uuid.UUID(call_out["legs"][0]["id"]))

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=leg.provider_call_id,
        provider_event_id="rec-evt-2",
        recording_url="https://recordings.telnyx.com/abc.wav",
        provider_recording_id="rec-def",
    )
    await _seed_pending_recording(session, call, leg, event)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {k.lower() for k in request.headers}
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/wav"})

    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert n == 1
    rec = await _get_recording(session, "rec-def")
    assert rec.status == "stored"
    assert rec.content_type == "audio/wav"


async def test_octet_stream_is_treated_as_audio_mpeg(app_with_voice_carrier, session):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "rec3@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])
    call_out = await _make_call(client, token, org)
    set_org_context(session, org_id)
    call = await session.get(Call, uuid.UUID(call_out["id"]))
    leg = await session.get(CallLeg, uuid.UUID(call_out["legs"][0]["id"]))

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=leg.provider_call_id,
        provider_event_id="rec-evt-3",
        recording_url="https://voice.bandwidth.com/x",
        provider_recording_id="rec-oct",
    )
    await _seed_pending_recording(session, call, leg, event)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=AUDIO, headers={"content-type": "application/octet-stream"}
        )

    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert n == 1
    rec = await _get_recording(session, "rec-oct")
    assert rec.status == "stored"
    assert rec.content_type == "audio/mpeg"


async def test_unsupported_content_type_fails_without_storing(app_with_voice_carrier, session):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "rec4@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])
    call_out = await _make_call(client, token, org)
    set_org_context(session, org_id)
    call = await session.get(Call, uuid.UUID(call_out["id"]))
    leg = await session.get(CallLeg, uuid.UUID(call_out["legs"][0]["id"]))

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=leg.provider_call_id,
        provider_event_id="rec-evt-4",
        recording_url="https://voice.bandwidth.com/x",
        provider_recording_id="rec-bad",
    )
    recording = await _seed_pending_recording(session, call, leg, event)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})

    store = InMemoryObjectStore()
    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, store, application.state.carriers, client=http
        )
    assert n == 0
    await session.refresh(recording)
    assert recording.status == "failed"
    assert not await store.exists(recording.storage_key), (
        "nothing unverified may reach the object store"
    )


async def test_oversized_recording_is_refused_midstream(
    app_with_voice_carrier, session, monkeypatch
):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "rec5@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])
    call_out = await _make_call(client, token, org)
    set_org_context(session, org_id)
    call = await session.get(Call, uuid.UUID(call_out["id"]))
    leg = await session.get(CallLeg, uuid.UUID(call_out["legs"][0]["id"]))

    monkeypatch.setattr(recordings_svc, "MAX_RECORDING_BYTES", 10)

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=leg.provider_call_id,
        provider_event_id="rec-evt-5",
        recording_url="https://voice.bandwidth.com/x",
        provider_recording_id="rec-big",
    )
    recording = await _seed_pending_recording(session, call, leg, event)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert n == 0
    await session.refresh(recording)
    assert recording.status == "failed"


async def test_a_redelivered_recording_ready_does_not_create_a_second_row(
    app_with_voice_carrier, session
):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "rec6@example.com", "Org R", OUR)
    org_id = uuid.UUID(org["id"])
    call_out = await _make_call(client, token, org)
    provider_call_id = call_out["legs"][0]["provider_call_id"]

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=provider_call_id,
        provider_event_id="rec-evt-6",
        recording_url="https://voice.bandwidth.com/x",
        provider_recording_id="rec-dupe",
    )
    first_resp = await _fire_recording_ready(client, fake, event)
    assert first_resp.status_code == 200

    # A byte-identical redelivery must dedupe on provider_event_id and touch nothing.
    second_resp = await _fire_recording_ready(client, fake, event)
    assert second_resp.status_code == 200

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(CallRecording).where(CallRecording.provider_recording_id == "rec-dupe")
        )
    ).scalars().all()
    assert len(rows) == 1, "a redelivered recording_ready must not create a second row"
    assert rows[0].status == "pending"

    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    fake.events_to_return = [event]
    store = InMemoryObjectStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = await recordings_svc.fetch_pending_recordings(
            session, store, application.state.carriers, client=http
        )
        second = await recordings_svc.fetch_pending_recordings(
            session, store, application.state.carriers, client=http
        )
    assert first == 1
    assert second == 0, "an already-stored recording must not be re-fetched"
    assert fetch_count == 1


# ---------------------------------------------------------------------------------
# F6: sweeper retry bookkeeping - backoff, then success on the next eligible run
# ---------------------------------------------------------------------------------
async def test_sweeper_recording_fetch_backs_off_then_retries_and_succeeds(
    app_with_voice_carrier, session
):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "recB@example.com", "Org RB", OUR)
    org_id = uuid.UUID(org["id"])
    call_out = await _make_call(client, token, org)
    provider_call_id = call_out["legs"][0]["provider_call_id"]

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=provider_call_id,
        provider_event_id="rec-evt-backoff",
        recording_url="https://voice.bandwidth.com/x.mp3",
        provider_recording_id="rec-backoff",
        duration_seconds=9,
    )
    r = await _fire_recording_ready(client, fake, event)
    assert r.status_code == 200

    set_org_context(session, org_id)
    recording = await _get_recording(session, "rec-backoff")
    assert recording.status == "pending"

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    # Run 1: the pending row's first attempt fails. The return value counts SUCCESSFUL
    # fetches (same convention as media.py::fetch_pending_media), so a failed attempt is 0.
    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first_run = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert first_run == 0
    await session.refresh(recording)
    assert recording.status == "failed"

    # Still inside the backoff window - must not be retried yet.
    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        too_soon = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert too_soon == 0, "a failed row must not be retried inside its backoff window"
    assert attempts["n"] == 1

    # Force the row stale past RETRY_BACKOFF_SECONDS without touching real wall-clock -
    # `updated_at` is TimestampMixin's own onupdate, not something this module controls.
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=recordings_svc.RETRY_BACKOFF_SECONDS + 5
    )
    await session.execute(
        sa.update(CallRecording).where(CallRecording.id == recording.id).values(updated_at=stale)
    )
    await session.commit()

    # Run 2: now eligible again, and this time it succeeds.
    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        second_run = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert second_run == 1
    await session.refresh(recording)
    assert recording.status == "stored"


async def test_sweeper_recording_fetch_gives_up_permanently_past_the_ceiling(
    app_with_voice_carrier, session
):
    client, fake, application = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "recC@example.com", "Org RC", OUR)
    org_id = uuid.UUID(org["id"])
    call_out = await _make_call(client, token, org)
    provider_call_id = call_out["legs"][0]["provider_call_id"]

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=provider_call_id,
        provider_event_id="rec-evt-giveup",
        recording_url="https://voice.bandwidth.com/x.mp3",
        provider_recording_id="rec-giveup",
    )
    await _fire_recording_ready(client, fake, event)

    set_org_context(session, org_id)
    recording = await _get_recording(session, "rec-giveup")

    ancient = datetime.now(timezone.utc) - timedelta(
        seconds=recordings_svc.GIVE_UP_AFTER_SECONDS + 60
    )
    recording.status = "failed"
    await session.commit()
    await session.execute(
        sa.update(CallRecording)
        .where(CallRecording.id == recording.id)
        .values(updated_at=ancient)
    )
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a permanently-abandoned recording must never be fetched again")

    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = await recordings_svc.fetch_pending_recordings(
            session, InMemoryObjectStore(), application.state.carriers, client=http
        )
    assert n == 0
    await session.refresh(recording)
    assert recording.status == "failed"


# ---------------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------------
async def test_cross_org_recording_fetch_is_404(app_with_voice_carrier, session):
    client, fake, application = app_with_voice_carrier
    fake.recording_auth_result = ("bw-user", "bw-pass")
    token_a, org_a, _ = await make_org_with_number(client, "recA@example.com", "Org A", OUR)
    org_a_id = uuid.UUID(org_a["id"])
    call_out = await _make_call(client, token_a, org_a)
    call_id = uuid.UUID(call_out["id"])
    provider_call_id = call_out["legs"][0]["provider_call_id"]

    event = VoiceEvent(
        event_type="recording_ready",
        provider_call_id=provider_call_id,
        provider_event_id="rec-evt-cross",
        recording_url="https://voice.bandwidth.com/x.mp3",
        provider_recording_id="rec-cross",
    )
    await _fire_recording_ready(client, fake, event)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    fake.events_to_return = [event]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await recordings_svc.fetch_pending_recordings(
            session, application.state.media_store, application.state.carriers, client=http
        )

    set_org_context(session, org_a_id)
    recording = await _get_recording(session, "rec-cross")

    token_b, org_b, _ = await make_org_with_number(
        client, "recB2@example.com", "Org B", "+12145550101"
    )

    got = await client.get(
        f"/api/v1/calls/{call_id}/recordings/{recording.id}",
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert got.status_code == 404
