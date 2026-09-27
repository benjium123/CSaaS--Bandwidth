"""Wiring tests for app.services.lkrec sweeps and app.services.monitor_calls."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from app.db.base import set_org_context
from app.models import CallReview
from app.services import agent, lkrec, monitor_calls
from app.storage.base import InMemoryObjectStore
from tests.test_p43_call_monitoring import (
    FakeLiveKit,
    _finished_call,
    _org,
    mon_settings,
    voice,  # noqa: F401
)

# monitor_calls may expose the listener dispatch under any of these names.
_DISPATCH_NAMES = (
    "dispatch_listener",
    "_dispatch_listener",
    "start_listener",
    "_start_listener",
    "ensure_listener",
    "dispatch_agent",
    "_dispatch_agent",
)


def _settings(mon_settings, tmp_path):
    return mon_settings.model_copy(
        update={
            "monitor_recorder_url": "http://lkrec:9099",
            "monitor_recorder_dir": str(tmp_path),
            "elevenlabs_api_key": SecretStr("el-test"),
            "deepgram_api_key": SecretStr("dg-test"),
            "monitor_recording_keep_days": 7,
        }
    )


def _recording(prefix, started_at, files):
    return {
        "prefix": prefix,
        "started_at": started_at,
        "files": [{"role": role, "key": key} for role, key in files],
    }


def _segment(row):
    if isinstance(row, dict):
        role = row.get("role")
        text = row.get("text")
        start = row.get("start_ms", row.get("offset_ms", row.get("start")))
    else:
        role = getattr(row, "role", None)
        text = getattr(row, "text", None)
        start = getattr(row, "start_ms", getattr(row, "offset_ms", getattr(row, "at_ms", None)))
    return (role, text, int(start))


def _patch_dispatch(monkeypatch):
    """Spy on whichever listener-dispatch hook monitor_calls/agent exposes."""
    calls: list = []

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))

    for module in (monitor_calls, agent):
        for name in _DISPATCH_NAMES:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, spy)
                return calls
    raise AssertionError("no listener-dispatch hook found on monitor_calls/agent")


@pytest.mark.asyncio
async def test_purge_deletes_ok_recordings_only(session, mon_settings, tmp_path):
    settings = _settings(mon_settings, tmp_path)
    store = InMemoryObjectStore()
    org_id = await _org(session)
    set_org_context(session, org_id)

    ok_call = await _finished_call(session, org_id)
    scam_call = await _finished_call(session, org_id)
    await session.commit()

    ok_key = f"org/{org_id}/monitor/{ok_call.id}/p/customer-sip_a.ogg"
    scam_key = f"org/{org_id}/monitor/{scam_call.id}/p/customer-sip_b.ogg"
    ok_call.extra = {
        "monitor_recordings": [_recording("p", "2026-09-26T10:00:00Z", [("customer", ok_key)])]
    }
    scam_call.extra = {
        "monitor_recordings": [_recording("p", "2026-09-26T10:05:00Z", [("customer", scam_key)])]
    }
    await store.put(ok_key, b"ok-audio", "audio/ogg")
    await store.put(scam_key, b"scam-audio", "audio/ogg")

    # _finished_call already inserted one CallReview per call (call_id is unique), so
    # update those rows in place rather than inserting new ones.
    reviewed_at = datetime.now(timezone.utc) - timedelta(days=10)
    ok_review = (
        await session.execute(sa.select(CallReview).where(CallReview.call_id == ok_call.id))
    ).scalar_one()
    ok_review.status = "reviewed"
    ok_review.verdict = "ok"
    ok_review.reviewed_at = reviewed_at
    scam_review = (
        await session.execute(sa.select(CallReview).where(CallReview.call_id == scam_call.id))
    ).scalar_one()
    scam_review.status = "reviewed"
    scam_review.verdict = "scam"
    scam_review.reviewed_at = reviewed_at
    await session.commit()

    purged = await lkrec.purge_tick(session, settings, store)

    assert purged == 1
    with pytest.raises(KeyError):
        await store.get(ok_key)
    assert await store.get(scam_key) == b"scam-audio"
    await session.refresh(ok_call)
    assert ok_call.extra["monitor_recordings_purged"] is True


@pytest.mark.asyncio
async def test_resume_restarts_missing_rooms_only(session, mon_settings, tmp_path):
    settings = _settings(mon_settings, tmp_path)
    org_id = await _org(session)
    set_org_context(session, org_id)
    now = datetime.now(timezone.utc)

    played = await _finished_call(session, org_id)
    played.status = "in_progress"
    played.created_at = now - timedelta(minutes=10)
    played.answered_at = now - timedelta(minutes=5)
    played.extra = {
        "via": "livekit",
        "recorder": {"room": "call-A", "announcement": "a.ogg"},
    }

    fresh = await _finished_call(session, org_id)
    fresh.status = "in_progress"
    fresh.created_at = now - timedelta(minutes=10)
    fresh.answered_at = now - timedelta(seconds=5)
    fresh.extra = {
        "via": "livekit",
        "recorder": {"room": "call-B", "announcement": "a.ogg"},
    }
    await session.commit()

    starts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"ok": True, "rooms": []})
        starts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        restarted = await lkrec.resume_tick(session, settings, client=client)

    assert restarted == 2
    bodies = {body["room"]: body for body in starts}
    assert bodies["call-A"]["resume"] is True
    assert bodies["call-A"]["announcement"] == ""
    assert bodies["call-B"]["resume"] is False
    assert bodies["call-B"]["announcement"] == "a.ogg"


@pytest.mark.asyncio
async def test_on_livekit_event_uses_recorder_and_falls_back(
    session, mon_settings, tmp_path, monkeypatch
):
    settings = _settings(mon_settings, tmp_path)
    org_id = await _org(session)
    set_org_context(session, org_id)

    call = await _finished_call(session, org_id)
    call.status = "in_progress"
    call.extra = {"via": "livekit", "monitor": "new_account", "room": f"call-{call.id}"}
    await session.commit()
    room = f"call-{call.id}"
    api = FakeLiveKit()

    async def announcement(settings_, org_, **kwargs):
        return "a.ogg"

    started: list[str] = []

    async def start_ok(settings_, room_, **kwargs):
        started.append(room_)
        return True

    monkeypatch.setattr(lkrec, "ensure_announcement", announcement)
    monkeypatch.setattr(lkrec, "start", start_ok)
    dispatched = _patch_dispatch(monkeypatch)

    event = {
        "event": "participant_joined",
        "room": {"name": room},
        "participant": {
            "identity": "sip_caller",
            "kind": "sip",
            "attributes": {"sip.callID": "SIP-no-match"},
        },
    }
    await monitor_calls.on_livekit_event(session, api, settings, event)

    await session.refresh(call)
    assert call.extra["recorder"]["room"] == room
    assert room in started
    assert dispatched == []

    async def start_no(settings_, room_, **kwargs):
        return False

    monkeypatch.setattr(lkrec, "start", start_no)
    call2 = await _finished_call(session, org_id)
    call2.status = "in_progress"
    call2.extra = {"via": "livekit", "monitor": "new_account", "room": f"call-{call2.id}"}
    await session.commit()
    room2 = f"call-{call2.id}"

    event2 = {
        "event": "participant_joined",
        "room": {"name": room2},
        "participant": {
            "identity": "sip_caller",
            "kind": "sip",
            "attributes": {"sip.callID": "SIP-no-match-2"},
        },
    }
    await monitor_calls.on_livekit_event(session, api, settings, event2)

    assert dispatched


@pytest.mark.asyncio
async def test_transcribe_sides_labels_roles_and_offsets(
    session, mon_settings, tmp_path, monkeypatch
):
    settings = _settings(mon_settings, tmp_path).model_copy(
        update={"stt_url": "http://stt:9100"}
    )
    store = InMemoryObjectStore()
    org_id = await _org(session)
    set_org_context(session, org_id)

    call = await _finished_call(session, org_id)
    call.extra = {"recorder": {"room": f"call-{call.id}", "announcement": "a.ogg"}}
    await session.commit()

    # Our local Zipformer stt worker, one request per side.
    segments = [
        {"channel": 0, "text": "hello", "start_ms": 1500},
        {"channel": 0, "text": "hi there", "start_ms": 500},
    ]
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        item = segments[min(len(seen) - 1, len(segments) - 1)]
        return httpx.Response(200, json={"segments": [item], "channels": 1, "audio_sec": 2})

    captured: list = []

    async def capture(*args, **kwargs):
        for value in (*args, *kwargs.values()):
            if isinstance(value, (list, tuple)):
                captured.extend(value)
        return len(captured)

    monkeypatch.setattr("app.services.agent.upsert_transcript_segments", capture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await monitor_calls.transcribe_recording(
            session, settings, store, call, client=client
        ) == "waiting"

        k1 = f"org/{org_id}/monitor/{call.id}/p/customer-sip_a.ogg"
        k2 = f"org/{org_id}/monitor/{call.id}/p/agent-agent1.ogg"
        call.extra = {
            "recorder": {"room": f"call-{call.id}", "announcement": "a.ogg"},
            "monitor_recordings": [
                _recording(
                    "p",
                    "2026-09-26T10:00:00Z",
                    [("customer", k1), ("agent", k2)],
                )
            ],
        }
        await session.commit()
        await store.put(k1, b"customer-ogg", "audio/ogg")
        await store.put(k2, b"agent-ogg", "audio/ogg")

        status = await monitor_calls.transcribe_recording(
            session, settings, store, call, client=client
        )

    assert status == "done"
    assert len(seen) == 2
    rows = sorted((_segment(row) for row in captured), key=lambda row: row[2])
    assert rows == [("agent", "hi there", 500), ("user", "hello", 1500)]


