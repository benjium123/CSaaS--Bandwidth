"""Customer recording wiring: the LiveKit hook starts lkrec for orgs that record every call."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org
from app.models.voice import CallRecording
from app.services import calling_settings, lkrec, monitor_calls
from tests.test_lkrec_wiring import _patch_dispatch, _settings
from tests.test_p43_call_monitoring import (
    FakeLiveKit,
    _finished_call,
    _org,
    mon_settings,  # noqa: F401
    voice,  # noqa: F401
)


async def _room_call(session, org_id, **extra):
    call = await _finished_call(session, org_id)
    call.status = "in_progress"
    call.extra = {"via": "livekit", "room": f"call-{call.id}", **extra}
    await session.commit()
    return call


def _joined(call):
    return {
        "event": "participant_joined",
        "room": {"name": f"call-{call.id}"},
        "participant": {
            "identity": "sip_caller",
            "kind": "sip",
            "attributes": {"sip.callID": f"SIP-{call.id}"},
        },
    }


async def _setup(session, mon_settings, tmp_path, monkeypatch, *, record=True, announce=False):
    settings = _settings(mon_settings, tmp_path).model_copy(update={"monitor_enforced": False})
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    calling_settings.apply(org, record_calls=record)
    if record:
        # Customer recording also needs the ops-controlled entitlement (P2).
        from app.services import entitlements

        await entitlements.set_feature(
            session, org_id, "call_recording",
            enabled=True, price_override_micros=None, actor_user_id=None,
        )
    org.recording_announcement = announce
    await session.commit()
    started: list[tuple[str, str]] = []
    announced: list[str] = []

    async def start_ok(settings_, room_, **kwargs):
        started.append((room_, kwargs.get("announcement", "")))
        return True

    async def announcement(settings_, org_, **kwargs):
        announced.append("yes")
        return "a.ogg"

    monkeypatch.setattr(lkrec, "start", start_ok)
    monkeypatch.setattr(lkrec, "ensure_announcement", announcement)
    return settings, org_id, started, announced


async def _rows(session, call):
    return (
        await session.execute(sa.select(CallRecording).where(CallRecording.call_id == call.id))
    ).scalars().all()


@pytest.mark.asyncio
async def test_record_calls_starts_recorder_without_announcement_and_queues_row(
    session, mon_settings, tmp_path, monkeypatch
):
    settings, org_id, started, announced = await _setup(session, mon_settings, tmp_path, monkeypatch)
    dispatched = _patch_dispatch(monkeypatch)
    call = await _room_call(session, org_id)

    await monitor_calls.on_livekit_event(session, FakeLiveKit(), settings, _joined(call))
    # participant_joined fires more than once per call: still one recorder, one row.
    await monitor_calls.on_livekit_event(session, FakeLiveKit(), settings, _joined(call))

    await session.refresh(call)
    assert started == [(f"call-{call.id}", "")]
    assert announced == []
    assert dispatched == []
    assert call.extra["customer_recording"] is True
    rows = await _rows(session, call)
    assert len(rows) == 1
    assert rows[0].provider_recording_id == f"lkrec:{call.id}"
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_record_calls_plays_the_announcement_when_the_org_turned_it_on(
    session, mon_settings, tmp_path, monkeypatch
):
    settings, org_id, started, announced = await _setup(
        session, mon_settings, tmp_path, monkeypatch, announce=True
    )
    call = await _room_call(session, org_id)

    await monitor_calls.on_livekit_event(session, FakeLiveKit(), settings, _joined(call))

    assert started == [(f"call-{call.id}", "a.ogg")]


@pytest.mark.asyncio
async def test_announcement_unavailable_means_no_recording(
    session, mon_settings, tmp_path, monkeypatch
):
    settings, org_id, started, _ = await _setup(
        session, mon_settings, tmp_path, monkeypatch, announce=True
    )

    async def unavailable(settings_, org_, **kwargs):
        raise lkrec.AnnouncementUnavailable("down")

    monkeypatch.setattr(lkrec, "ensure_announcement", unavailable)
    call = await _room_call(session, org_id)

    await monitor_calls.on_livekit_event(session, FakeLiveKit(), settings, _joined(call))

    await session.refresh(call)
    assert started == []
    assert "recorder" not in call.extra
    assert await _rows(session, call) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("record,extra", [(False, {}), (True, {"emergency": True})])
async def test_no_recording_when_off_or_emergency(
    session, mon_settings, tmp_path, monkeypatch, record, extra
):
    settings, org_id, started, _ = await _setup(
        session, mon_settings, tmp_path, monkeypatch, record=record
    )
    call = await _room_call(session, org_id, **extra)

    await monitor_calls.on_livekit_event(session, FakeLiveKit(), settings, _joined(call))

    assert started == []
    assert await _rows(session, call) == []


def test_record_calls_setting_round_trip():
    class O:
        calling_settings = None
        recording_announcement = False
        recording_announcement_text = None

    org = O()
    assert calling_settings.as_dict(org)["record_calls"] is False
    calling_settings.apply(org, record_calls=True)
    assert calling_settings.record_calls_for(org) is True
    assert calling_settings.as_dict(org)["record_calls"] is True
