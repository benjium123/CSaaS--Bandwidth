from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.models import (
    AuditLogEntry,
    Call,
    CallRecording,
    CallTranscriptSegment,
    ConsentEvent,
    Contact,
    ContactList,
    ContactNote,
    ContactPhone,
    DncEntry,
    ErasureRequest,
    MediaAsset,
    Message,
    MessageThread,
    OrgMembership,
    Role,
)
from app.repositories import users as users_repo
from app.services import list_import as list_import_svc
from app.services import privacy as privacy_svc
from app.services import retention as retention_svc
from app.storage.base import InMemoryObjectStore
from tests.conftest import auth_headers, create_contact, create_org, register_and_login


async def _thread_with_message(
    session, org_id, contact_id, e164, *, body="hello", created_at=None
):
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        contact_id=contact_id,
        contact_e164=e164,
        our_e164="+12145559999",
        status="open",
    )
    session.add(thread)
    await session.flush()
    msg = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="inbound",
        status="received",
        from_e164=e164,
        to_e164="+12145559999",
        body=body,
    )
    if created_at is not None:
        msg.created_at = created_at
    session.add(msg)
    await session.flush()
    return thread, msg


async def _register_member(client, session, org_id, email, role_name="agent"):
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role_id = (
        await session.execute(sa.select(Role).where(Role.name == role_name))
    ).scalar_one().id
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role_id)
    )
    await session.commit()
    return token, user


async def test_retention_purge_per_type_with_counts_and_audit(client, session):
    token = await register_and_login(client, "p27-ret-all@example.com")
    org = await create_org(client, token, "Ret All")
    org_id = uuid.UUID(org["id"])
    store = InMemoryObjectStore()

    set_org_context(session, org_id)
    contact = await create_contact(client, token, org["id"], "Ret Contact", ["+12145550700"])
    set_org_context(session, org_id)

    old = datetime.now(timezone.utc) - timedelta(days=100)
    _thread, msg = await _thread_with_message(
        session, org_id, uuid.UUID(contact["id"]), "+12145550700", created_at=old
    )

    asset_id = uuid.uuid4()
    media_key = f"org/{org_id}/media/{asset_id}"
    session.add(
        MediaAsset(
            id=asset_id,
            org_id=org_id,
            message_id=msg.id,
            direction="inbound",
            storage_key=media_key,
            content_type="image/png",
            status="stored",
        )
    )

    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+12145550700",
        our_e164="+12145559999",
        status="completed",
        carrier="bandwidth",
    )
    session.add(call)
    await session.flush()

    recording_id = uuid.uuid4()
    recording_key = f"org/{org_id}/media/{recording_id}"
    recording = CallRecording(
        id=recording_id,
        org_id=org_id,
        call_id=call.id,
        provider_recording_id="recording-1",
        storage_key=recording_key,
        content_type="audio/mpeg",
        status="stored",
    )
    recording.created_at = old
    session.add(recording)

    transcript = CallTranscriptSegment(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        role="agent",
        text="hello from transcript",
        at_ms=100,
    )
    transcript.created_at = old
    session.add(transcript)

    lst = ContactList(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Old import list",
        source_filename="people.csv",
        status="ready",
    )
    lst.created_at = old
    session.add(lst)
    await session.commit()

    await store.put(media_key, b"img", "image/png")
    await store.put(recording_key, b"audio", "audio/mpeg")
    await store.put(
        f"org/{org_id}/imports/{lst.id}/source",
        b"phone,first_name\n+12145551234,Ann\n",
        "text/csv",
    )

    set_org_context(session, org_id)
    policy = await retention_svc.get_or_create_policy(session, org_id)
    policy.messages_days = 30
    policy.recordings_days = 30
    policy.transcripts_days = 30
    policy.imports_days = 30
    await session.commit()

    future = datetime.now(timezone.utc) + timedelta(days=400)
    counts = await retention_svc.retention_tick(session, store, now=future)

    assert counts["messages"] >= 1, counts
    assert counts["media"] >= 1, counts
    assert counts["recordings"] >= 1, counts
    assert counts["transcripts"] >= 1, counts
    assert counts["imports"] >= 1, counts

    set_org_context(session, org_id)

    msg_row = await session.get(Message, msg.id)
    await session.refresh(msg_row)
    assert msg_row.body is None

    recording_row = await session.get(CallRecording, recording.id)
    await session.refresh(recording_row)
    assert recording_row.status == "purged"
    assert recording_row.storage_key == ""
    assert not await store.exists(recording_key)

    transcript_count = (
        await session.execute(sa.select(sa.func.count(CallTranscriptSegment.id)))
    ).scalar_one()
    assert transcript_count == 0

    assert not await store.exists(media_key)
    assert not await store.exists(f"org/{org_id}/imports/{lst.id}/source")
    lst_row = await session.get(ContactList, lst.id)
    await session.refresh(lst_row)
    assert "source_purged_at" in (lst_row.import_summary or {})

    audits = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "retention.purge")
        )
    ).scalars().all()
    assert len(audits) == 1, audits

    # REGRESSION (the purge re-processing bug): a second sweep over an already-purged
    # workspace must process ZERO rows of EVERY type. Each purge loop therefore has to
    # (a) commit per batch, so the next batch query sees the previous batch's writes,
    # and (b) filter on the "still has something to clear" predicate for its own type
    # (Message.body IS NOT NULL, MediaAsset/CallRecording.status == 'stored',
    # Voicemail.transcript_status != 'purged', import_summary without source_purged_at)
    # so a purged row can never be re-selected, re-written or re-counted. Without both,
    # every sweep forever reports the same purge and writes a fresh audit row.
    counts2 = await retention_svc.retention_tick(session, store, now=future)
    assert counts2["messages"] == 0, counts2
    assert counts2["media"] == 0, counts2
    assert counts2["recordings"] == 0, counts2
    assert counts2["transcripts"] == 0, counts2
    assert counts2["voicemails"] == 0, counts2
    assert counts2["imports"] == 0, counts2

    # A settled sweep writes no audit row at all (retention_tick only records when
    # something was actually purged), so the count stays at the single run above.
    audits_after = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "retention.purge")
        )
    ).scalars().all()
    assert len(audits_after) == 1, audits_after


