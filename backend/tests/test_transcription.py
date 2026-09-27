from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa
from pydantic import SecretStr

from app.db.base import set_org_context
from app.models import Org
from app.models.voice import CallRecording, TranscriptionJob
from app.services import recordings, transcription
from app.storage.base import InMemoryObjectStore
from tests.test_p43_call_monitoring import _finished_call, _org, mon_settings, voice  # noqa: F401

EASTERN = ZoneInfo('America/New_York')


def _utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _settings():
    return SimpleNamespace(
        stt_url='http://stt:9100',
        livekit_api_secret=SecretStr('s' * 40),
    )


def _stt_handler(
    *,
    busy=0,
    capacity=1,
    engines=None,
    health_status=200,
    transcribe_status=200,
    transcribe_json=None,
):
    engines = list(engines or ['parakeet', 'zipformer'])
    default_json = {
        'engine': 'parakeet',
        'channels': 2,
        'audio_sec': 60,
        'cpu_sec': 1.5,
        'wall_sec': 2.0,
        'segments': [
            {'channel': 0, 'start_ms': 0, 'end_ms': 1000, 'text': 'agent hello'},
            {'channel': 1, 'start_ms': 1000, 'end_ms': 2000, 'text': 'customer hello'},
        ],
    }

    async def handler(request):
        if request.url.path == '/health':
            return httpx.Response(
                health_status,
                json={'busy': busy, 'capacity': capacity, 'engines': engines},
            )
        if request.url.path == '/transcribe':
            if transcribe_status == 503:
                return httpx.Response(503, json={'error': 'busy'})
            if transcribe_status >= 400:
                return httpx.Response(transcribe_status, json={'error': 'boom'})
            return httpx.Response(200, json=transcribe_json or default_json)
        return httpx.Response(404)

    return handler


def _client_for(**kwargs):
    return httpx.AsyncClient(transport=httpx.MockTransport(_stt_handler(**kwargs)))


async def _stored_recording(session, store, org_id, call, *, duration_seconds=60):
    set_org_context(session, org_id)
    rec_id = uuid.uuid4()
    rec = CallRecording(
        id=rec_id,
        org_id=org_id,
        call_id=call.id,
        provider_recording_id=f'lkrec:{call.id}',
        storage_key=recordings.storage_key(org_id, rec_id),
        content_type='audio/mpeg',
        status='stored',
        duration_seconds=duration_seconds,
    )
    session.add(rec)
    await session.commit()
    await store.put(rec.storage_key, b'ID3fake', 'audio/mpeg')
    return rec


async def _add_job(session, call, rec, *, tier='soon', status='queued', **kwargs):
    set_org_context(session, rec.org_id)
    job = TranscriptionJob(
        org_id=rec.org_id,
        call_id=call.id,
        recording_id=rec.id,
        tier=tier,
        status=status,
        **kwargs,
    )
    session.add(job)
    await session.commit()
    return job


async def _transcript_rows(session, call_id):
    from app.models import CallTranscriptSegment

    result = await session.execute(
        sa.select(CallTranscriptSegment.role, CallTranscriptSegment.at_ms)
        .where(CallTranscriptSegment.call_id == call_id)
        .order_by(CallTranscriptSegment.at_ms)
    )
    return [tuple(row) for row in result.all()]


# --- pure helpers ----------------------------------------------------------------


def test_pressure_and_ramping():
    assert transcription.pressure(0.5, 8, 0) == 0.5 / 8
    assert transcription.pressure(4.0, 8, 4) == (4.0 + 0.05 * 4) / 8
    assert transcription.ramping(4.0, 1.0, 8) is True
    assert transcription.ramping(4.0, 4.0, 8) is False


def test_pick_engine():
    # Parakeet removed 2026-09-27: Zipformer is the only local engine.
    engines = ['zipformer']
    assert transcription.pick_engine('night', 0.8, engines, overdue=False) == 'zipformer'
    assert transcription.pick_engine('night', 0.8, ['parakeet'], overdue=False) is None
    assert transcription.pick_engine('night', 0.8, [], overdue=False) is None
    assert transcription.pick_engine('soon', 0.4, engines, overdue=False) == 'zipformer'
    assert transcription.pick_engine('soon', 0.4, engines, overdue=True) == 'zipformer'


def test_allowed_tiers():
    assert transcription.allowed_tiers(0.89, False, False) == ['on_request']
    assert transcription.allowed_tiers(0.59, False, False) == ['on_request', 'soon']
    assert transcription.allowed_tiers(0.29, False, False) == ['on_request', 'soon', 'night']
    assert transcription.allowed_tiers(0.29, True, True) == ['on_request']
    assert transcription.allowed_tiers(0.9375, False, False) == []


