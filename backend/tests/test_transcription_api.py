"""Transcription API: the calling-settings fields and POST /calls/{id}/transcribe."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import set_org_context
from app.models import Call, CallRecording
from app.services import recordings as recordings_svc
from app.services import sweeper as sweeper_svc
from tests.conftest import auth_headers
from tests.test_p29_voice_completeness import _org, p29_app  # noqa: F401


async def test_settings_round_trip_record_calls_and_transcription_mode(p29_app):
    client, _fake, _app = p29_app
    token, _org_body, org_id = await _org(client, "stt-settings")
    headers = auth_headers(token, org_id)

    r = await client.get("/api/v1/orgs/current/calling", headers=headers)
    assert r.json()["record_calls"] is False
    assert r.json()["transcription_mode"] == "off"

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"record_calls": True, "transcription_mode": "overnight"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["record_calls"] is True
    assert r.json()["transcription_mode"] == "overnight"

    r = await client.patch(
        "/api/v1/orgs/current/calling", json={"transcription_mode": "live"}, headers=headers
    )
    assert r.status_code == 422, r.text


async def _call_with_recording(session, org_id, *, provider_id=None, layout="mixed"):
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164="+12145550199",
        our_e164="+12145550100",
        carrier="bandwidth",
        status="completed",
        duration_seconds=60,
        ended_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        extra={"via": "livekit"},
    )
    session.add(call)
    await session.flush()
    rec_id = uuid.uuid4()
    session.add(
        CallRecording(
            id=rec_id,
            org_id=org_id,
            call_id=call.id,
            provider_recording_id=provider_id or f"lkrec:{call.id}",
            storage_key=recordings_svc.storage_key(org_id, rec_id),
            content_type="audio/mpeg",
            channel_layout=layout,
            status="stored",
            duration_seconds=60,
        )
    )
    await session.commit()
    return call


@pytest.fixture
def no_kick(monkeypatch):
    kicked: list = []
    monkeypatch.setattr(sweeper_svc, "kick_transcription", lambda app: kicked.append(app))
    return kicked


async def test_transcribe_is_unavailable_without_the_worker(p29_app, session, no_kick):
    client, _fake, app = p29_app
    token, _org_body, org_id = await _org(client, "stt-off")
    call = await _call_with_recording(session, org_id)

    r = await client.post(
        f"/api/v1/calls/{call.id}/transcribe", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 503, r.text
    assert no_kick == []


async def test_transcribe_queues_a_stereo_recording(p29_app, session, no_kick):
    client, _fake, app = p29_app
    app.state.settings = app.state.settings.model_copy(update={"stt_url": "http://stt:9100"})
    token, _org_body, org_id = await _org(client, "stt-on")
    call = await _call_with_recording(session, org_id)
    headers = auth_headers(token, org_id)

    r = await client.post(f"/api/v1/calls/{call.id}/transcribe", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["transcription_status"] == "queued"
    assert len(no_kick) == 1

    r = await client.get(f"/api/v1/calls/{call.id}", headers=headers)
    assert r.json()["transcription_status"] == "queued"


async def test_transcribe_refuses_a_single_track_recording(p29_app, session, no_kick):
    client, _fake, app = p29_app
    app.state.settings = app.state.settings.model_copy(update={"stt_url": "http://stt:9100"})
    token, _org_body, org_id = await _org(client, "stt-mono")
    call = await _call_with_recording(session, org_id, provider_id="carrier-rec-1")

    r = await client.post(
        f"/api/v1/calls/{call.id}/transcribe", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 409, r.text
    assert no_kick == []
