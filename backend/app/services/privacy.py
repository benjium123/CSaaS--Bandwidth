from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError
from app.models import (
    Call,
    CallRecording,
    CallTranscriptSegment,
    ConsentEvent,
    Contact,
    ContactNote,
    ContactPhone,
    ContactTag,
    DncEntry,
    ErasureRequest,
    MediaAsset,
    Message,
    MessageThread,
    Org,
    Tag,
    Voicemail,
)
from app.services import audit as audit_svc

log = structlog.get_logger("privacy")

# Consent and opt-out rows survive erasure. ConsentEvent, DncEntry and ComplianceBlock are
# never touched, read-only, by anything in this file - the law requires the opt-out to
# outlive the person's record, otherwise we would text them again tomorrow.
ERASED_NAME = "Erased contact"
ERASED_BODY = "[erased]"


async def request_erasure(session, org_id, *, contact, requested_by) -> ErasureRequest:
    existing = (
        await session.execute(
            sa.select(ErasureRequest).where(
                ErasureRequest.contact_id == contact.id,
                ErasureRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("An erasure request is already pending for this contact.")

    request_row = ErasureRequest(
        org_id=org_id,
        contact_id=contact.id,
        requested_by=requested_by,
        status="pending",
    )
    session.add(request_row)
    audit_svc.record(
        session,
        org_id,
        action="contact.erase_requested",
        target_type="contact",
        target_id=str(contact.id),
        actor_user_id=requested_by,
    )
    await session.flush()
    return request_row


async def perform_erasure(session, store, request_row) -> dict:
    now = datetime.now(timezone.utc)
    contact = await session.get(Contact, request_row.contact_id)
    if contact is None:
        request_row.status = "failed"
        request_row.summary = {"error": "Contact no longer exists"}
        audit_svc.record(
            session,
            request_row.org_id,
            action="contact.erase",
            target_type="contact",
            target_id=str(request_row.contact_id),
            detail=request_row.summary,
        )
        await session.commit()
        return {
            "messages": 0,
            "media": 0,
            "transcripts": 0,
            "recordings": 0,
            "notes": 0,
            "phones": 0,
            "consent_rows_kept": 0,
            "dnc_rows_kept": 0,
        }

    phones = list(
        (
            await session.execute(
                sa.select(ContactPhone).where(ContactPhone.contact_id == contact.id)
            )
        )
        .scalars()
        .all()
    )
    e164s = [phone.e164 for phone in phones]

    thread_ids = list(
        (
            await session.execute(
                sa.select(MessageThread.id).where(
                    sa.or_(
                        MessageThread.contact_id == contact.id,
                        MessageThread.contact_e164.in_(e164s)
                        if e164s
                        else sa.false(),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    messages: list[Message] = []
    if thread_ids:
        messages = list(
            (
                await session.execute(
                    sa.select(Message).where(Message.thread_id.in_(thread_ids))
                )
            )
            .scalars()
            .all()
        )
    message_ids = [message.id for message in messages]
    for message in messages:
        message.body = ERASED_BODY
        message.media = []

    assets: list[MediaAsset] = []
    if message_ids:
        assets = list(
            (
                await session.execute(
                    sa.select(MediaAsset).where(
                        MediaAsset.message_id.in_(message_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    for asset in assets:
        if asset.storage_key:
            await store.delete(asset.storage_key)
        asset.status = "purged"
        asset.storage_key = None

    calls = []
    if e164s:
        calls = list(
            (
                await session.execute(
                    sa.select(Call).where(Call.contact_e164.in_(e164s))
                )
            )
            .scalars()
            .all()
        )
    call_ids = [call.id for call in calls]
    transcripts = 0
    recordings = 0
    if call_ids:
        transcript_rows = list(
            (
                await session.execute(
                    sa.select(CallTranscriptSegment).where(
                        CallTranscriptSegment.call_id.in_(call_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        transcripts = len(transcript_rows)
        if transcript_rows:
            await session.execute(
                sa.delete(CallTranscriptSegment).where(
                    CallTranscriptSegment.id.in_(
                        [segment.id for segment in transcript_rows]
                    )
                )
            )

        recording_rows = list(
            (
                await session.execute(
                    sa.select(CallRecording).where(
                        CallRecording.call_id.in_(call_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for recording in recording_rows:
            if recording.storage_key:
                await store.delete(recording.storage_key)
            recording.status = "purged"
            recording.storage_key = ""
        recordings = len(recording_rows)

        voicemails = list(
            (
                await session.execute(
                    sa.select(Voicemail).where(Voicemail.call_id.in_(call_ids))
                )
            )
            .scalars()
            .all()
        )
        for voicemail in voicemails:
            voicemail.transcript = None
            voicemail.transcript_status = "purged"

    notes = list(
        (
            await session.execute(
                sa.select(ContactNote).where(ContactNote.contact_id == contact.id)
            )
        )
        .scalars()
        .all()
    )
    for note in notes:
        await session.delete(note)

    for phone in phones:
        await session.delete(phone)

    contact.display_name = ERASED_NAME
    contact.first_name = None
    contact.last_name = None
    contact.attributes = {}
    contact.company_id = None
    contact.timezone = None

    consent_rows_kept = 0
    dnc_rows_kept = 0
    if e164s:
        consent_rows_kept = (
            await session.execute(
                sa.select(sa.func.count(ConsentEvent.id)).where(
                    ConsentEvent.contact_e164.in_(e164s)
                )
            )
        ).scalar_one()
        dnc_rows_kept = (
            await session.execute(
                sa.select(sa.func.count(DncEntry.id)).where(
                    DncEntry.e164.in_(e164s)
                )
            )
        ).scalar_one()

    summary = {
        "messages": len(messages),
        "media": len(assets),
        "transcripts": transcripts,
        "recordings": recordings,
        "notes": len(notes),
        "phones": len(phones),
        "consent_rows_kept": consent_rows_kept,
        "dnc_rows_kept": dnc_rows_kept,
    }

    request_row.status = "done"
    request_row.completed_at = now
    request_row.summary = summary

    audit_svc.record(
        session,
        request_row.org_id,
        action="contact.erase",
        target_type="contact",
        target_id=str(contact.id),
        detail=summary,
    )
    await session.commit()
    return summary


async def erasure_tick(session, store) -> dict[str, int]:
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    result = {"orgs": len(org_ids), "completed": 0, "failed": 0}
    now = datetime.now(timezone.utc)

    for org_id in org_ids:
        set_org_context(session, org_id)
        try:
            pending_ids = list(
                (
                    await session.execute(
                        sa.select(ErasureRequest.id).where(
                            ErasureRequest.status == "pending"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for request_id in pending_ids:
                row = await session.get(ErasureRequest, request_id)
                if row is None or row.status != "pending":
                    continue
                try:
                    await perform_erasure(session, store, row)
                    result["completed"] += 1
                except Exception:
                    log.exception(
                        "erasure_request_failed", request_id=str(request_id)
                    )
                    await session.rollback()
                    failed_row = await session.get(ErasureRequest, request_id)
                    if failed_row is not None:
                        failed_row.status = "failed"
                        failed_row.completed_at = now
                        failed_row.summary = {
                            "error": "Erasure failed; see server logs"
                        }
                        await session.commit()
                    result["failed"] += 1
        except Exception:
            log.exception("erasure_org_failed", org_id=str(org_id))
            await session.rollback()

    return result


async def build_my_data_bundle(session, contact) -> dict:
    contact_id = contact.id
    phones = list(
        (
            await session.execute(
                sa.select(ContactPhone).where(ContactPhone.contact_id == contact_id)
            )
        )
        .scalars()
        .all()
    )
    e164s = [phone.e164 for phone in phones]

    contact_data = {
        "id": str(contact.id),
        "display_name": contact.display_name,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "attributes": contact.attributes,
        "company_id": str(contact.company_id) if contact.company_id else None,
        "timezone": contact.timezone,
        "created_at": contact.created_at.isoformat()
        if contact.created_at
        else None,
    }

    tags = list(
        (
            await session.execute(
                sa.select(Tag.name)
                .join(ContactTag, ContactTag.tag_id == Tag.id)
                .where(ContactTag.contact_id == contact_id)
                .order_by(Tag.name.asc())
            )
        )
        .scalars()
        .all()
    )

    notes = list(
        (
            await session.execute(
                sa.select(ContactNote).where(ContactNote.contact_id == contact_id)
            )
        )
        .scalars()
        .all()
    )
    note_data = [
        {
            "body": note.body,
            "created_at": note.created_at.isoformat()
            if note.created_at
            else None,
        }
        for note in notes
    ]

    threads = []
    if e164s or contact_id:
        threads = list(
            (
                await session.execute(
                    sa.select(MessageThread).where(
                        sa.or_(
                            MessageThread.contact_id == contact_id,
                            MessageThread.contact_e164.in_(e164s)
                            if e164s
                            else sa.false(),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    conversations = []
    for thread in threads:
        thread_messages = list(
            (
                await session.execute(
                    sa.select(Message)
                    .where(Message.thread_id == thread.id)
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )
        conversations.append(
            {
                "thread_id": str(thread.id),
                "with": thread.contact_e164,
                "messages": [
                    {
                        "direction": message.direction,
                        "body": message.body,
                        "created_at": message.created_at.isoformat()
                        if message.created_at
                        else None,
                    }
                    for message in thread_messages
                ],
            }
        )

    calls = []
    if e164s:
        calls = list(
            (
                await session.execute(
                    sa.select(Call).where(Call.contact_e164.in_(e164s))
                )
            )
            .scalars()
            .all()
        )
    call_data = [
        {
            "id": str(call.id),
            "direction": call.direction,
            "status": call.status,
            "created_at": call.created_at.isoformat()
            if call.created_at
            else None,
        }
        for call in calls
    ]

    consent_rows = []
    if e164s:
        consent_rows = list(
            (
                await session.execute(
                    sa.select(ConsentEvent).where(
                        ConsentEvent.contact_e164.in_(e164s)
                    )
                )
            )
            .scalars()
            .all()
        )
    consent_data = [
        {
            "id": str(event.id),
            "event": event.event,
            "channel": event.channel,
            "created_at": event.created_at.isoformat()
            if event.created_at
            else None,
        }
        for event in consent_rows
    ]

    dnc_rows = []
    if e164s:
        dnc_rows = list(
            (
                await session.execute(
                    sa.select(DncEntry).where(DncEntry.e164.in_(e164s))
                )
            )
            .scalars()
            .all()
        )
    dnc_data = [
        {
            "id": str(entry.id),
            "e164": entry.e164,
            "created_at": entry.created_at.isoformat()
            if entry.created_at
            else None,
        }
        for entry in dnc_rows
    ]

    return {
        "contact": contact_data,
        "phones": [phone.e164 for phone in phones],
        "tags": tags,
        "notes": note_data,
        "conversations": conversations,
        "calls": call_data,
        "consent": consent_data,
        "dnc": dnc_data,
    }


def my_data_csv(bundle: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "key", "value"])

    for key, value in bundle.get("contact", {}).items():
        writer.writerow(["contact", key, value])

    for phone in bundle.get("phones", []):
        writer.writerow(["phone", "e164", phone])

    for tag in bundle.get("tags", []):
        writer.writerow(["tag", "name", tag])

    for note in bundle.get("notes", []):
        # section, key, value - the note BODY is the value; putting it in the key column
        # shifted the timestamp into "value" and made the column headers a lie.
        writer.writerow(["note", note.get("created_at"), note.get("body")])

    return output.getvalue()