async def test_retention_purge_commits_per_batch():
    source = inspect.getsource(retention_svc.purge_org)
    assert "PURGE_BATCH" in source
    assert "await session.commit()" in source
    assert retention_svc.PURGE_BATCH == 500

    # Per-batch commits matter on SQLite StaticPool because a single transaction kept
    # open while fetching every row would hold the in-memory DB lock for the entire
    # sweep, and because the NEXT batch query must see the previous batch's writes to
    # exclude them. On Postgres a per-batch commit also bounds the undo log and avoids
    # an all-or-nothing outage if one row fails mid-purge.
    #
    # The commit alone is not enough: every loop must ALSO carry a predicate that drops
    # already-processed rows, or the loop re-selects them forever. Pinned here so a
    # future refactor cannot quietly delete the filters.
    assert "Message.body.is_not(None)" in source
    assert 'MediaAsset.status == "stored"' in source
    assert 'CallRecording.status == "stored"' in source
    assert 'Voicemail.transcript_status != "purged"' in source
    assert "source_purged_at" in source


async def test_retention_policy_rejects_zero_and_negative_values(client, session):
    token = await register_and_login(client, "p27-ret-policy@example.com")
    org = await create_org(client, token, "Ret Policy")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org_id)

    r = await client.patch(
        "/api/v1/orgs/current/retention",
        json={"messages_days": 0},
        headers=headers,
    )
    assert r.status_code >= 400
    assert "at least one day" in r.text

    r = await client.patch(
        "/api/v1/orgs/current/retention",
        json={"recordings_days": -5},
        headers=headers,
    )
    assert r.status_code >= 400
    assert "at least one day" in r.text

    r = await client.patch(
        "/api/v1/orgs/current/retention",
        json={"messages_days": None},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/orgs/current/retention", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages_days"] is None
    assert body["recordings_days"] == 90
    assert body["transcripts_days"] == 365
    assert body["imports_days"] == 30

    agent_token, _ = await _register_member(
        client, session, org_id, "p27-ret-agent@example.com", role_name="agent"
    )
    agent_headers = auth_headers(agent_token, org_id)
    r = await client.patch(
        "/api/v1/orgs/current/retention",
        json={"messages_days": 7},
        headers=agent_headers,
    )
    assert r.status_code == 403


