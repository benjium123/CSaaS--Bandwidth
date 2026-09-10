from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ValidationFailedError
from app.models import (
    CallRecording,
    CallTranscriptSegment,
    ContactList,
    MediaAsset,
    Message,
    Org,
    RetentionPolicy,
    Voicemail,
)
from app.services import audit as audit_svc

log = structlog.get_logger("retention")

RETENTION_DEFAULTS: dict[str, int | None] = {
    "messages_days": None,
    "recordings_days": 90,
    "transcripts_days": 365,
    "imports_days": 30,
}
PURGE_BATCH = 500


def _import_source_key(org_id, list_id) -> str:
    return f"org/{org_id}/imports/{list_id}/source"


def validate_days(field: str, value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        label = field.replace("_days", "")
        raise ValidationFailedError(
            f"Keep {label} for at least one day, or leave it empty to keep them forever."
        )
    return value


async def get_or_create_policy(session: AsyncSession, org_id) -> RetentionPolicy:
    policy = (
        await session.execute(sa.select(RetentionPolicy))
    ).scalar_one_or_none()
    if policy is None:
        policy = RetentionPolicy(org_id=org_id, **RETENTION_DEFAULTS)
        session.add(policy)
        await session.flush()
    return policy


async def update_policy(session, org_id, updates: dict) -> RetentionPolicy:
    policy = await get_or_create_policy(session, org_id)
    for field in RETENTION_DEFAULTS:
        if field in updates:
            value = validate_days(field, updates[field])
            setattr(policy, field, value)
    return policy


async def purge_org(session, store, org_id, policy, *, now=None) -> dict[str, int]:
    counts = {
        "messages": 0,
        "media": 0,
        "recordings": 0,
        "transcripts": 0,
        "voicemails": 0,
        "imports": 0,
    }
    moment = now or datetime.now(timezone.utc)
    is_sqlite = session.get_bind().dialect.name == "sqlite"

    def bind_dt(value: datetime):
        return value.replace(tzinfo=None) if is_sqlite else value

    if policy.messages_days is not None:
        cutoff = moment - timedelta(days=policy.messages_days)
        # Only rows that still HAVE something to clear. Without this filter every past
        # message is re-selected, re-written and re-counted on every single sweep - the
        # audit row would report the same purge forever and the pass would never settle.
        last_id = None
        while True:
            stmt = (
                sa.select(Message)
                .where(Message.created_at < bind_dt(cutoff), Message.body.is_not(None))
                .order_by(Message.id)
                .limit(PURGE_BATCH)
            )
            if last_id is not None:
                stmt = stmt.where(Message.id > last_id)
            messages = list((await session.execute(stmt)).scalars().all())
            if not messages:
                break
            for message in messages:
                message.body = None
                message.media = []
            await session.commit()
            counts["messages"] += len(messages)
            last_id = messages[-1].id
            session.expunge_all()
            set_org_context(session, org_id)

        # Attachments hang off those messages and are purged on their own status, so a
        # message whose body was already cleared still gives up its stored file.
        last_id = None
        while True:
            stmt = (
                sa.select(MediaAsset)
                .join(Message, Message.id == MediaAsset.message_id)
                .where(
                    Message.created_at < bind_dt(cutoff),
                    MediaAsset.status == "stored",
                )
                .order_by(MediaAsset.id)
                .limit(PURGE_BATCH)
            )
            if last_id is not None:
                stmt = stmt.where(MediaAsset.id > last_id)
            assets = list((await session.execute(stmt)).scalars().all())
            if not assets:
                break
            for asset in assets:
                if asset.storage_key:
                    await store.delete(asset.storage_key)
                asset.status = "purged"
                asset.storage_key = None
            await session.commit()
            counts["media"] += len(assets)
            last_id = assets[-1].id
            session.expunge_all()
            set_org_context(session, org_id)

    if policy.recordings_days is not None:
        cutoff = moment - timedelta(days=policy.recordings_days)
        last_id = None
        while True:
            stmt = (
                sa.select(CallRecording)
                .where(
                    CallRecording.created_at < bind_dt(cutoff),
                    CallRecording.status == "stored",
                )
                .order_by(CallRecording.id)
                .limit(PURGE_BATCH)
            )
            if last_id is not None:
                stmt = stmt.where(CallRecording.id > last_id)
            recordings = list((await session.execute(stmt)).scalars().all())
            if not recordings:
                break
            for recording in recordings:
                if recording.storage_key:
                    await store.delete(recording.storage_key)
                recording.status = "purged"
                recording.storage_key = ""
            await session.commit()
            counts["recordings"] += len(recordings)
            last_id = recordings[-1].id
            session.expunge_all()
            set_org_context(session, org_id)

    if policy.transcripts_days is not None:
        cutoff = moment - timedelta(days=policy.transcripts_days)
        last_id = None
        while True:
            stmt = (
                sa.select(CallTranscriptSegment.id)
                .where(CallTranscriptSegment.created_at < bind_dt(cutoff))
                .order_by(CallTranscriptSegment.id)
                .limit(PURGE_BATCH)
            )
            if last_id is not None:
                stmt = stmt.where(CallTranscriptSegment.id > last_id)
            segment_ids = list((await session.execute(stmt)).scalars().all())
            if not segment_ids:
                break
            await session.execute(
                sa.delete(CallTranscriptSegment).where(
                    CallTranscriptSegment.id.in_(segment_ids)
                )
            )
            await session.commit()
            counts["transcripts"] += len(segment_ids)
            last_id = segment_ids[-1]
            session.expunge_all()
            set_org_context(session, org_id)

        last_id = None
        while True:
            stmt = (
                sa.select(Voicemail)
                .where(
                    Voicemail.created_at < bind_dt(cutoff),
                    Voicemail.transcript_status != "purged",
                )
                .order_by(Voicemail.id)
                .limit(PURGE_BATCH)
            )
            if last_id is not None:
                stmt = stmt.where(Voicemail.id > last_id)
            voicemails = list((await session.execute(stmt)).scalars().all())
            if not voicemails:
                break
            for voicemail in voicemails:
                voicemail.transcript = None
                voicemail.transcript_status = "purged"
            await session.commit()
            counts["voicemails"] += len(voicemails)
            last_id = voicemails[-1].id
            session.expunge_all()
            set_org_context(session, org_id)

    if policy.imports_days is not None:
        cutoff = moment - timedelta(days=policy.imports_days)
        last_id = None
        while True:
            stmt = (
                sa.select(ContactList)
                .where(ContactList.created_at < bind_dt(cutoff))
                .order_by(ContactList.id)
                .limit(PURGE_BATCH)
            )
            if last_id is not None:
                stmt = stmt.where(ContactList.id > last_id)
            lists = list((await session.execute(stmt)).scalars().all())
            if not lists:
                break
            purged = 0
            for lst in lists:
                summary = lst.import_summary or {}
                if "source_purged_at" in summary:
                    continue
                await store.delete(_import_source_key(org_id, lst.id))
                lst.import_summary = {
                    **summary,
                    "source_purged_at": moment.isoformat(),
                }
                purged += 1
            await session.commit()
            counts["imports"] += purged
            last_id = lists[-1].id
            session.expunge_all()
            set_org_context(session, org_id)

    return counts


async def retention_tick(session, store, *, now=None) -> dict[str, int]:
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    moment = now or datetime.now(timezone.utc)
    totals = {
        "messages": 0,
        "media": 0,
        "recordings": 0,
        "transcripts": 0,
        "voicemails": 0,
        "imports": 0,
    }

    for org_id in org_ids:
        set_org_context(session, org_id)
        try:
            policy = await get_or_create_policy(session, org_id)
            counts = await purge_org(session, store, org_id, policy, now=moment)
            if any(counts.values()):
                audit_svc.record(
                    session,
                    org_id,
                    action="retention.purge",
                    target_type="org",
                    target_id=str(org_id),
                    detail=counts,
                )
            await session.commit()
            for key in totals:
                totals[key] += counts.get(key, 0)
        except Exception:
            log.exception("retention_org_purge_failed", org_id=str(org_id))
            await session.rollback()

    totals["orgs"] = len(org_ids)
    return totals