def test_in_night_window():
    assert transcription.in_night_window(datetime(2026, 9, 27, 1, 0, tzinfo=EASTERN)) is True
    assert transcription.in_night_window(datetime(2026, 9, 27, 12, 0, tzinfo=EASTERN)) is False


def test_night_deadline():
    at_1am = datetime(2026, 9, 27, 1, 0, tzinfo=EASTERN)
    expected_same_day = datetime(2026, 9, 27, 8, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    assert transcription.night_deadline(at_1am) == expected_same_day

    at_noon = datetime(2026, 9, 27, 12, 0, tzinfo=EASTERN)
    expected_next_day = datetime(2026, 9, 28, 8, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    assert transcription.night_deadline(at_noon) == expected_next_day


# --- enqueue ----------------------------------------------------------------------


async def test_enqueue_skips_short_recordings(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call, duration_seconds=9)

    set_org_context(session, org_id)
    now = datetime.now(timezone.utc)
    job = await transcription.enqueue(session, call, rec, tier='soon', now=now)
    await session.commit()
    await session.refresh(job)

    assert job.status == 'skipped'
    assert job.error == 'too short'


async def test_enqueue_upgrades_soon_to_on_request(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)

    set_org_context(session, org_id)
    now = datetime.now(timezone.utc)
    job1 = await transcription.enqueue(session, call, rec, tier='soon', now=now)
    await session.commit()

    job2 = await transcription.enqueue(session, call, rec, tier='on_request', now=now)
    await session.commit()
    await session.refresh(job2)

    assert job2.id == job1.id
    assert job2.tier == 'on_request'
    assert _utc(job2.deadline) == now + transcription.ON_REQUEST_DEADLINE


async def test_enqueue_night_does_not_downgrade_soon(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)

    set_org_context(session, org_id)
    now = datetime.now(timezone.utc)
    job1 = await transcription.enqueue(session, call, rec, tier='soon', now=now)
    await session.commit()

    job2 = await transcription.enqueue(session, call, rec, tier='night', now=now)
    await session.commit()
    await session.refresh(job2)

    assert job2.id == job1.id
    assert job2.tier == 'soon'


async def test_enqueue_done_job_not_rerun(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='night', status='done')

    set_org_context(session, org_id)
    now = datetime.now(timezone.utc)
    returned = await transcription.enqueue(session, call, rec, tier='on_request', now=now)
    await session.commit()
    await session.refresh(returned)

    assert returned.id == job.id
    assert returned.status == 'done'
    assert returned.tier == 'night'


async def test_enqueue_failed_on_request_requeues(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='night', status='failed', attempts=2, error='old')

    set_org_context(session, org_id)
    now = datetime.now(timezone.utc)
    returned = await transcription.enqueue(session, call, rec, tier='on_request', now=now)
    await session.commit()
    await session.refresh(returned)

    assert returned.id == job.id
    assert returned.status == 'queued'
    assert returned.tier == 'on_request'
    assert returned.attempts == 0
    assert returned.error is None
    assert returned.not_before is None


# --- wanted() ---------------------------------------------------------------------


async def test_wanted_mode_off_returns_none(session):
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.calling_settings = {'transcription_mode': 'off'}

    assert await transcription.wanted(session, org) is None


async def test_wanted_after_call_needs_the_call_transcription_entitlement(session):
    from app.services import entitlements

    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.calling_settings = {'transcription_mode': 'after_call'}
    # The workspace's own mode is not enough: ops must enable call_transcription (P2).
    assert await transcription.wanted(session, org) is None
    await entitlements.set_feature(
        session, org_id, 'call_transcription',
        enabled=True, price_override_micros=None, actor_user_id=None,
    )
    assert await transcription.wanted(session, org) == 'after_call'


# --- tick -------------------------------------------------------------------------


async def test_tick_happy_path_zipformer_at_low_load(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='soon', status='queued')

    settings = _settings()
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    client = _client_for()
    async with client:
        counts = await transcription.tick(
            session,
            settings,
            store,
            now=now,
            client=client,
            loadavg=(0.5, 0.5, 0.5),
            cpus=8,
        )

    assert counts == {'done': 1}
    assert await _transcript_rows(session, call.id) == [('agent', 0), ('user', 1000)]

    set_org_context(session, org_id)
    await session.refresh(job)
    assert job.status == 'done'
    assert job.engine == 'zipformer'
    assert job.audio_seconds == 60
    assert job.cpu_ms == 1500


async def test_tick_happy_path_zipformer_at_p_0_5(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='on_request', status='queued')

    settings = _settings()
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    client = _client_for()
    async with client:
        counts = await transcription.tick(
            session,
            settings,
            store,
            now=now,
            client=client,
            loadavg=(4.0, 4.0, 4.0),
            cpus=8,
        )

    assert counts == {'done': 1}
    set_org_context(session, org_id)
    await session.refresh(job)
    assert job.status == 'done'
    assert job.engine == 'zipformer'


async def test_tick_night_job_deferred_at_noon_but_runs_at_1am(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='night', status='queued')

    settings = _settings()
    noon_utc = datetime(2026, 9, 27, 12, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    night_utc = datetime(2026, 9, 28, 1, 0, tzinfo=EASTERN).astimezone(timezone.utc)
    client = _client_for()

    async with client:
        counts_noon = await transcription.tick(
            session,
            settings,
            store,
            now=noon_utc,
            client=client,
            loadavg=(4.0, 4.0, 4.0),
            cpus=8,
        )
        assert counts_noon == {}
        assert await _transcript_rows(session, call.id) == []

        set_org_context(session, org_id)
        await session.refresh(job)
        assert job.status == 'queued'

        counts_night = await transcription.tick(
            session,
            settings,
            store,
            now=night_utc,
            client=client,
            loadavg=(4.0, 4.0, 4.0),
            cpus=8,
        )
        assert counts_night == {'done': 1}


async def test_tick_high_load_defers_soon_and_runs_on_request(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)

    call_soon = await _finished_call(session, org_id)
    rec_soon = await _stored_recording(session, store, org_id, call_soon)
    job_soon = await _add_job(session, call_soon, rec_soon, tier='soon', status='queued')

    call_onreq = await _finished_call(session, org_id)
    rec_onreq = await _stored_recording(session, store, org_id, call_onreq)
    job_onreq = await _add_job(session, call_onreq, rec_onreq, tier='on_request', status='queued')

    settings = _settings()
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    client = _client_for(capacity=1)
    async with client:
        counts = await transcription.tick(
            session,
            settings,
            store,
            now=now,
            client=client,
            loadavg=(7.0, 7.0, 7.0),
            cpus=8,
        )

    assert counts == {'done': 1}

    set_org_context(session, org_id)
    await session.refresh(job_onreq)
    await session.refresh(job_soon)
    assert job_onreq.status == 'done'
    assert job_soon.status == 'queued'


async def test_tick_503_requeues_job(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='on_request', status='queued')

    settings = _settings()
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    client = _client_for(transcribe_status=503)
    async with client:
        counts = await transcription.tick(
            session,
            settings,
            store,
            now=now,
            client=client,
            loadavg=(0.5, 0.5, 0.5),
            cpus=8,
        )

    assert counts == {}
    set_org_context(session, org_id)
    await session.refresh(job)
    assert job.status == 'queued'
    assert job.attempts == 0


async def test_tick_http_500_three_times_fails_job(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    job = await _add_job(session, call, rec, tier='on_request', status='queued')

    settings = _settings()
    base = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    now = base
    client = _client_for(transcribe_status=500)

    async with client:
        for attempt in range(3):
            await transcription.tick(
                session,
                settings,
                store,
                now=now,
                client=client,
                loadavg=(0.5, 0.5, 0.5),
                cpus=8,
            )
            set_org_context(session, org_id)
            await session.refresh(job)
            if attempt < 2:
                assert job.status == 'queued'
                assert job.attempts == attempt + 1
                now += transcription.RETRY_AFTER + timedelta(seconds=1)
            else:
                assert job.status == 'failed'
                assert 'boom' in (job.error or '')


async def test_tick_health_unreachable(session):
    settings = _settings()
    store = InMemoryObjectStore()
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    client = _client_for(health_status=500)

    async with client:
        counts = await transcription.tick(session, settings, store, now=now, client=client)

    assert counts == {'unreachable': 1}


async def test_tick_stuck_running_job_requeued(session):
    store = InMemoryObjectStore()
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    rec = await _stored_recording(session, store, org_id, call)
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    job = await _add_job(
        session,
        call,
        rec,
        tier='night',
        status='running',
        started_at=now - transcription.STUCK_AFTER - timedelta(seconds=1),
    )

    settings = _settings()
    client = _client_for()
    async with client:
        counts = await transcription.tick(
            session,
            settings,
            store,
            now=now,
            client=client,
            loadavg=(4.0, 4.0, 4.0),
            cpus=8,
        )

    assert counts == {'requeued': 1}
    set_org_context(session, org_id)
    await session.refresh(job)
    assert job.status == 'queued'