async def test_erasure_keeps_optout_rows(client, session):
    token = await register_and_login(client, "p27-erase-optout@example.com")
    org = await create_org(client, token, "Erase Optout")
    org_id = uuid.UUID(org["id"])
    e164 = "+12145550800"

    contact = await create_contact(client, token, org["id"], "Optout Person", [e164])
    contact_id = uuid.UUID(contact["id"])

    set_org_context(session, org_id)
    session.add(
        ConsentEvent(
            id=uuid.uuid4(),
            org_id=org_id,
            contact_e164=e164,
            channel="sms",
            event="opt_out",
            source="inbound",
        )
    )
    session.add(DncEntry(id=uuid.uuid4(), org_id=org_id, e164=e164, source="manual"))
    await session.commit()

    store = InMemoryObjectStore()

    r = await client.post(
        f"/api/v1/contacts/{contact_id}/erase", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 202, r.text
    request_id = uuid.UUID(r.json()["id"])

    result = await privacy_svc.erasure_tick(session, store)
    assert result["completed"] == 1, result

    set_org_context(session, org_id)
    request_row = await session.get(ErasureRequest, request_id)
    await session.refresh(request_row)
    assert request_row.status == "done"
    assert request_row.summary["consent_rows_kept"] == 1
    assert request_row.summary["dnc_rows_kept"] == 1

    # The opt-out and DNC rows must outlive the person's record; otherwise the next
    # campaign send could see no opt-out and text the erased number again.
    consent_kept = (
        await session.execute(sa.select(sa.func.count(ConsentEvent.id)))
    ).scalar_one()
    dnc_kept = (
        await session.execute(sa.select(sa.func.count(DncEntry.id)))
    ).scalar_one()
    assert consent_kept == 1
    assert dnc_kept == 1


async def test_erasure_anonymises_and_deletes_media(client, session):
    token = await register_and_login(client, "p27-erase-full@example.com")
    org = await create_org(client, token, "Erase Full")
    org_id = uuid.UUID(org["id"])
    e164 = "+12145550900"
    store = InMemoryObjectStore()

    contact = await create_contact(client, token, org["id"], "Jane Doe", [e164])
    contact_id = uuid.UUID(contact["id"])

    set_org_context(session, org_id)
    contact_row = await session.get(Contact, contact_id)
    contact_row.first_name = "Jane"
    contact_row.last_name = "Doe"
    contact_row.attributes = {"dept": "sales"}

    _thread, msg = await _thread_with_message(
        session, org_id, contact_id, e164, body="secret"
    )

    asset_id = uuid.uuid4()
    media_key = f"org/{org_id}/media/{asset_id}"
    session.add(
        MediaAsset(
            id=asset_id,
            org_id=org_id,
            message_id=msg.id,
            direction="inbound",
            storage_key=media_key,
            content_type="image/png",
            status="stored",
        )
    )
    session.add(
        ContactNote(
            id=uuid.uuid4(),
            org_id=org_id,
            contact_id=contact_id,
            body="private note",
        )
    )
    await session.commit()
    await store.put(media_key, b"img", "image/png")

    r = await client.post(
        f"/api/v1/contacts/{contact_id}/erase", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 202, r.text
    request_id = uuid.UUID(r.json()["id"])

    result = await privacy_svc.erasure_tick(session, store)
    assert result["completed"] == 1, result

    set_org_context(session, org_id)

    contact_row = await session.get(Contact, contact_id)
    await session.refresh(contact_row)
    assert contact_row.display_name == "Erased contact"
    assert contact_row.first_name is None
    assert contact_row.last_name is None
    assert contact_row.attributes == {}

    phones = (
        await session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == contact_id)
        )
    ).scalars().all()
    assert phones == []

    notes = (
        await session.execute(
            sa.select(ContactNote).where(ContactNote.contact_id == contact_id)
        )
    ).scalars().all()
    assert notes == []

    msg_row = await session.get(Message, msg.id)
    await session.refresh(msg_row)
    assert msg_row.body == "[erased]"

    asset_row = await session.get(MediaAsset, asset_id)
    await session.refresh(asset_row)
    assert asset_row.status == "purged"
    assert asset_row.storage_key is None
    assert not await store.exists(media_key)

    request_row = await session.get(ErasureRequest, request_id)
    await session.refresh(request_row)
    assert request_row.status == "done"
    assert request_row.completed_at is not None

    audit = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "contact.erase")
        )
    ).scalars().all()
    assert len(audit) >= 1


