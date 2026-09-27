"""Live captions: dispatch rules, the monthly Deepgram overflow cap, and usage metering."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from app.db.base import set_org_context
from app.models import Org
from app.services import ai_usage, calling_settings, live_captions, lkrec, monitor_calls
from tests.test_customer_recording_wiring import _joined, _room_call
from tests.test_lkrec_wiring import _settings
from tests.test_p43_call_monitoring import FakeLiveKit, _org, mon_settings, voice  # noqa: F401


class DispatchSpy(FakeLiveKit):
    def __init__(self):
        super().__init__()
        self.dispatches: list[dict] = []

    async def create_agent_dispatch(self, *, room, agent_name, metadata):  # noqa: ANN001
        self.dispatches.append({"room": room, "agent_name": agent_name, "metadata": json.loads(metadata)})


async def _entitle(session, org_id, key="call_transcription"):
    """P2: transcription is an ops-controlled per-workspace feature (default off)."""
    import uuid as _uuid

    from app.services import entitlements

    await entitlements.set_feature(
        session, _uuid.UUID(str(org_id)), key,
        enabled=True, price_override_micros=None, actor_user_id=None,
    )
    await session.commit()


async def _live_org(session, mode="live", record=True):
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    calling_settings.apply(org, record_calls=record, transcription_mode=mode)
    await session.commit()
    await _entitle(session, org_id)
    await _entitle(session, org_id, "call_recording")
    return org_id


def _live_settings(mon_settings, tmp_path, **extra):
    return _settings(mon_settings, tmp_path).model_copy(
        update={"monitor_enforced": False, "live_captions_agent_name": "live-captions", **extra}
    )


@pytest.fixture
def recorder_ok(monkeypatch):
    async def start_ok(settings_, room_, **kwargs):
        return True

    monkeypatch.setattr(lkrec, "start", start_ok)


@pytest.mark.asyncio
async def test_live_mode_dispatches_captions_with_deepgram_overflow(
    session, mon_settings, tmp_path, recorder_ok
):
    settings = _live_settings(mon_settings, tmp_path)
    org_id = await _live_org(session)
    call = await _room_call(session, org_id)
    api = DispatchSpy()

    await monitor_calls.on_livekit_event(session, api, settings, _joined(call))
    await monitor_calls.on_livekit_event(session, api, settings, _joined(call))

    assert len(api.dispatches) == 1
    d = api.dispatches[0]
    assert d["agent_name"] == "live-captions"
    assert d["metadata"] == {"call_id": str(call.id), "org_id": str(org_id), "overflow": "deepgram"}
    await session.refresh(call)
    assert call.extra["live_captions"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,record", [("after_call", True), ("live", False)])
async def test_no_captions_unless_live_and_recording(
    session, mon_settings, tmp_path, recorder_ok, mode, record
):
    settings = _live_settings(mon_settings, tmp_path)
    org_id = await _live_org(session, mode=mode, record=record)
    call = await _room_call(session, org_id)
    api = DispatchSpy()

    await monitor_calls.on_livekit_event(session, api, settings, _joined(call))

    assert api.dispatches == []


@pytest.mark.asyncio
async def test_overflow_off_without_deepgram_key_or_over_cap(session, mon_settings, tmp_path, monkeypatch):
    org_id = await _live_org(session)
    no_key = _live_settings(mon_settings, tmp_path, deepgram_api_key=SecretStr(""))
    assert await live_captions.overflow_allowed(session, no_key, org_id) is False

    with_key = _live_settings(mon_settings, tmp_path, live_overflow_max_minutes=1)
    assert await live_captions.overflow_allowed(session, with_key, org_id) is True

    async def used(session_, org_id_, now=None):
        return 60

    monkeypatch.setattr(live_captions, "overflow_seconds_this_month", used)
    assert await live_captions.overflow_allowed(session, with_key, org_id) is False


@pytest.mark.asyncio
async def test_meter_overflow_records_platform_cost_once_per_call(session, monkeypatch):
    org_id = await _live_org(session)
    calls: list[dict] = []

    async def record(session_, org_id_, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(ai_usage, "record", record)

    class C:
        id = "call-1"

    C.org_id = org_id
    await live_captions.meter_overflow(session, C, 0)
    await live_captions.meter_overflow(session, C, 12.2)

    assert len(calls) == 1
    assert calls[0]["source"] == "platform"
    assert calls[0]["provider"] == "deepgram"
    assert calls[0]["quantity"] == 13
    assert calls[0]["idempotency_key"] == "live-stt:call-1"
