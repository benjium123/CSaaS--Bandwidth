"""Call recordings: carrier-authenticated fetch, our own storage, our own auth.

Same discipline as services/media.py's inbound-MMS re-hosting (P3): credentials for the
download go ONLY to the carrier's own host - never to a foreign one just because a webhook
payload named it - and the carrier's URL is never handed to the UI. It expires, it needs
carrier credentials, and serving it back would leak those.

F6/F7/F16 moved the actual fetch OUT of the webhook path and onto the sweeper, mirroring
services/media.py's inbound-MMS pattern exactly: `on_recording_ready` only upserts a
`pending` CallRecording row (zero network I/O - ARCHITECTURE D6's webhook-path constraint
was being violated before this fix), and `fetch_pending_recordings` is what a sweeper pass
actually drives. The one wrinkle: CallRecording has nowhere to put the carrier's URL (adding
a column is out of scope here, and putting a carrier-authenticated link one query away from
the API/UI is exactly the leak this module exists to prevent), so the fetch re-derives it
from the `voice_events` row the webhook already ledgered, by re-parsing that row's stored
payload through the owning carrier's own `parse_voice_webhook`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError
from app.models.voice import Call, CallLeg, CallRecording
from app.models.voice import VoiceEvent as VoiceEventRow
from app.providers.voice import VoiceEvent, as_voice_carrier

log = structlog.get_logger("recordings")

#: Refuse anything larger; a call recording has no business anywhere near this.
MAX_RECORDING_BYTES = 100 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10.0

ALLOWED_RECORDING_CONTENT_TYPES = frozenset(
    {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"}
)
_OCTET_STREAM = "application/octet-stream"

#: F6 retry bookkeeping WITHOUT a schema change: CallRecording gets no fetch_attempts /
#: next_attempt_at columns (unlike MediaAsset) - staleness is read off TimestampMixin's own
#: `updated_at` instead. A "failed" row is eligible again once it has sat untouched for
#: RETRY_BACKOFF_SECONDS, and abandoned for good - left "failed" forever, never selected
#: again - once it has sat untouched past GIVE_UP_AFTER_SECONDS.
RETRY_BACKOFF_SECONDS = 60
GIVE_UP_AFTER_SECONDS = 24 * 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def storage_key(org_id: uuid.UUID, recording_id: uuid.UUID) -> str:
    return f"org/{org_id}/recordings/{recording_id}"


async def on_recording_ready(
    session: AsyncSession,
    event: VoiceEvent,
    call: Call,
    leg: CallLeg | None,
) -> CallRecording | None:
    """Upsert the CallRecording row for one `recording_ready` event. NO network I/O here -
    the webhook path stays DB-only (ARCHITECTURE D6). `fetch_pending_recordings` (driven by
    the sweeper) does the actual download.

    The row is upserted (dedupe on provider_recording_id, same nested-savepoint /
    IntegrityError pattern as the voice_events dedupe) - a redelivered `recording_ready`
    must find the row that already exists rather than fail, and must never downgrade an
    already-"stored" row back to "pending".
    """
    if not event.provider_recording_id:
        log.warning("recording_ready_missing_provider_id", call_id=str(call.id))
        return None

    recording_id = uuid.uuid4()
    recording = CallRecording(
        id=recording_id,
        org_id=call.org_id,
        call_id=call.id,
        leg_id=leg.id if leg is not None else None,
        provider_recording_id=event.provider_recording_id,
        # storage_key is NOT NULL and deterministic from (org, recording id) - computed
        # upfront so the "pending" row is valid even before anything is actually stored.
        storage_key=storage_key(call.org_id, recording_id),
        status="pending",
    )
    try:
        async with session.begin_nested():
            session.add(recording)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                sa.select(CallRecording).where(
                    CallRecording.provider_recording_id == event.provider_recording_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - the constraint guarantees this
            raise
        return existing
    return recording


async def _fail(recording: CallRecording, reason: str) -> bool:
    recording.status = "failed"
    log.error("recording_fetch_failed", recording_id=str(recording.id), reason=reason)
    return False


async def fetch_pending_recordings(
    session: AsyncSession,
    store,  # noqa: ANN001 - ObjectStore protocol (app/storage/base.py)
    registry,  # noqa: ANN001 - CarrierRegistry
    *,
    client: httpx.AsyncClient | None = None,
    limit: int = 25,
    now: datetime | None = None,
) -> int:
    """Sweeper-driven: download and store every recording whose CallRecording row is still
    `pending`, or `failed` but past its retry backoff (see RETRY_BACKOFF_SECONDS /
    GIVE_UP_AFTER_SECONDS above). Never called from the webhook path.
    """
    moment = now or _now()
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    bind_moment = moment.replace(tzinfo=None) if is_sqlite else moment
    retry_cutoff = bind_moment - timedelta(seconds=RETRY_BACKOFF_SECONDS)
    giveup_cutoff = bind_moment - timedelta(seconds=GIVE_UP_AFTER_SECONDS)

    stmt = (
        sa.select(CallRecording)
        .where(
            sa.or_(
                CallRecording.status == "pending",
                sa.and_(
                    CallRecording.status == "failed",
                    CallRecording.updated_at <= retry_cutoff,
                    CallRecording.updated_at > giveup_cutoff,
                ),
            )
        )
        .order_by(CallRecording.created_at)
        .limit(limit)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    pending = list((await session.execute(stmt)).scalars().all())
    if not pending:
        return 0

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False)
    fetched = 0
    try:
        for recording in pending:
            set_org_context(session, recording.org_id)
            ok = await _fetch_one_recording(session, store, registry, client, recording)
            fetched += 1 if ok else 0
        await session.commit()
    finally:
        if owns_client:
            await client.aclose()
    return fetched


async def _resolve_recording_source(
    session: AsyncSession, registry, recording: CallRecording  # noqa: ANN001
) -> tuple[object, VoiceEvent] | tuple[None, None]:
    """Re-derive (adapter, VoiceEvent) for a pending recording from the `voice_events` row
    the webhook already ledgered, by replaying that row's stored payload back through the
    OWNING carrier's own parser - the same parser that produced the event the first time,
    so there is exactly one place that knows the carrier's JSON shape."""
    call = await session.get(Call, recording.call_id)
    if call is None:  # pragma: no cover - FK guarantees this
        return None, None

    carrier_obj = registry.get(call.carrier) if registry is not None else None
    try:
        adapter = as_voice_carrier(carrier_obj) if carrier_obj is not None else None
    except FeatureUnavailableError:
        adapter = None
    if adapter is None:
        return None, None

    rows_stmt = (
        sa.select(VoiceEventRow)
        .where(
            VoiceEventRow.call_id == recording.call_id,
            VoiceEventRow.event_type == "recording_ready",
        )
        .order_by(VoiceEventRow.created_at.desc())
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    for row in (await session.execute(rows_stmt)).scalars().all():
        try:
            parsed = adapter.parse_voice_webhook(json.dumps(row.payload).encode())
        except Exception:  # noqa: BLE001 - a malformed stored payload must not crash the sweep
            continue
        for candidate_event in parsed:
            if (
                candidate_event.event_type == "recording_ready"
                and candidate_event.provider_recording_id == recording.provider_recording_id
            ):
                return adapter, candidate_event
    return None, None


async def _fetch_one_recording(
    session: AsyncSession,
    store,  # noqa: ANN001
    registry,  # noqa: ANN001
    client: httpx.AsyncClient,
    recording: CallRecording,
) -> bool:
    adapter, event = await _resolve_recording_source(session, registry, recording)
    if adapter is None or event is None:
        return await _fail(recording, "no carrier adapter / source voice_event to re-derive URL")

    if not event.recording_url:
        return await _fail(recording, "recording_ready event carried no URL")

    auth = adapter.recording_auth(event.recording_url)

    try:
        async with client.stream("GET", event.recording_url, auth=auth) as response:
            if response.status_code >= 400:
                return await _fail(recording, f"http {response.status_code}")

            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            if content_type == _OCTET_STREAM:
                content_type = "audio/mpeg"
            if content_type not in ALLOWED_RECORDING_CONTENT_TYPES:
                return await _fail(recording, f"unsupported type {content_type or '<missing>'}")

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RECORDING_BYTES:
                    return await _fail(recording, f"exceeds {MAX_RECORDING_BYTES} bytes")
                chunks.append(chunk)
            data = b"".join(chunks)
    except httpx.HTTPError as exc:
        return await _fail(recording, f"{type(exc).__name__}: {exc}")

    key = storage_key(recording.org_id, recording.id)
    await store.put(key, data, content_type)
    recording.storage_key = key
    recording.content_type = content_type
    recording.size_bytes = len(data)
    if event.duration_seconds is not None:
        recording.duration_seconds = event.duration_seconds
    recording.status = "stored"
    return True


async def load_recording_bytes(store, recording: CallRecording) -> bytes:  # noqa: ANN001
    """Mirror routes/media.py's `content` endpoint: read the stored bytes back for serving."""
    return await store.get(recording.storage_key)