async def test_erasure_request_requires_compliance_manage(client, session):
    owner_token = await register_and_login(client, "p27-erase-owner@example.com")
    org = await create_org(client, owner_token, "Erase Perm")
    org_id = uuid.UUID(org["id"])

    contact = await create_contact(
        client, owner_token, org["id"], "Perm Person", ["+12145551000"]
    )
    contact_id = contact["id"]

    agent_token, _ = await _register_member(
        client, session, org_id, "p27-erase-agent@example.com", role_name="agent"
    )
    r = await client.post(
        f"/api/v1/contacts/{contact_id}/erase",
        headers=auth_headers(agent_token, org_id),
    )
    assert r.status_code == 403


async def test_retention_and_erasure_flow(client, session):
    token = await register_and_login(client, "p27-flow@example.com")
    org = await create_org(client, token, "Flow")
    org_id = uuid.UUID(org["id"])

    contact_a = await create_contact(
        client, token, org["id"], "Contact A", ["+12145551100"]
    )
    contact_b = await create_contact(
        client, token, org["id"], "Contact B", ["+12145551101"]
    )
    contact_a_id = uuid.UUID(contact_a["id"])
    contact_b_id = uuid.UUID(contact_b["id"])

    set_org_context(session, org_id)
    session.add(
        ContactNote(
            id=uuid.uuid4(), org_id=org_id, contact_id=contact_a_id, body="note a"
        )
    )
    session.add(
        ContactNote(
            id=uuid.uuid4(), org_id=org_id, contact_id=contact_b_id, body="note b"
        )
    )
    await session.commit()

    r = await client.patch(
        "/api/v1/orgs/current/retention",
        json={"messages_days": 30},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    store = InMemoryObjectStore()
    future = datetime.now(timezone.utc) + timedelta(days=60)
    await retention_svc.retention_tick(session, store, now=future)

    r = await client.post(
        f"/api/v1/contacts/{contact_a_id}/erase",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 202, r.text
    await privacy_svc.erasure_tick(session, store)

    set_org_context(session, org_id)

    row_a = await session.get(Contact, contact_a_id)
    await session.refresh(row_a)
    assert row_a.display_name == "Erased contact"
    a_phones = (
        await session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == contact_a_id)
        )
    ).scalars().all()
    assert a_phones == []
    a_notes = (
        await session.execute(
            sa.select(ContactNote).where(ContactNote.contact_id == contact_a_id)
        )
    ).scalars().all()
    assert a_notes == []

    row_b = await session.get(Contact, contact_b_id)
    await session.refresh(row_b)
    assert row_b.display_name == "Contact B"
    b_phones = (
        await session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == contact_b_id)
        )
    ).scalars().all()
    assert len(b_phones) == 1
    b_notes = (
        await session.execute(
            sa.select(ContactNote).where(ContactNote.contact_id == contact_b_id)
        )
    ).scalars().all()
    assert len(b_notes) == 1


async def test_import_summary_persisted_and_returned(client, session):
    token = await register_and_login(client, "p27-import-summary@example.com")
    org = await create_org(client, token, "Import Summary")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    list_id = uuid.uuid4()
    session.add(
        ContactList(
            id=list_id,
            org_id=org_id,
            name="Import List",
            source_filename="people.csv",
            status="ready",
            import_summary={},
        )
    )
    await session.commit()

    summary = await list_import_svc.run_import(
        get_sessionmaker(),
        list_id=list_id,
        org_id=org_id,
        filename="people.csv",
        data=(
            b"phone,first_name,owner\n"
            b"+12145551234,Ann,nobody@example.com\n"
        ),
        mapping={"phone": "phone", "first_name": "first_name", "owner": "owner"},
    )

    assert "unknown_owner_emails" in summary
    assert "nobody@example.com" in summary["unknown_owner_emails"]
    assert "assigned" in summary
    assert "counts" in summary

    set_org_context(session, org_id)
    session.expire_all()
    lst_row = await session.get(ContactList, list_id)
    await session.refresh(lst_row)
    persisted = lst_row.import_summary or {}
    assert "nobody@example.com" in persisted["unknown_owner_emails"]
    assert "assigned" in persisted
    assert "counts" in persisted

    r = await client.get(
        "/api/v1/outbound/lists", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200
