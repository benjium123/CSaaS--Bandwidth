from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models.voice import Call, CallRecording, TranscriptionJob
from app.services import calling_settings, recordings

log = structlog.get_logger('transcription')

TIERS = ('on_request', 'soon', 'night')
SOON_DEADLINE = timedelta(minutes=10)
ON_REQUEST_DEADLINE = timedelta(minutes=2)
MIN_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=5)
STUCK_AFTER = timedelta(minutes=30)
NIGHT_TZ = 'America/New_York'
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 8

_TIER_PRIORITY = {tier: index for index, tier in enumerate(TIERS)}


def enabled(settings) -> bool:
    return bool(settings.stt_url)


def auth_token(settings) -> str:
    secret = settings.livekit_api_secret
    value = secret.get_secret_value() if hasattr(secret, 'get_secret_value') else str(secret)
    return hashlib.sha256(f'stt:{value}'.encode()).hexdigest()


def _to_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def in_night_window(now) -> bool:
    zone = ZoneInfo(NIGHT_TZ)
    local = now.astimezone(zone) if now.tzinfo is not None else now.replace(tzinfo=timezone.utc).astimezone(zone)
    return NIGHT_START_HOUR <= local.hour < NIGHT_END_HOUR


def night_deadline(now) -> datetime:
    zone = ZoneInfo(NIGHT_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(zone)
    candidate = local.replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _deadline_for(tier, now):
    if tier == 'on_request':
        return now + ON_REQUEST_DEADLINE
    if tier == 'soon':
        return now + SOON_DEADLINE
    if tier == 'night':
        return night_deadline(now)
    return now + SOON_DEADLINE


def _tier_rank(tier):
    return _TIER_PRIORITY.get(tier, len(TIERS))


async def wanted(session, org) -> str | None:
    mode = calling_settings.transcription_mode_for(org)
    if mode == 'off':
        return None
    try:
        from app.services import entitlements
    except ImportError:
        return mode
    if not await entitlements.has(session, org.id, 'call_transcription'):
        return None
    return mode


async def entitled(session, org) -> bool:
    try:
        from app.services import entitlements
    except ImportError:
        return True
    return bool(await entitlements.has(session, org.id, 'call_transcription'))


async def enqueue(session, call, recording, *, tier, now=None) -> TranscriptionJob:
    now = now or datetime.now(timezone.utc)

    stmt = (
        sa.select(TranscriptionJob)
        .where(TranscriptionJob.call_id == call.id)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    existing = (await session.execute(stmt)).scalars().first()

    if recording.duration_seconds is not None and recording.duration_seconds < MIN_SECONDS:
        if existing is not None and existing.status in ('running', 'done'):
            return existing
        if existing is None:
            job = TranscriptionJob(
                org_id=call.org_id,
                call_id=call.id,
                recording_id=recording.id,
                tier=tier,
                status='skipped',
                error='too short',
            )
            session.add(job)
            return job
        existing.status = 'skipped'
        existing.error = 'too short'
        existing.deadline = None
        existing.not_before = None
        return existing

    if existing is None:
        job = TranscriptionJob(
            org_id=call.org_id,
            call_id=call.id,
            recording_id=recording.id,
            tier=tier,
            status='queued',
            attempts=0,
            error=None,
            deadline=_deadline_for(tier, now),
            not_before=None,
        )
        session.add(job)
        return job

    if existing.status == 'running' or existing.status == 'done':
        return existing

    if existing.status == 'queued':
        if _tier_rank(tier) < _tier_rank(existing.tier):
            existing.tier = tier
            existing.deadline = _deadline_for(tier, now)
        return existing

    if existing.status in ('failed', 'skipped') and tier == 'on_request':
        existing.status = 'queued'
        existing.recording_id = recording.id
        existing.tier = tier
        existing.attempts = 0
        existing.error = None
        existing.deadline = _deadline_for(tier, now)
        existing.not_before = None

    return existing


def pressure(load1: float, cpus: int, live_calls: int) -> float:
    return (load1 + 0.05 * live_calls) / max(cpus, 1)


def ramping(load1: float, load5: float, cpus: int) -> bool:
    return load1 > 1.25 * load5 and load1 / max(cpus, 1) > 0.3


def pick_engine(tier, p, engines, *, overdue: bool) -> str | None:
    engines = engines or []
    if tier == 'night':
        if 'zipformer' in engines:
            return 'zipformer'
        if 'parakeet' in engines:
            return 'parakeet'
        return None

    if p < 0.45 and not overdue and 'parakeet' in engines:
        return 'parakeet'
    if 'zipformer' in engines:
        return 'zipformer'
    if 'parakeet' in engines:
        return 'parakeet'
    return None


def allowed_tiers(p, is_ramping, night: bool) -> list[str]:
    tiers = []
    if p < 0.9:
        tiers.append('on_request')
    if p < 0.6 and not is_ramping:
        tiers.append('soon')
    if not is_ramping and ((night and p < 0.75) or p < 0.3):
        tiers.append('night')
    return tiers


def _db_now(session, now):
    if session.get_bind().dialect.name == 'sqlite':
        return now.replace(tzinfo=None)
    return now


async def _handle_transcribe_failure(session, job, error_text, now):
    if job.attempts >= MAX_ATTEMPTS:
        job.status = 'failed'
        job.error = (error_text or f'attempt {job.attempts}')[:255]
    else:
        job.status = 'queued'
        job.error = None
        job.not_before = _db_now(session, now + RETRY_AFTER)
    await session.commit()


async def tick(session, settings, store, *, now=None, client=None, loadavg=None, cpus=None) -> dict:
    if not enabled(settings):
        return {}

    now = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    stuck_cutoff = _db_now(session, now - STUCK_AFTER)
    stuck_stmt = (
        sa.select(TranscriptionJob)
        .where(
            TranscriptionJob.status == 'running',
            TranscriptionJob.started_at.is_not(None),
            TranscriptionJob.started_at < stuck_cutoff,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    stuck_rows = (await session.execute(stuck_stmt)).scalars().all()
    for job in stuck_rows:
        set_org_context(session, job.org_id)
        job.status = 'queued'
        job.started_at = None
        counts['requeued'] = counts.get('requeued', 0) + 1
    if stuck_rows:
        await session.commit()

    owns_client = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=5.0)

    try:
        health_url = settings.stt_url.rstrip('/') + '/health'
        health_headers = {'Authorization': f'Bearer {auth_token(settings)}'}
        try:
            health_resp = await http.get(health_url, headers=health_headers, timeout=5.0)
            if health_resp.status_code != 200:
                counts['unreachable'] = 1
                return {k: v for k, v in counts.items() if v}
            health = health_resp.json()
            busy = int(health.get('busy', 0))
            capacity = int(health.get('capacity', 0))
        except Exception:
            counts['unreachable'] = 1
            return {k: v for k, v in counts.items() if v}

        free = capacity - busy
        if free <= 0:
            counts['busy'] = 1
            return {k: v for k, v in counts.items() if v}

        engines = health.get('engines') or []

        if loadavg is None:
            loadavg = os.getloadavg()
        load1 = float(loadavg[0])
        load5 = float(loadavg[1]) if len(loadavg) > 1 else load1
        if cpus is None:
            cpus = os.cpu_count() or 1
        cpus = int(cpus)

        live_stmt = (
            sa.select(sa.func.count())
            .select_from(Call)
            .where(
                Call.status.in_(('queued', 'ringing', 'answered', 'in_progress')),
                Call.ended_at.is_(None),
                # A row that never got its end event must not look "live" forever.
                Call.created_at > _db_now(session, now - timedelta(hours=6)),
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
        live_calls = int((await session.execute(live_stmt)).scalar_one())

        p = pressure(load1, cpus, live_calls)
        is_ramping = ramping(load1, load5, cpus)
        night = in_night_window(now)
        tiers = allowed_tiers(p, is_ramping, night)

        if not tiers:
            queued_count_stmt = (
                sa.select(sa.func.count())
                .select_from(TranscriptionJob)
                .where(TranscriptionJob.status == 'queued')
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
            queued_count = int((await session.execute(queued_count_stmt)).scalar_one())
            if queued_count:
                counts['deferred'] = queued_count
            return {k: v for k, v in counts.items() if v}

        db_now = _db_now(session, now)
        tier_case = sa.case(
            (TranscriptionJob.tier == 'on_request', 0),
            (TranscriptionJob.tier == 'soon', 1),
            (TranscriptionJob.tier == 'night', 2),
            else_=3,
        )
        job_stmt = (
            sa.select(TranscriptionJob)
            .where(
                TranscriptionJob.status == 'queued',
                TranscriptionJob.tier.in_(tiers),
                sa.or_(
                    TranscriptionJob.not_before.is_(None),
                    TranscriptionJob.not_before <= db_now,
                ),
            )
            .order_by(tier_case, TranscriptionJob.created_at)
            .limit(free)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
        jobs = (await session.execute(job_stmt)).scalars().all()

        for job in jobs:
            set_org_context(session, job.org_id)
            try:
                rec = await session.get(CallRecording, job.recording_id)
                if rec is None or rec.status != 'stored':
                    job.status = 'failed'
                    job.error = 'no recording'
                    await session.commit()
                    counts['failed'] = counts.get('failed', 0) + 1
                    continue

                call = await session.get(Call, job.call_id)
                if call is None:
                    job.status = 'failed'
                    job.error = 'call missing'
                    await session.commit()
                    counts['failed'] = counts.get('failed', 0) + 1
                    continue

                overdue = job.deadline is not None and _to_utc(job.deadline) < now
                engine = pick_engine(job.tier, p, engines, overdue=overdue)
                if engine is None:
                    continue

                job.status = 'running'
                job.engine = engine
                job.attempts += 1
                job.started_at = _db_now(session, now)
                await session.commit()

                try:
                    audio = await recordings.load_recording_bytes(store, rec)
                except KeyError:
                    job.status = 'failed'
                    job.error = 'recording missing'
                    await session.commit()
                    counts['failed'] = counts.get('failed', 0) + 1
                    continue

                headers = {
                    'Authorization': f'Bearer {auth_token(settings)}',
                    'Content-Type': rec.content_type or 'audio/mpeg',
                }
                transcribe_url = settings.stt_url.rstrip('/') + f'/transcribe?engine={engine}'

                try:
                    resp = await http.post(transcribe_url, content=audio, headers=headers, timeout=900.0)
                except httpx.HTTPError as exc:
                    await _handle_transcribe_failure(session, job, str(exc) or 'httpx error', now)
                    if job.status == 'failed':
                        counts['failed'] = counts.get('failed', 0) + 1
                    continue

                if resp.status_code == 503:
                    job.status = 'queued'
                    job.attempts = max(0, job.attempts - 1)
                    job.started_at = None
                    await session.commit()
                    break

                if resp.status_code != 200:
                    await _handle_transcribe_failure(session, job, resp.text or f'HTTP {resp.status_code}', now)
                    if job.status == 'failed':
                        counts['failed'] = counts.get('failed', 0) + 1
                    continue

                data = resp.json()
                segments = data.get('segments', [])
                channels = data.get('channels', 1)
                rows = []
                for segment in segments:
                    if not isinstance(segment, dict):
                        continue
                    channel = segment.get('channel', 0)
                    if channels == 1:
                        role = 'user'
                    else:
                        role = 'agent' if channel == 0 else 'user'
                    rows.append(
                        SimpleNamespace(
                            role=role,
                            text=str(segment.get('text', '')),
                            at_ms=int(segment.get('start_ms', 0)),
                        )
                    )

                from app.services import agent as agent_svc

                await agent_svc.upsert_transcript_segments(session, call, rows)

                job.status = 'done'
                job.audio_seconds = int(data.get('audio_sec', 0))
                job.cpu_ms = int(float(data.get('cpu_sec', 0)) * 1000)
                job.finished_at = _db_now(session, now)
                job.error = None
                # Transcript minutes (P46 P2b): $0 + a price_unset alert while unset.
                from app.services import telephony_billing

                await telephony_billing.charge_feature_minutes(
                    session,
                    job.org_id,
                    'transcription_min',
                    job.audio_seconds,
                    reference=f'stt:{job.id}',
                    note=f'{job.audio_seconds}s call transcript',
                    feature='call_transcription',
                )
                await session.commit()
                counts['done'] = counts.get('done', 0) + 1

            except Exception:
                log.exception('transcription_job_failed', job_id=str(job.id))
                await session.rollback()
                try:
                    set_org_context(session, job.org_id)
                    await session.execute(
                        sa.update(TranscriptionJob)
                        .where(TranscriptionJob.id == job.id)
                        .values(status='queued', not_before=_db_now(session, now + RETRY_AFTER))
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    continue

        return {k: v for k, v in counts.items() if v}

    finally:
        if owns_client:
            await http.aclose()
