"""P12 voicemail (DR-8): rows created from a flow's voicemail terminal, plus the
transcription seam.

Recording linkage deliberately does not touch `services/recordings.py`. That module's
`on_recording_ready` only upserts a `pending` CallRecording row from inside the voice
webhook path (zero network I/O by ARCHITECTURE D6); the actual download is driven by the
sweeper's `fetch_pending_recordings`. This module links AFTER both of those, on its own
sweeper pass: `create_from_flow` writes the Voicemail row the instant
`services/routing_exec.py` reaches a voicemail terminal (BEFORE any recording exists at
all - the greeting/StartRecording commands haven't even been rendered to the carrier yet),
and `link_pending_recordings` is what later matches it to the CallRecording that call's own
`recording_ready` webhook eventually produces.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models.callflow import Voicemail
from app.models.voice import Call, CallRecording
from app.services import outbox
from app.services import recordings as recordings_svc

log = structlog.get_logger("voicemail")

DEEPGRAM_TRANSCRIBE_URL = "https://api.deepgram.com/v1/listen"
TRANSCRIBE_TIMEOUT_SECONDS = 15.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_from_flow(
    session: AsyncSession,
    call: Call,
    flow,
    *,
    node_id: str,
    greeting: str,  # noqa: ANN001 - CallFlow
) -> Voicemail:
    """Written the instant the flow reaches its voicemail terminal. The durable
    `voicemail.created` outbox event is recorded in the SAME transaction (OPEN_ISSUES D14) -
    the caller (routing_exec) commits once, right after this returns."""
    row = Voicemail(
        id=uuid.uuid4(),
        org_id=call.org_id,
        call_id=call.id,
        recording_id=None,
        greeting_node=node_id,
        transcript=None,
        transcript_status="pending",
        status="new",
    )
    session.add(row)
    await session.flush()
    outbox.record_platform_event(
        session,
        call.org_id,
        "voicemail.created",
        {
            "voicemail_id": str(row.id),
            "call_id": str(call.id),
            "flow_id": str(flow.id) if flow is not None else None,
            "greeting_node": node_id,
        },
    )
    return row


async def link_pending_recordings(session: AsyncSession, *, limit: int = 25) -> int:
    """Sweeper-driven: a Voicemail row still lacking a recording, whose call now has a
    CallRecording row (any status - the recording continues its own lifecycle via
    recordings.fetch_pending_recordings independently) gets linked.

    B2: commits PER ROW, immediately after that row's own mutation, before the next
    row's `set_org_context` - same reasoning as `routing_exec.routing_tick` (B1): a
    trailing single commit would autoflush a still-pending mutation under the WRONG org
    context, and a mid-loop exception (this loop touches TWO orgs' worth of rows in one
    pass) would otherwise lose every row's status update, not just the failing one's.
    """
    stmt = (
        sa.select(Voicemail)
        .where(Voicemail.recording_id.is_(None))
        .order_by(Voicemail.created_at)
        .limit(limit)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    pending = list((await session.execute(stmt)).scalars().all())
    linked = 0
    for voicemail in pending:
        set_org_context(session, voicemail.org_id)
        recording = (
            await session.execute(
                sa.select(CallRecording)
                .where(CallRecording.call_id == voicemail.call_id)
                .order_by(CallRecording.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if recording is None:
            continue
        voicemail.recording_id = recording.id
        await session.commit()
        linked += 1
    return linked


async def transcribe_pending(
    session: AsyncSession,
    store,  # noqa: ANN001 - ObjectStore protocol (app/storage/base.py)
    *,
    deepgram_api_key: str = "",
    client: httpx.AsyncClient | None = None,
    limit: int = 10,
) -> dict[str, int]:
    """Sweeper-driven transcription seam (DR-8). No key configured -> every pending
    voicemail with a stored recording is honestly marked `disabled`, never faked.

    B2: commits PER ROW - same reasoning as `link_pending_recordings` above. A trailing
    single commit both risks the wrong-org-context autoflush AND, since this loop makes a
    real HTTP call per row, means one Deepgram failure partway through would roll back
    every OTHER row's already-successful transcript in the same pass.
    """
    counts = {"done": 0, "failed": 0, "disabled": 0}

    stmt = (
        sa.select(Voicemail)
        .where(Voicemail.transcript_status == "pending", Voicemail.recording_id.isnot(None))
        .order_by(Voicemail.created_at)
        .limit(limit)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    pending = list((await session.execute(stmt)).scalars().all())
    if not pending:
        return counts

    if not deepgram_api_key:
        for voicemail in pending:
            set_org_context(session, voicemail.org_id)
            voicemail.transcript_status = "disabled"
            await session.commit()
            counts["disabled"] += 1
        return counts

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT_SECONDS)
    try:
        for voicemail in pending:
            set_org_context(session, voicemail.org_id)
            recording = await session.get(CallRecording, voicemail.recording_id)
            if recording is None or recording.status != "stored":
                continue  # still waiting on recordings.fetch_pending_recordings - try next pass
            ok = await _transcribe_one(
                session, store, client, deepgram_api_key, voicemail, recording
            )
            await session.commit()
            counts["done" if ok else "failed"] += 1
    finally:
        if owns_client:
            await client.aclose()
    return counts


async def _transcribe_one(
    session: AsyncSession,
    store,  # noqa: ANN001
    client: httpx.AsyncClient,
    api_key: str,
    voicemail: Voicemail,
    recording: CallRecording,
) -> bool:
    try:
        data = await recordings_svc.load_recording_bytes(store, recording)
    except KeyError:
        voicemail.transcript_status = "failed"
        log.warning("voicemail_transcribe_missing_bytes", voicemail_id=str(voicemail.id))
        return False

    try:
        resp = await client.post(
            DEEPGRAM_TRANSCRIBE_URL,
            params={"model": "nova-2", "smart_format": "true"},
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": recording.content_type or "audio/mpeg",
            },
            content=data,
        )
    except httpx.HTTPError as exc:
        log.warning(
            "voicemail_transcribe_request_failed", voicemail_id=str(voicemail.id), error=str(exc)
        )
        voicemail.transcript_status = "failed"
        return False

    if resp.status_code >= 400:
        voicemail.transcript_status = "failed"
        return False

    try:
        payload = resp.json()
        transcript = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError, ValueError, TypeError):
        voicemail.transcript_status = "failed"
        return False

    voicemail.transcript = transcript
    voicemail.transcript_status = "done"
    return True
