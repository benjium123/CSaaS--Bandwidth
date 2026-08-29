"""P12 services/voicemail.py: creation from a flow terminal (+ the durable outbox event,
D14), recording linkage after `recordings.on_recording_ready`, and the DR-8 transcription
seam (no key -> disabled; a mocked transport -> transcript stored, done).
"""

from __future__ import annotations

import json
import uuid

import httpx
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models.callflow import Voicemail
from app.models.org import Org
from app.models.platform import PlatformEvent
from app.models.voice import Call, CallRecording
from app.services import voicemail as voicemail_svc
from app.storage.base import build_store


async def _make_org(session) -> uuid.UUID:
    """A real Org row - Call/CallRecording/Voicemail are all TenantScoped with a genuine
    FK to orgs.id (SQLite enforces it here - conftest.py's PRAGMA foreign_keys=ON)."""
    org = Org(id=uuid.uuid4(), name="Test Org", slug=f"org-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()
    return org.id


def _make_call(org_id: uuid.UUID) -> Call:
    return Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="bandwidth",
        status="answered",
    )


async def test_create_from_flow_writes_voicemail_and_outbox_event_in_same_transaction(
    session, engine
):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()

    class _Flow:
        id = uuid.uuid4()

    vm = await voicemail_svc.create_from_flow(
        session, call, _Flow(), node_id="voicemail-node", greeting="leave one"
    )
    await session.commit()

    stored = await session.get(Voicemail, vm.id)
    assert stored is not None
    assert stored.call_id == call.id
    assert stored.status == "new"
    assert stored.transcript_status == "pending"
    assert stored.greeting_node == "voicemail-node"

    events = (
        (
            await session.execute(
                sa.select(PlatformEvent).where(PlatformEvent.event_type == "voicemail.created")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["voicemail_id"] == str(vm.id)
    assert events[0].payload["call_id"] == str(call.id)


async def test_link_pending_recordings_matches_by_call_id(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()

    vm = Voicemail(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        recording_id=None,
        greeting_node="vm",
        transcript=None,
        transcript_status="pending",
        status="new",
    )
    session.add(vm)
    recording = CallRecording(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        leg_id=None,
        provider_recording_id="rec-1",
        storage_key="org/x/recordings/1",
        status="pending",
    )
    session.add(recording)
    await session.commit()

    linked = await voicemail_svc.link_pending_recordings(session)
    assert linked == 1
    await session.refresh(vm)
    assert vm.recording_id == recording.id


async def test_transcribe_pending_without_key_marks_disabled(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    recording = CallRecording(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        leg_id=None,
        provider_recording_id="rec-2",
        storage_key="org/x/recordings/2",
        status="stored",
        content_type="audio/mpeg",
    )
    session.add(recording)
    await session.flush()
    vm = Voicemail(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        recording_id=recording.id,
        greeting_node="vm",
        transcript=None,
        transcript_status="pending",
        status="new",
    )
    session.add(vm)
    await session.commit()

    store = build_store("memory")
    counts = await voicemail_svc.transcribe_pending(session, store, deepgram_api_key="")
    assert counts == {"done": 0, "failed": 0, "disabled": 1}
    await session.refresh(vm)
    assert vm.transcript_status == "disabled"


async def test_transcribe_pending_with_mocked_deepgram_stores_transcript(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    recording = CallRecording(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        leg_id=None,
        provider_recording_id="rec-3",
        storage_key="org/x/recordings/3",
        status="stored",
        content_type="audio/mpeg",
    )
    session.add(recording)
    await session.flush()
    vm = Voicemail(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        recording_id=recording.id,
        greeting_node="vm",
        transcript=None,
        transcript_status="pending",
        status="new",
    )
    session.add(vm)
    await session.commit()

    store = build_store("memory")
    await store.put(recording.storage_key, b"fake-audio-bytes", "audio/mpeg")

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Token dg-test-key"
        payload = {
            "results": {"channels": [{"alternatives": [{"transcript": "hello, leave a message"}]}]}
        }
        return httpx.Response(200, content=json.dumps(payload))

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        counts = await voicemail_svc.transcribe_pending(
            session, store, deepgram_api_key="dg-test-key", client=client
        )
    finally:
        await client.aclose()

    assert counts == {"done": 1, "failed": 0, "disabled": 0}
    await session.refresh(vm)
    assert vm.transcript_status == "done"
    assert vm.transcript == "hello, leave a message"


async def test_transcribe_pending_skips_recording_not_yet_stored(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    recording = CallRecording(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        leg_id=None,
        provider_recording_id="rec-4",
        storage_key="org/x/recordings/4",
        status="pending",
    )
    session.add(recording)
    await session.flush()
    vm = Voicemail(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        recording_id=recording.id,
        greeting_node="vm",
        transcript=None,
        transcript_status="pending",
        status="new",
    )
    session.add(vm)
    await session.commit()

    store = build_store("memory")
    counts = await voicemail_svc.transcribe_pending(session, store, deepgram_api_key="dg-test-key")
    assert counts == {"done": 0, "failed": 0, "disabled": 0}
    await session.refresh(vm)
    assert vm.transcript_status == "pending"
