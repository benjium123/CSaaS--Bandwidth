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

P29 dual-channel layer: a 'dual' recording keeps three stored objects - the agent's
side, the customer's side, and a stitched two-channel WAV. The mixed layout's storage key
is exactly the pre-P29 `recording.storage_key`, so single-file recordings do not move. The
org's "recording layout" setting is an INTENT for the capture side only; a carrier-delivered
single file is 'mixed' no matter what the setting says, and `on_recording_ready` therefore
keeps writing rows at the 'mixed' default.
"""

from __future__ import annotations

import io
import json
import sys
import uuid
import wave
from array import array
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, ValidationFailedError
from app.models.voice import Call, CallLeg, CallRecording
from app.models.voice import VoiceEvent as VoiceEventRow
from app.providers import registry_org
from app.services import credentials as credential_svc
from app.providers.voice import VoiceEvent, as_voice_carrier

log = structlog.get_logger("recordings")

#: Refuse anything larger; a call recording has no business anywhere near this.
MAX_RECORDING_BYTES = 100 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10.0

ALLOWED_RECORDING_CONTENT_TYPES = frozenset(
    {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"}
)
_OCTET_STREAM = "application/octet-stream"

RECORDING_LAYOUTS: tuple[str, ...] = ("mixed", "agent", "customer")

#: PCM sample width -> `array` typecode. 8-bit WAV is unsigned; 16- and 32-bit signed.
_ARRAY_TYPECODES = {1: "B", 2: "h", 4: "i"}

#: Per-carrier host allowlist for recording URLs. A webhook payload is untrusted input;
#: never fetch a recording from a host the carrier has not explicitly named as safe.
CARRIER_RECORDING_HOST_ALLOWLIST = {
    "bandwidth": (".bandwidth.com",),
    "telnyx": (".telnyx.com",),
    "twilio": (".twilio.com",),
    # Matches providers/plivo/voice.py::recording_auth's own host check.
    "plivo": (".plivo.com",),
}

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


def layout_storage_key(recording: CallRecording, layout: str) -> str:
    if layout == "mixed":
        return recording.storage_key
    if layout in ("agent", "customer"):
        return f"{recording.storage_key}/{layout}"
    raise ValueError("Unknown recording layout")


def available_layouts(recording: CallRecording) -> list[str]:
    if recording.channel_layout != "dual":
        return ["mixed"]
    return list(RECORDING_LAYOUTS)


def _read_pcm_mono_wav(data: bytes) -> tuple[int, int, bytes]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getcomptype() != "NONE":
                raise ValidationFailedError("Recording tracks must be mono PCM WAV")
            sampwidth = wav.getsampwidth()
            if sampwidth not in (1, 2, 4):
                raise ValidationFailedError(
                    "Recording tracks must use 8-, 16-, or 32-bit PCM"
                )
            framerate = wav.getframerate()
            frames = bytes(wav.readframes(wav.getnframes()))
            return sampwidth, framerate, frames
    except (wave.Error, EOFError) as exc:
        raise ValidationFailedError("Recording tracks must be mono PCM WAV") from exc


def wav_duration_seconds(data: bytes) -> int | None:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            framerate = wav.getframerate()
            if framerate <= 0:
                return None
            return wav.getnframes() // framerate
    except (wave.Error, EOFError, ValueError):
        return None


def stitch_dual_channel(agent_wav: bytes, customer_wav: bytes) -> bytes:
    """Stitch two mono PCM WAV tracks into one two-channel WAV.

    Agent frames are channel 0 (left) and customer frames are channel 1 (right). The
    shorter track is padded with digital silence, never truncated or mixed down.
    """
    agent_sampwidth, agent_framerate, agent_frames = _read_pcm_mono_wav(agent_wav)
    customer_sampwidth, customer_framerate, customer_frames = _read_pcm_mono_wav(
        customer_wav
    )
    if agent_sampwidth != customer_sampwidth or agent_framerate != customer_framerate:
        raise ValidationFailedError("Recording tracks must share the same audio format")

    sampwidth = agent_sampwidth
    framerate = agent_framerate
    #: 8-bit WAV is UNSIGNED, so its digital zero is 128, not 0.
    silence = 128 if sampwidth == 1 else 0

    left = array(_ARRAY_TYPECODES[sampwidth], agent_frames)
    right = array(_ARRAY_TYPECODES[sampwidth], customer_frames)
    if sys.byteorder == "big":
        # WAV samples are little-endian on the wire; `array` is native-endian.
        left.byteswap()
        right.byteswap()

    total_samples = max(len(left), len(right))
    left.extend([silence] * (total_samples - len(left)))
    right.extend([silence] * (total_samples - len(right)))

    # Interleave left=agent, right=customer at C speed - a per-sample Python loop turns a
    # 30-minute call into tens of seconds of CPU. A mono downmix would silently destroy
    # the speaker separation that dual-channel recording exists to preserve, so this
    # deliberately writes BOTH channels rather than summing them.
    interleaved = array(_ARRAY_TYPECODES[sampwidth], bytes(total_samples * 2 * sampwidth))
    interleaved[0::2] = left
    interleaved[1::2] = right
    if sys.byteorder == "big":
        interleaved.byteswap()

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(sampwidth)
        wav.setframerate(framerate)
        wav.writeframes(interleaved.tobytes())
    return output.getvalue()


def _recording_host_allowed(carrier_name: str, url: str) -> bool:
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    suffixes = CARRIER_RECORDING_HOST_ALLOWLIST.get(carrier_name)
    if not suffixes:
        return False
    return any(hostname == suffix.lstrip(".") or hostname.endswith(suffix) for suffix in suffixes)


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
    settings=None,  # noqa: ANN001 - app.config.Settings; D4 org-context priming
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
            # D4: prime this org's DB-backed carrier registry into CURRENT_ORG_ID,
            # exactly as outbound_tick does - without it, _fetch_one_recording's
            # registry.get(carrier_name) below only ever resolves the env-configured
            # carrier, never a DB-only org's own provider account.
            org_token = registry_org.CURRENT_ORG_ID.set(recording.org_id)
            try:
                if (
                    settings is not None
                    and credential_svc.master_key_present(settings)
                    and not registry_org.is_primed(recording.org_id)
                ):
                    try:
                        global_registry = getattr(registry, "global_registry", None)
                        await registry_org.prime_org_registry(
                            session, settings, recording.org_id, global_registry=global_registry
                        )
                    except Exception:  # noqa: BLE001 - priming must not kill the whole pass
                        log.exception(
                            "org_registry_prime_failed", org_id=str(recording.org_id)
                        )
                try:
                    ok = await _fetch_one_recording(session, store, registry, client, recording)
                except Exception:  # noqa: BLE001 - one bad row must not kill the whole pass
                    log.exception(
                        "recording_fetch_row_failed",
                        org_id=str(recording.org_id),
                        recording_id=str(recording.id),
                    )
                    await session.rollback()
                    continue
                # 3.14: commit PER ROW, not once across every org in `pending` - a later
                # row's failure (exception or otherwise) must never roll back an
                # earlier row's already-successful fetch, especially across DIFFERENT
                # orgs.
                try:
                    await session.commit()
                except Exception:  # noqa: BLE001
                    log.exception(
                        "recording_fetch_commit_failed",
                        org_id=str(recording.org_id),
                        recording_id=str(recording.id),
                    )
                    await session.rollback()
                    continue
            finally:
                registry_org.CURRENT_ORG_ID.reset(org_token)
            fetched += 1 if ok else 0
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

    call = await session.get(Call, recording.call_id)
    if call is None:  # pragma: no cover - FK guarantees
        return await _fail(recording, "call row missing")
    if not _recording_host_allowed(call.carrier, event.recording_url):
        return await _fail(recording, "recording URL host is not allowlisted for carrier")

    # Prefer a header-based credential when the adapter has one. A Bandwidth account on
    # OAuth2 has no Basic pair to hand back, so recording_auth() correctly returns None
    # there and the download would otherwise go out unauthenticated and 401.
    auth = adapter.recording_auth(event.recording_url)
    headers: dict = {}
    header_fn = getattr(adapter, "recording_headers", None)
    if header_fn is not None:
        headers = await header_fn(event.recording_url) or {}

    try:
        async with client.stream(
            "GET", event.recording_url, auth=auth, headers=headers
        ) as response:
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


async def finalize_dual_recording(
    session: AsyncSession,
    store,  # noqa: ANN001 - ObjectStore protocol (app/storage/base.py)
    recording: CallRecording,
    *,
    agent_wav: bytes,
    customer_wav: bytes,
) -> CallRecording:
    """Store all three objects for a dual-channel recording and flip the row to stored.

    The caller owns the transaction; this never commits, same discipline as
    `on_recording_ready`.
    """
    if len(agent_wav) > MAX_RECORDING_BYTES:
        await _fail(recording, "agent track exceeds MAX_RECORDING_BYTES")
        return recording
    if len(customer_wav) > MAX_RECORDING_BYTES:
        await _fail(recording, "customer track exceeds MAX_RECORDING_BYTES")
        return recording

    mixed = stitch_dual_channel(agent_wav, customer_wav)
    if len(mixed) > MAX_RECORDING_BYTES:
        await _fail(
            recording, "stitched dual-channel recording exceeds MAX_RECORDING_BYTES"
        )
        return recording

    await store.put(layout_storage_key(recording, "agent"), agent_wav, "audio/wav")
    await store.put(layout_storage_key(recording, "customer"), customer_wav, "audio/wav")
    await store.put(layout_storage_key(recording, "mixed"), mixed, "audio/wav")

    recording.channel_layout = "dual"
    recording.content_type = "audio/wav"
    recording.size_bytes = len(mixed)
    if recording.duration_seconds is None:
        recording.duration_seconds = wav_duration_seconds(mixed)
    recording.status = "stored"
    return recording


async def load_recording_bytes(
    store,  # noqa: ANN001 - ObjectStore protocol (app/storage/base.py)
    recording: CallRecording,
    layout: str = "mixed",
) -> bytes:
    """Read one layout of a stored recording back for serving.

    The default ``"mixed"`` preserves the pre-P29 storage key exactly. A layout not in
    ``available_layouts(recording)`` raises ``KeyError``; callers turn that into a 404.
    """
    if layout not in available_layouts(recording):
        raise KeyError(layout)
    return await store.get(layout_storage_key(recording, layout))
