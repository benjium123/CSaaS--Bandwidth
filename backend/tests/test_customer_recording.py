"""Tests for app/services/customer_recording.py: lkrec Ogg/Opus sides -> stereo MP3."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org
from app.models.voice import CallRecording
from app.services import calling_settings, customer_recording, recordings
from app.storage.base import InMemoryObjectStore
from tests.test_p43_call_monitoring import _finished_call, _org, mon_settings, voice  # noqa: F401


async def _row_for_call(session, org_id, call):
    """Refetch the (single, lkrec) CallRecording row for `call`, refreshed from the DB."""
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(CallRecording).where(CallRecording.call_id == call.id))
    ).scalar_one()
    await session.refresh(row)
    return row


async def fake_runner(argv: list[str], timeout: float) -> None:
    fake_runner.calls.append(argv)
    for i, arg in enumerate(argv):
        if arg.endswith(".mp3"):
            name = Path(arg).stem
            Path(arg).write_bytes(b"ID3fake-" + name.encode())


fake_runner.calls = []


@pytest.fixture(autouse=True)
def _reset_fake_runner():
    fake_runner.calls = []
    yield
    fake_runner.calls = []


async def raising_runner(argv: list[str], timeout: float) -> None:
    raise customer_recording.RenderError("boom")


async def _seed_sides(session, store, org_id, call, *, started_at="2026-09-27T10:00:00Z"):
    key_agent = f"org/{org_id}/monitor/{call.id}/p1/agent-a.ogg"
    key_customer = f"org/{org_id}/monitor/{call.id}/p1/customer-sip_x.ogg"
    await store.put(key_agent, b"OggS-agent", "audio/ogg")
    await store.put(key_customer, b"OggS-customer", "audio/ogg")
    call.extra = {
        **call.extra,
        "monitor_recordings": [
            {
                "prefix": "p1",
                "started_at": started_at,
                "files": [
                    {"role": "agent", "key": key_agent, "duration_ms": 61000},
                    {"role": "customer", "key": key_customer, "duration_ms": 59000},
                ],
            }
        ],
    }
    customer_recording.queue(session, call)
    await session.commit()
    return key_agent, key_customer


# --- wanted() -------------------------------------------------------------------------


async def test_wanted_false_when_org_has_no_calling_settings(session):
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.calling_settings = None
    assert await customer_recording.wanted(session, org) is False


async def test_wanted_true_when_record_calls_on_and_no_entitlements_module(session):
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.calling_settings = {"record_calls": True}
    # entitlements module does not exist in the repo, so the import fails and wanted()
    # falls back to True.
    assert await customer_recording.wanted(session, org) is True


async def test_wanted_defers_to_entitlements_module_when_present(session, monkeypatch):
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.calling_settings = {"record_calls": True}

    class FakeEntitlements:
        async def has(self, session_, org_id_, feature):
            return False

    fake = FakeEntitlements()
    import app.services

    monkeypatch.setitem(sys.modules, "app.services.entitlements", fake)
    monkeypatch.setattr(app.services, "entitlements", fake, raising=False)

    assert await customer_recording.wanted(session, org) is False


# --- queue() ----------------------------------------------------------------------------


async def test_queue_creates_pending_row_and_marks_call_extra(session):
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    prior_extra = dict(call.extra)

    row = customer_recording.queue(session, call)
    await session.commit()
    await session.refresh(row)
    await session.refresh(call)

    assert row.provider_recording_id == f"lkrec:{call.id}"
    assert row.status == "pending"
    assert row.content_type == "audio/mpeg"
    assert row.channel_layout == "mixed"
    assert row.storage_key == recordings.storage_key(org_id, row.id)
    for key, value in prior_extra.items():
        assert call.extra[key] == value
    assert call.extra["customer_recording"] is True


# --- render() ---------------------------------------------------------------------------


async def test_render_two_parts_builds_filter_and_mp3_outputs():
    parts = [("agent", b"OggS-agent", 0), ("customer", b"OggS-customer", 1500)]
    stereo, sides = await customer_recording.render(parts, dual=False, runner=fake_runner)

    argv = fake_runner.calls[0]
    assert argv.count("-i") == 2
    filter_idx = argv.index("-filter_complex")
    filter_complex = argv[filter_idx + 1]
    assert "adelay=1500:all=1" in filter_complex
    assert "pan=stereo|c0=c0|c1=0*c0" in filter_complex
    assert "libmp3lame" in argv
    assert stereo == b"ID3fake-stereo"
    assert sides == {}


async def test_render_dual_produces_three_outputs_and_sides():
    parts = [("agent", b"OggS-agent", 0), ("customer", b"OggS-customer", 1500)]
    stereo, sides = await customer_recording.render(parts, dual=True, runner=fake_runner)

    argv = fake_runner.calls[0]
    mp3_outputs = [a for a in argv if a.endswith(".mp3")]
    assert len(mp3_outputs) == 3
    assert stereo == b"ID3fake-stereo"
    assert set(sides.keys()) == {"agent", "customer"}
    assert sides["agent"] == b"ID3fake-agent"
    assert sides["customer"] == b"ID3fake-customer"


async def test_render_agent_only_part_uses_aevalsrc_for_missing_customer():
    parts = [("agent", b"OggS-agent", 0)]
    await customer_recording.render(parts, dual=False, runner=fake_runner)

    argv = fake_runner.calls[0]
    filter_complex = argv[argv.index("-filter_complex") + 1]
    assert "aevalsrc" in filter_complex


async def test_render_raises_on_empty_parts():
    with pytest.raises(customer_recording.RenderError):
        await customer_recording.render([], dual=False, runner=fake_runner)


# --- duration_seconds() ------------------------------------------------------------------


def test_duration_seconds_takes_the_latest_end_or_none_without_durations():
    assert customer_recording.duration_seconds(
        [("agent", "k", 0, 61000), ("customer", "k", 2000, 60000)]
    ) == 62
    assert customer_recording.duration_seconds(
        [("agent", "k", 0, None), ("customer", "k", 2000, None)]
    ) is None


# --- finalize_tick() ----------------------------------------------------------------------


async def test_finalize_tick_leaves_freshly_ended_call_pending(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    now = datetime.now(timezone.utc)
    call.ended_at = now - timedelta(seconds=5)
    await session.commit()
    key_agent, key_customer = await _seed_sides(session, store, org_id, call)

    counts = await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=fake_runner)

    assert counts["stored"] == 0
    row = await _row_for_call(session, org_id, call)
    assert row.status == "pending"


async def test_finalize_tick_stores_unmonitored_call_and_purges_ogg_sides(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    call.extra = {k: v for k, v in call.extra.items() if k != "monitor"}
    await session.commit()
    key_agent, key_customer = await _seed_sides(session, store, org_id, call)
    now = datetime.now(timezone.utc)

    counts = await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=fake_runner)

    assert counts["stored"] == 1
    row = await _row_for_call(session, org_id, call)
    await session.refresh(call)

    assert row.status == "stored"
    assert await store.get(row.storage_key) == b"ID3fake-stereo"
    assert row.duration_seconds == 61
    with pytest.raises(KeyError):
        await store.get(key_agent)
    with pytest.raises(KeyError):
        await store.get(key_customer)
    assert call.extra["monitor_recordings_purged"] is True


async def test_finalize_tick_keeps_ogg_sides_for_a_monitored_call(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)  # extra keeps "monitor": "new_account"
    await session.commit()
    key_agent, key_customer = await _seed_sides(session, store, org_id, call)
    now = datetime.now(timezone.utc)

    counts = await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=fake_runner)

    assert counts["stored"] == 1
    row = await _row_for_call(session, org_id, call)

    assert row.status == "stored"
    assert await store.get(key_agent) == b"OggS-agent"
    assert await store.get(key_customer) == b"OggS-customer"


async def test_finalize_tick_dual_layout_writes_agent_and_customer_side_files(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    calling_settings.apply(org, channel_layout="dual")
    await session.commit()

    call = await _finished_call(session, org_id)
    call.extra = {k: v for k, v in call.extra.items() if k != "monitor"}
    await session.commit()
    await _seed_sides(session, store, org_id, call)
    now = datetime.now(timezone.utc)

    counts = await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=fake_runner)

    assert counts["stored"] == 1
    row = await _row_for_call(session, org_id, call)

    assert row.channel_layout == "dual"
    assert await store.get(recordings.layout_storage_key(row, "agent")) == b"ID3fake-agent"
    assert await store.get(recordings.layout_storage_key(row, "customer")) == b"ID3fake-customer"


async def test_finalize_tick_fails_call_with_no_monitor_recordings_after_giving_up(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    now = datetime.now(timezone.utc)
    call.ended_at = now - timedelta(hours=2)
    await session.commit()
    customer_recording.queue(session, call)
    await session.commit()

    counts = await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=fake_runner)

    assert counts["failed"] == 1
    row = await _row_for_call(session, org_id, call)
    assert row.status == "failed"


async def test_finalize_tick_fails_when_runner_raises(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    call.extra = {k: v for k, v in call.extra.items() if k != "monitor"}
    await session.commit()
    await _seed_sides(session, store, org_id, call)
    now = datetime.now(timezone.utc)

    counts = await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=raising_runner)

    assert counts["failed"] == 1
    row = await _row_for_call(session, org_id, call)
    assert row.status == "failed"


async def test_finalize_tick_leaves_carrier_recordings_untouched(session, mon_settings):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    await session.commit()
    set_org_context(session, org_id)
    carrier_row = CallRecording(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_recording_id="carrier-1",
        storage_key="x",
        status="pending",
    )
    session.add(carrier_row)
    await session.commit()
    now = datetime.now(timezone.utc)

    await customer_recording.finalize_tick(session, mon_settings, store, now=now, runner=fake_runner)

    set_org_context(session, org_id)
    await session.refresh(carrier_row)
    assert carrier_row.status == "pending"


# --- real ffmpeg (skipped when ffmpeg is not installed) -----------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_render_with_real_ffmpeg_puts_agent_audio_on_the_left_channel(tmp_path):
    tone_path = tmp_path / "tone.ogg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", "-f", "ogg", str(tone_path)],
        check=True, capture_output=True,
    )
    tone = tone_path.read_bytes()

    stereo, sides = await customer_recording.render([("agent", tone, 0)], dual=False)
    assert sides == {}

    mp3_path = tmp_path / "stereo.mp3"
    mp3_path.write_bytes(stereo)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3_path), "-f", "s16le", "-ac", "2", "-ar", "16000", "-"],
        check=True,
        capture_output=True,
    )
    pcm = array("h")
    pcm.frombytes(proc.stdout)
    left = sum(abs(s) for s in pcm[0::2])
    right = sum(abs(s) for s in pcm[1::2])
    assert left > 20 * (right + 1)


async def test_dual_layout_works_on_the_local_disk_store_and_erasure_keys_cover_it(
    session, mon_settings, tmp_path
):
    """Regression (first live test call): on local disk the mixed file IS the storage_key
    path, so side files must be siblings, not children - and erasure must find them."""
    from app.storage.base import LocalFSObjectStore

    store = LocalFSObjectStore(tmp_path)
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    calling_settings.apply(org, channel_layout="dual")
    await session.commit()
    call = await _finished_call(session, org_id)
    call.extra = {k: v for k, v in call.extra.items() if k != "monitor"}
    await session.commit()
    await _seed_sides(session, store, org_id, call)

    counts = await customer_recording.finalize_tick(
        session, mon_settings, store, now=datetime.now(timezone.utc), runner=fake_runner
    )

    assert counts["stored"] == 1
    row = await _row_for_call(session, org_id, call)
    keys = recordings.all_storage_keys(row)
    assert keys == [row.storage_key, f"{row.storage_key}.agent", f"{row.storage_key}.customer"]
    for key in keys:
        assert await store.get(key)
    for key in keys:
        await store.delete(key)
    for key in keys:
        with pytest.raises(KeyError):
            await store.get(key)
