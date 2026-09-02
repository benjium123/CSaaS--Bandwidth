from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
import pytest

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    Call,
    CallLeg,
    CallRecording,
    Contact,
    ContactPhone,
    Inbox,
    InboxGrant,
    Message,
    MessageThread,
    OrgMembership,
    OrgNumber,
    Role,
    VoiceEvent,
    Voicemail,
)
from app.providers.voice import CreateCallResult
from app.repositories import users as users_repo
from tests.conftest import (
    FROZEN_NOW,
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier


@pytest.fixture
async def app_with_voice_carrier(engine):
    """App wired with a FakeVoiceCarrier - local to this file (mirrors test_voice_api.py's
    own copy) so a rejected POST /api/v1/calls exercises the REAL
    services/calls.py::create_outbound_call rejection path, which is what stamps
    CallLeg.extra["error_detail"] (services/calls.py:281-283) that
    _extract_failure_detail must read first (P16 Opus review point 3)."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


# ----------------------------------------------------------------------------------
# Direct DB helpers
# ----------------------------------------------------------------------------------
async def _make_number(
    session, org_id: uuid.UUID, e164: str
) -> tuple[OrgNumber, Inbox]:
    set_org_context(session, org_id)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164=e164,
        carrier="bandwidth",
        capabilities={},
        status="active",
    )
    session.add(number)
    await session.flush()
    inbox = Inbox(id=uuid.uuid4(), org_id=org_id, name=e164, number_id=number.id)
    session.add(inbox)
    await session.commit()
    return number, inbox


async def _make_thread(
    session,
    org_id: uuid.UUID,
    our_e164: str,
    contact_e164: str,
    *,
    last_message_at: datetime | None = None,
    status: str = "open",
    last_read_at: datetime | None = None,
) -> MessageThread:
    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        last_message_at=last_message_at,
        status=status,
        last_read_at=last_read_at,
        ai_state="off",
    )
    session.add(thread)
    await session.commit()
    return thread


async def _make_message(
    session,
    org_id: uuid.UUID,
    thread: MessageThread,
    *,
    direction: str,
    body: str,
    created_at: datetime,
    status: str | None = None,
) -> Message:
    set_org_context(session, org_id)
    if status is None:
        status = "delivered" if direction == "outbound" else "received"
    from_e164 = thread.our_e164 if direction == "outbound" else thread.contact_e164
    to_e164 = thread.contact_e164 if direction == "outbound" else thread.our_e164
    msg = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction=direction,
        status=status,
        from_e164=from_e164,
        to_e164=to_e164,
        body=body,
        media=[],
        carrier="bandwidth",
        created_at=created_at,
    )
    session.add(msg)
    await session.commit()
    return msg


async def _make_call(
    session,
    org_id: uuid.UUID,
    *,
    our_e164: str,
    contact_e164: str,
    direction: str,
    status: str,
    created_at: datetime,
    ended_at: datetime | None = None,
    answered_at: datetime | None = None,
    duration_seconds: int | None = None,
) -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        direction=direction,
        carrier="bandwidth",
        status=status,
        ended_at=ended_at,
        answered_at=answered_at,
        duration_seconds=duration_seconds,
        extra={},
        created_at=created_at,
    )
    session.add(call)
    await session.commit()
    return call


async def _make_voicemail(
    session,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    transcript: str | None = None,
    transcript_status: str = "done",
    created_at: datetime,
    recording_id: uuid.UUID | None = None,
) -> Voicemail:
    set_org_context(session, org_id)
    vm = Voicemail(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call_id,
        recording_id=recording_id,
        transcript=transcript,
        transcript_status=transcript_status,
        status="new",
        created_at=created_at,
    )
    session.add(vm)
    await session.commit()
    return vm


async def _make_voice_event(
    session,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    event_type: str,
    payload: dict,
    created_at: datetime,
) -> VoiceEvent:
    set_org_context(session, org_id)
    event = VoiceEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call_id,
        carrier="bandwidth",
        provider_event_id=str(uuid.uuid4()),
        event_type=event_type,
        payload=payload,
        occurred_at=created_at,
        created_at=created_at,
    )
    session.add(event)
    await session.commit()
    return event


async def _make_call_recording(
    session,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    duration_seconds: int | None = None,
    status: str = "stored",
    created_at: datetime | None = None,
) -> CallRecording:
    set_org_context(session, org_id)
    recording = CallRecording(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call_id,
        provider_recording_id=str(uuid.uuid4()),
        storage_key=f"test/{uuid.uuid4()}",
        content_type="audio/mpeg",
        duration_seconds=duration_seconds,
        status=status,
        created_at=created_at,
    )
    session.add(recording)
    await session.commit()
    return recording


async def _register_user_with_role(
    client: httpx.AsyncClient,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
) -> tuple[str, object]:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    assert user is not None

    set_org_context(session, org_id)
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"role-{email.split('@')[0]}",
        permissions=permissions,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=user.id,
            role_id=role.id,
        )
    )
    await session.commit()
    return token, user


# ----------------------------------------------------------------------------------
# P16 test suite
# ----------------------------------------------------------------------------------
async def test_list_merges_thread_only_call_only_and_both(client, session):
    owner_token = await register_and_login(client, "p16a@example.com")
    org = await create_org(client, owner_token, "P16 Org A")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    B = "+12145550101"
    c1 = "+19725550111"
    c2 = "+19725550122"
    c3 = "+19725550133"

    _, inbox_a = await _make_number(session, org_id, A)
    await _make_number(session, org_id, B)

    t1 = FROZEN_NOW - timedelta(hours=1)
    thread_only = await _make_thread(session, org_id, A, c1, last_message_at=t1)
    await _make_message(
        session, org_id, thread_only, direction="outbound", body="hello there", created_at=t1
    )

    t2 = FROZEN_NOW - timedelta(minutes=50)
    await _make_call(
        session,
        org_id,
        our_e164=B,
        contact_e164=c2,
        direction="inbound",
        status="no_answer",
        created_at=t2,
        ended_at=t2,
    )

    t3 = FROZEN_NOW - timedelta(minutes=40)
    thread_both = await _make_thread(session, org_id, A, c3, last_message_at=t3)
    await _make_message(
        session, org_id, thread_both, direction="inbound", body="question", created_at=t3
    )

    t4 = FROZEN_NOW - timedelta(minutes=30)
    call_both = await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c3,
        direction="inbound",
        status="completed",
        created_at=t4,
        ended_at=t4,
        answered_at=t4,
    )
    await _make_voicemail(
        session, org_id, call_both.id, transcript="I left a message", created_at=t4
    )

    r = await client.get("/api/v1/conversations", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    by_contact = {item["contact_e164"]: item for item in body["items"]}
    assert set(by_contact) == {c1, c2, c3}

    item1 = by_contact[c1]
    assert item1["last_event_type"] == "message"
    assert item1["direction"] == "outbound"
    assert item1["snippet"] == "You: hello there"
    assert item1["thread_id"] == str(thread_only.id)
    assert item1["unread"] is False

    item2 = by_contact[c2]
    assert item2["last_event_type"] == "call"
    assert item2["direction"] == "inbound"
    assert item2["snippet"] == "Missed call"
    assert item2["thread_id"] is None
    assert item2["status"] == "open"

    item3 = by_contact[c3]
    assert item3["last_event_type"] == "voicemail"
    assert item3["snippet"] == "Voicemail: I left a message"
    assert item3["direction"] == "inbound"
    assert item3["inbox_id"] == str(inbox_a.id)


async def test_tab_calls_excludes_message_last_pairs(client, session):
    owner_token = await register_and_login(client, "p16b@example.com")
    org = await create_org(client, owner_token, "P16 Org B")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    c_msg = "+19725550111"
    c_call = "+19725550122"
    c_both = "+19725550133"
    await _make_number(session, org_id, A)

    t_msg = FROZEN_NOW - timedelta(minutes=10)
    thread_msg = await _make_thread(session, org_id, A, c_msg, last_message_at=t_msg)
    await _make_message(
        session, org_id, thread_msg, direction="outbound", body="hi", created_at=t_msg
    )

    t_call = FROZEN_NOW - timedelta(minutes=20)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_call,
        direction="inbound",
        status="no_answer",
        created_at=t_call,
        ended_at=t_call,
    )

    t_both_msg = FROZEN_NOW - timedelta(minutes=15)
    thread_both = await _make_thread(session, org_id, A, c_both, last_message_at=t_both_msg)
    await _make_message(
        session, org_id, thread_both, direction="outbound", body="later", created_at=t_both_msg
    )
    t_both_call = FROZEN_NOW - timedelta(minutes=30)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_both,
        direction="inbound",
        status="completed",
        created_at=t_both_call,
        ended_at=t_both_call,
    )

    r = await client.get("/api/v1/conversations", params={"tab": "calls"}, headers=h)
    assert r.status_code == 200, r.text
    contacts = [item["contact_e164"] for item in r.json()["items"]]
    assert contacts == [c_call]


async def test_filter_unread_honors_last_read_at(client, session):
    owner_token = await register_and_login(client, "p16c@example.com")
    org = await create_org(client, owner_token, "P16 Org C")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    c_unread = "+19725550111"
    c_read = "+19725550122"
    c_outbound = "+19725550133"
    await _make_number(session, org_id, A)

    t_unread = FROZEN_NOW - timedelta(minutes=5)
    thread_unread = await _make_thread(
        session, org_id, A, c_unread, last_message_at=t_unread, last_read_at=None
    )
    await _make_message(
        session, org_id, thread_unread, direction="inbound", body="unread", created_at=t_unread
    )

    t_read = FROZEN_NOW - timedelta(minutes=10)
    thread_read = await _make_thread(
        session, org_id, A, c_read, last_message_at=t_read, last_read_at=t_read
    )
    await _make_message(
        session, org_id, thread_read, direction="inbound", body="read", created_at=t_read
    )

    t_out = FROZEN_NOW - timedelta(minutes=15)
    thread_out = await _make_thread(session, org_id, A, c_outbound, last_message_at=t_out)
    await _make_message(
        session, org_id, thread_out, direction="outbound", body="out", created_at=t_out
    )

    r = await client.get("/api/v1/conversations", params={"filter": "unread"}, headers=h)
    assert r.status_code == 200, r.text
    contacts = [item["contact_e164"] for item in r.json()["items"]]
    assert contacts == [c_unread]


async def test_p15_agent_no_grants_viewer_grant_and_ungranted_404(client, session):
    owner_token = await register_and_login(client, "p16d@example.com")
    org = await create_org(client, owner_token, "P16 Org D")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    B = "+12145550101"
    _, inbox_a = await _make_number(session, org_id, A)
    _, inbox_b = await _make_number(session, org_id, B)

    c_a = "+19725550111"
    c_b = "+19725550122"
    t = FROZEN_NOW - timedelta(minutes=5)

    thread_a = await _make_thread(session, org_id, A, c_a, last_message_at=t)
    await _make_message(
        session, org_id, thread_a, direction="inbound", body="hi", created_at=t
    )
    thread_b = await _make_thread(session, org_id, B, c_b, last_message_at=t)
    await _make_message(
        session, org_id, thread_b, direction="inbound", body="hi", created_at=t
    )

    agent_token, agent_user = await _register_user_with_role(
        client, session, org_id, "agent-p16d@example.com", ["inbox:read"]
    )
    h_agent = auth_headers(agent_token, org["id"])

    r = await client.get("/api/v1/conversations", headers=h_agent)
    assert r.status_code == 200
    assert r.json()["items"] == []

    set_org_context(session, org_id)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox_a.id,
            grantee_type="user",
            grantee_id=agent_user.id,
            role="viewer",
        )
    )
    await session.commit()

    r = await client.get("/api/v1/conversations", headers=h_agent)
    assert r.status_code == 200
    contacts = [item["contact_e164"] for item in r.json()["items"]]
    assert contacts == [c_a]
    assert all(item["inbox_id"] == str(inbox_a.id) for item in r.json()["items"])

    denied = await client.get(
        "/api/v1/conversations",
        params={"inbox_id": str(inbox_b.id)},
        headers=h_agent,
    )
    assert denied.status_code == 404


async def test_timeline_order_and_failure_detail_from_voice_event(client, session):
    owner_token = await register_and_login(client, "p16e@example.com")
    org = await create_org(client, owner_token, "P16 Org E")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    contact = "+19725550999"
    await _make_number(session, org_id, A)

    t_old = FROZEN_NOW - timedelta(minutes=20)
    thread = await _make_thread(
        session,
        org_id,
        A,
        contact,
        last_message_at=FROZEN_NOW - timedelta(minutes=5),
    )
    await _make_message(
        session, org_id, thread, direction="inbound", body="old", created_at=t_old
    )

    t_call = FROZEN_NOW - timedelta(minutes=15)
    call = await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=contact,
        direction="outbound",
        status="failed",
        created_at=t_call,
    )
    await _make_voice_event(
        session,
        org_id,
        call.id,
        event_type="error",
        payload={"error": '{"description":"Call could not be completed"}'},
        created_at=t_call,
    )

    rec = await _make_call_recording(
        session,
        org_id,
        call.id,
        duration_seconds=42,
        status="stored",
        created_at=FROZEN_NOW - timedelta(minutes=14),
    )

    await _make_voicemail(
        session,
        org_id,
        call.id,
        transcript="Please call back",
        created_at=FROZEN_NOW - timedelta(minutes=10),
        recording_id=rec.id,
    )

    t_new = FROZEN_NOW - timedelta(minutes=5)
    await _make_message(
        session, org_id, thread, direction="outbound", body="new message", created_at=t_new
    )

    url = f"/api/v1/conversations/{quote(contact, safe='')}/timeline"
    r = await client.get(url, params={"our_e164": A}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    kinds = [item["kind"] for item in body["items"]]
    assert kinds == ["message", "voicemail", "call", "message"]

    assert body["items"][0]["body"] == "new message"
    assert body["items"][1]["transcript"] == "Please call back"
    assert body["items"][1]["duration_seconds"] == 42
    # Follow-up: VoicemailTimelineEvent.recording exposes the same {id, status,
    # duration_seconds} shape CallTimelineEvent.recording does, from the same
    # recordings_by_id lookup already used for duration_seconds above.
    assert body["items"][1]["recording"] == {
        "id": str(rec.id),
        "status": "stored",
        "duration_seconds": 42,
    }
    assert body["items"][2]["failure_detail"] == "Call could not be completed"
    assert body["items"][2]["has_voicemail"] is True
    assert body["items"][2]["recording"] == {
        "id": str(rec.id),
        "status": "stored",
        "duration_seconds": 42,
    }
    assert body["items"][3]["body"] == "old"


async def test_cursor_pagination_disjoint_pages(client, session):
    owner_token = await register_and_login(client, "p16f@example.com")
    org = await create_org(client, owner_token, "P16 Org F")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    contacts = [f"+1972555100{i}" for i in range(1, 6)]
    for idx, contact in enumerate(contacts):
        t = FROZEN_NOW - timedelta(minutes=idx)
        thread = await _make_thread(session, org_id, A, contact, last_message_at=t)
        await _make_message(
            session,
            org_id,
            thread,
            direction="outbound",
            body=f"body {idx}",
            created_at=t,
        )

    all_items = []
    cursor = None
    pages = 0

    while True:
        params: dict = {"limit": 2}
        if cursor:
            params["cursor"] = cursor

        r = await client.get("/api/v1/conversations", params=params, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        all_items.extend(body["items"])
        pages += 1
        cursor = body["next_cursor"]

        if cursor is None:
            break
        if pages > 10:
            raise AssertionError("too many pages")

    assert len(all_items) == 5
    keys = [(item["our_e164"], item["contact_e164"]) for item in all_items]
    assert len(keys) == len(set(keys)) == 5


async def test_timeline_cursor_pagination_stable_across_live_insert(client, session):
    """Plan bullet: "cursor pagination stable across a live insert". A client holding a
    cursor from page 1 must see page 2 remain disjoint from page 1 even when a new event
    lands (older than the cursor, i.e. inside the still-unserved range) between requests.
    """
    owner_token = await register_and_login(client, "p16g@example.com")
    org = await create_org(client, owner_token, "P16 Org G")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    contact = "+19725550777"
    await _make_number(session, org_id, A)

    thread = await _make_thread(session, org_id, A, contact, last_message_at=FROZEN_NOW)

    t1 = FROZEN_NOW - timedelta(minutes=40)
    t2 = FROZEN_NOW - timedelta(minutes=30)
    t3 = FROZEN_NOW - timedelta(minutes=20)
    t4 = FROZEN_NOW - timedelta(minutes=10)

    m1 = await _make_message(
        session, org_id, thread, direction="outbound", body="m1", created_at=t1
    )
    m2 = await _make_message(
        session, org_id, thread, direction="outbound", body="m2", created_at=t2
    )
    m3 = await _make_message(
        session, org_id, thread, direction="outbound", body="m3", created_at=t3
    )
    m4 = await _make_message(
        session, org_id, thread, direction="outbound", body="m4", created_at=t4
    )

    url = f"/api/v1/conversations/{quote(contact, safe='')}/timeline"

    r1 = await client.get(url, params={"our_e164": A, "limit": 2}, headers=h)
    assert r1.status_code == 200, r1.text
    page1 = r1.json()
    assert [item["id"] for item in page1["items"]] == [str(m4.id), str(m3.id)]
    cursor = page1["next_cursor"]
    assert cursor is not None

    # Live insert landing strictly between t2 and t3 - i.e. older than the cursor (t3),
    # so it belongs to the still-unserved range a page-2 fetch is about to walk into.
    t_new = FROZEN_NOW - timedelta(minutes=25)
    m_new = await _make_message(
        session, org_id, thread, direction="inbound", body="live insert", created_at=t_new
    )

    r2 = await client.get(
        url, params={"our_e164": A, "limit": 10, "cursor": cursor}, headers=h
    )
    assert r2.status_code == 200, r2.text
    page2_ids = [item["id"] for item in r2.json()["items"]]

    # Already-served items never reappear...
    assert str(m4.id) not in page2_ids
    assert str(m3.id) not in page2_ids
    # ...and every not-yet-served item (including the live insert) appears exactly once,
    # in strict chronological order.
    assert page2_ids == [str(m_new.id), str(m2.id), str(m1.id)]


async def test_timeline_failure_detail_falls_back_to_leg_hangup_cause(client, session):
    """Plan: failure detail comes from CallLeg.hangup_cause OR the latest VoiceEvent
    payload. This covers the CallLeg path when no VoiceEvent carries a description."""
    owner_token = await register_and_login(client, "p16h@example.com")
    org = await create_org(client, owner_token, "P16 Org H")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    contact = "+19725550888"
    await _make_number(session, org_id, A)

    t_call = FROZEN_NOW - timedelta(minutes=5)
    call = await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=contact,
        direction="outbound",
        status="failed",
        created_at=t_call,
    )

    set_org_context(session, org_id)
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id=str(uuid.uuid4()),
        to_e164=contact,
        from_e164=A,
        status="failed",
        reason="original",
        hangup_cause="normal-temporary-failure",
        created_at=t_call,
    )
    session.add(leg)
    await session.commit()

    url = f"/api/v1/conversations/{quote(contact, safe='')}/timeline"
    r = await client.get(url, params={"our_e164": A}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "call"
    assert items[0]["failure_detail"] == "normal-temporary-failure"


# ----------------------------------------------------------------------------------
# P16 Opus review fix list
# ----------------------------------------------------------------------------------
async def test_calls_read_permission_gates_call_and_voicemail_visibility(client, session):
    """BLOCKER 1: inbox:read alone must not expose call/voicemail-derived data - a caller
    additionally needs calls:read. The "inbox:read"-only role used by the P15 test above
    only ever exercises message threads, so it stays valid unchanged; this test is the
    dedicated coverage for the calls:read gate itself, on both endpoints."""
    owner_token = await register_and_login(client, "p16i@example.com")
    org = await create_org(client, owner_token, "P16 Org I")
    org_id = uuid.UUID(org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    c_msg = "+19725550201"
    c_call = "+19725550202"

    t = FROZEN_NOW - timedelta(minutes=10)
    thread = await _make_thread(session, org_id, A, c_msg, last_message_at=t)
    await _make_message(session, org_id, thread, direction="outbound", body="hi", created_at=t)

    t_call = FROZEN_NOW - timedelta(minutes=5)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_call,
        direction="inbound",
        status="no_answer",
        created_at=t_call,
        ended_at=t_call,
    )

    # inboxes:admin sidesteps the P15 tier so only the calls:read gate is under test.
    limited_token, _limited_user = await _register_user_with_role(
        client, session, org_id, "limited-p16i@example.com", ["inbox:read", "inboxes:admin"]
    )
    h_limited = auth_headers(limited_token, org["id"])

    r = await client.get("/api/v1/conversations", headers=h_limited)
    assert r.status_code == 200, r.text
    assert [item["contact_e164"] for item in r.json()["items"]] == [c_msg]

    calls_tab = await client.get(
        "/api/v1/conversations", params={"tab": "calls"}, headers=h_limited
    )
    assert calls_tab.status_code == 200, calls_tab.text
    assert calls_tab.json()["items"] == []

    timeline_url = f"/api/v1/conversations/{quote(c_call, safe='')}/timeline"
    tl = await client.get(timeline_url, params={"our_e164": A}, headers=h_limited)
    assert tl.status_code == 200, tl.text
    assert tl.json()["items"] == []

    # The agent SYSTEM_ROLE now carries calls:read (rbac.py SYSTEM_ROLES["agent"]).
    agent_token, _agent_user = await _register_user_with_role(
        client,
        session,
        org_id,
        "agent-p16i@example.com",
        ["inbox:read", "inboxes:admin", "calls:read"],
    )
    h_agent = auth_headers(agent_token, org["id"])

    r2 = await client.get("/api/v1/conversations", headers=h_agent)
    assert r2.status_code == 200, r2.text
    assert {item["contact_e164"] for item in r2.json()["items"]} == {c_msg, c_call}

    calls_tab2 = await client.get(
        "/api/v1/conversations", params={"tab": "calls"}, headers=h_agent
    )
    assert [item["contact_e164"] for item in calls_tab2.json()["items"]] == [c_call]

    tl2 = await client.get(timeline_url, params={"our_e164": A}, headers=h_agent)
    assert tl2.status_code == 200, tl2.text
    assert [item["kind"] for item in tl2.json()["items"]] == ["call"]


async def test_list_nonexistent_inbox_id_is_404(client, session):
    """BLOCKER 2 (second half): a syntactically valid but nonexistent inbox_id on the
    list endpoint is a 404, not an empty 200 or a 500."""
    owner_token = await register_and_login(client, "p16j@example.com")
    org = await create_org(client, owner_token, "P16 Org J")
    h = auth_headers(owner_token, org["id"])

    r = await client.get(
        "/api/v1/conversations", params={"inbox_id": str(uuid.uuid4())}, headers=h
    )
    assert r.status_code == 404


async def test_timeline_p15_gate_ungranted_our_e164_404_admin_sees_both(client, session):
    """BLOCKER 2 (first half): an agent granted only inbox A gets 404 requesting the
    timeline for inbox B's number; the org owner (admin) sees both."""
    owner_token = await register_and_login(client, "p16k@example.com")
    org = await create_org(client, owner_token, "P16 Org K")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    B = "+12145550101"
    _, inbox_a = await _make_number(session, org_id, A)
    await _make_number(session, org_id, B)

    c_a = "+19725550301"
    c_b = "+19725550302"
    t = FROZEN_NOW - timedelta(minutes=5)
    thread_a = await _make_thread(session, org_id, A, c_a, last_message_at=t)
    await _make_message(session, org_id, thread_a, direction="inbound", body="a", created_at=t)
    thread_b = await _make_thread(session, org_id, B, c_b, last_message_at=t)
    await _make_message(session, org_id, thread_b, direction="inbound", body="b", created_at=t)

    agent_token, agent_user = await _register_user_with_role(
        client, session, org_id, "agent-p16k@example.com", ["inbox:read", "calls:read"]
    )
    h_agent = auth_headers(agent_token, org["id"])

    set_org_context(session, org_id)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox_a.id,
            grantee_type="user",
            grantee_id=agent_user.id,
            role="viewer",
        )
    )
    await session.commit()

    url_a = f"/api/v1/conversations/{quote(c_a, safe='')}/timeline"
    url_b = f"/api/v1/conversations/{quote(c_b, safe='')}/timeline"

    ok = await client.get(url_a, params={"our_e164": A}, headers=h_agent)
    assert ok.status_code == 200, ok.text

    denied = await client.get(url_b, params={"our_e164": B}, headers=h_agent)
    assert denied.status_code == 404

    admin_a = await client.get(url_a, params={"our_e164": A}, headers=h_owner)
    admin_b = await client.get(url_b, params={"our_e164": B}, headers=h_owner)
    assert admin_a.status_code == 200, admin_a.text
    assert admin_b.status_code == 200, admin_b.text


async def test_failure_detail_prefers_call_leg_extra_error_detail_over_everything(
    app_with_voice_carrier, session
):
    """BLOCKER 3: through the REAL rejection path (POST /api/v1/calls -> a carrier
    rejection -> services/calls.py:281-283 stamping CallLeg.extra["error_detail"]) - a
    raw Bandwidth-402-shaped JSON body's "description" must surface, ranked above any
    VoiceEvent or hangup_cause."""
    client, fake, _app = app_with_voice_carrier
    fake.scripted_results = [
        CreateCallResult(
            "rejected",
            None,
            '{"type":"payment-required","description":"Insufficient account balance"}',
        )
    ]
    OUR = "+12145550100"
    THEIRS = "+19725550199"
    token, org, _ = await make_org_with_number(client, "p16fail@example.com", "Org Fail", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "failed"

    url = f"/api/v1/conversations/{quote(THEIRS, safe='')}/timeline"
    tl = await client.get(url, params={"our_e164": OUR}, headers=h)
    assert tl.status_code == 200, tl.text
    items = tl.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "call"
    assert items[0]["failure_detail"] == "Insufficient account balance"


async def test_cursor_tiebreak_same_contact_two_numbers_same_timestamp_no_drop(
    client, session
):
    """MAJOR 5: cursor key is (last_event_at, our_e164, contact_e164). Two pairs sharing
    a contact across two of our numbers with an IDENTICAL last_event_at must never
    collide/drop at a page boundary."""
    owner_token = await register_and_login(client, "p16l@example.com")
    org = await create_org(client, owner_token, "P16 Org L")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    B = "+12145550101"
    await _make_number(session, org_id, A)
    await _make_number(session, org_id, B)

    contact = "+19725550401"
    t = FROZEN_NOW - timedelta(minutes=5)

    thread_a = await _make_thread(session, org_id, A, contact, last_message_at=t)
    await _make_message(
        session, org_id, thread_a, direction="outbound", body="via A", created_at=t
    )
    thread_b = await _make_thread(session, org_id, B, contact, last_message_at=t)
    await _make_message(
        session, org_id, thread_b, direction="outbound", body="via B", created_at=t
    )

    all_items = []
    cursor = None
    pages = 0
    while True:
        params: dict = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/conversations", params=params, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        all_items.extend(body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        if pages > 5:
            raise AssertionError("too many pages")

    assert len(all_items) == 2
    keys = {(item["our_e164"], item["contact_e164"]) for item in all_items}
    assert keys == {(A, contact), (B, contact)}


async def test_call_only_pair_resolves_contact_via_contact_phone(client, session):
    """MAJOR 6: a call-only pair (no thread, so no thread.contact_id) still resolves its
    Contact through a batch ContactPhone.e164 lookup."""
    owner_token = await register_and_login(client, "p16m@example.com")
    org = await create_org(client, owner_token, "P16 Org M")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)
    c = "+19725550501"

    set_org_context(session, org_id)
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="Known Caller", attributes={})
    session.add(contact)
    await session.flush()
    session.add(
        ContactPhone(
            id=uuid.uuid4(),
            org_id=org_id,
            contact_id=contact.id,
            e164=c,
            label="mobile",
            is_primary=True,
        )
    )
    await session.commit()

    t = FROZEN_NOW - timedelta(minutes=5)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c,
        direction="inbound",
        status="completed",
        created_at=t,
        ended_at=t,
        answered_at=t,
    )

    r = await client.get("/api/v1/conversations", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["contact"]["display_name"] == "Known Caller"


async def test_filter_unresponded(client, session):
    """MAJOR 7: filter=unresponded covers an un-replied inbound message AND a missed
    inbound call, and excludes an already-answered thread."""
    owner_token = await register_and_login(client, "p16n@example.com")
    org = await create_org(client, owner_token, "P16 Org N")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    c_unresponded_msg = "+19725550601"
    c_responded_msg = "+19725550602"
    c_missed_call = "+19725550603"

    t1 = FROZEN_NOW - timedelta(minutes=10)
    th1 = await _make_thread(session, org_id, A, c_unresponded_msg, last_message_at=t1)
    await _make_message(session, org_id, th1, direction="inbound", body="hey", created_at=t1)

    t2 = FROZEN_NOW - timedelta(minutes=9)
    th2 = await _make_thread(session, org_id, A, c_responded_msg, last_message_at=t2)
    await _make_message(
        session, org_id, th2, direction="outbound", body="hi back", created_at=t2
    )

    t3 = FROZEN_NOW - timedelta(minutes=8)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_missed_call,
        direction="inbound",
        status="no_answer",
        created_at=t3,
        ended_at=t3,
    )

    r = await client.get(
        "/api/v1/conversations", params={"filter": "unresponded"}, headers=h
    )
    assert r.status_code == 200, r.text
    contacts = {item["contact_e164"] for item in r.json()["items"]}
    assert contacts == {c_unresponded_msg, c_missed_call}


async def test_filter_open_excludes_closed_and_all_includes_everything(client, session):
    """MAJOR 7: filter=open excludes a closed thread; filter=all includes it."""
    owner_token = await register_and_login(client, "p16o@example.com")
    org = await create_org(client, owner_token, "P16 Org O")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    c_open = "+19725550701"
    c_closed = "+19725550702"

    t1 = FROZEN_NOW - timedelta(minutes=5)
    th_open = await _make_thread(session, org_id, A, c_open, last_message_at=t1, status="open")
    await _make_message(session, org_id, th_open, direction="outbound", body="open", created_at=t1)

    t2 = FROZEN_NOW - timedelta(minutes=4)
    th_closed = await _make_thread(
        session, org_id, A, c_closed, last_message_at=t2, status="closed"
    )
    await _make_message(
        session, org_id, th_closed, direction="outbound", body="closed", created_at=t2
    )

    r_open = await client.get("/api/v1/conversations", params={"filter": "open"}, headers=h)
    assert r_open.status_code == 200, r_open.text
    assert {item["contact_e164"] for item in r_open.json()["items"]} == {c_open}

    r_all = await client.get("/api/v1/conversations", params={"filter": "all"}, headers=h)
    assert r_all.status_code == 200, r_all.text
    assert {item["contact_e164"] for item in r_all.json()["items"]} == {c_open, c_closed}


async def test_q_search_matches_contact_display_name_or_number(client, session):
    """MAJOR 7: q matches by contact display name (via the ContactPhone-resolved
    contact) and independently by raw contact_e164."""
    owner_token = await register_and_login(client, "p16p@example.com")
    org = await create_org(client, owner_token, "P16 Org P")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    c_named = "+19725550801"
    c_other = "+19725550802"

    set_org_context(session, org_id)
    contact = Contact(
        id=uuid.uuid4(), org_id=org_id, display_name="Search Target", attributes={}
    )
    session.add(contact)
    await session.flush()
    session.add(
        ContactPhone(
            id=uuid.uuid4(),
            org_id=org_id,
            contact_id=contact.id,
            e164=c_named,
            label="mobile",
            is_primary=True,
        )
    )
    await session.commit()

    t1 = FROZEN_NOW - timedelta(minutes=5)
    th_named = await _make_thread(session, org_id, A, c_named, last_message_at=t1)
    await _make_message(session, org_id, th_named, direction="outbound", body="hi", created_at=t1)

    t2 = FROZEN_NOW - timedelta(minutes=4)
    th_other = await _make_thread(session, org_id, A, c_other, last_message_at=t2)
    await _make_message(session, org_id, th_other, direction="outbound", body="hi", created_at=t2)

    by_name = await client.get("/api/v1/conversations", params={"q": "search"}, headers=h)
    assert by_name.status_code == 200, by_name.text
    assert {item["contact_e164"] for item in by_name.json()["items"]} == {c_named}

    by_number = await client.get(
        "/api/v1/conversations", params={"q": "550802"}, headers=h
    )
    assert by_number.status_code == 200, by_number.text
    assert {item["contact_e164"] for item in by_number.json()["items"]} == {c_other}


async def test_untranscribed_voicemail_does_not_become_last_event(client, session):
    """MAJOR 7: a voicemail with no transcript yet never upgrades the pair's last event
    away from the underlying (missed) call."""
    owner_token = await register_and_login(client, "p16q@example.com")
    org = await create_org(client, owner_token, "P16 Org Q")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)
    c = "+19725550901"

    t = FROZEN_NOW - timedelta(minutes=5)
    call = await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c,
        direction="inbound",
        status="no_answer",
        created_at=t,
        ended_at=t,
    )
    await _make_voicemail(
        session, org_id, call.id, transcript=None, transcript_status="pending", created_at=t
    )

    r = await client.get("/api/v1/conversations", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["last_event_type"] == "call"
    assert items[0]["snippet"] == "Missed call"


async def test_non_failed_call_with_error_shaped_event_has_no_failure_detail(client, session):
    """MAJOR 7: an error-shaped VoiceEvent on a call that did NOT fail must never
    surface a failure_detail - the call.status guard is the gate, not event shape."""
    owner_token = await register_and_login(client, "p16r@example.com")
    org = await create_org(client, owner_token, "P16 Org R")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)
    c = "+19725551001"

    t = FROZEN_NOW - timedelta(minutes=5)
    call = await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c,
        direction="outbound",
        status="completed",
        created_at=t,
        ended_at=t,
        answered_at=t,
    )
    await _make_voice_event(
        session,
        org_id,
        call.id,
        event_type="error",
        payload={"error": "some non-fatal warning"},
        created_at=t,
    )

    url = f"/api/v1/conversations/{quote(c, safe='')}/timeline"
    r = await client.get(url, params={"our_e164": A}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "call"
    assert items[0]["failure_detail"] is None


async def test_outbound_failed_call_snippet(client, session):
    """MAJOR 7: an outbound failed call snippets as "Call failed"."""
    owner_token = await register_and_login(client, "p16s@example.com")
    org = await create_org(client, owner_token, "P16 Org S")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)
    c = "+19725551101"

    t = FROZEN_NOW - timedelta(minutes=5)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c,
        direction="outbound",
        status="failed",
        created_at=t,
    )

    r = await client.get("/api/v1/conversations", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["snippet"] == "Call failed"


async def test_unread_missed_call_newer_than_last_read_at_or_no_thread(client, session):
    """MINOR 9: an inbound no_answer/busy/canceled call newer than thread.last_read_at
    (or with no thread at all) counts as unread; a missed call that IS the latest event
    but happened before the thread was last read does not."""
    owner_token = await register_and_login(client, "p16t@example.com")
    org = await create_org(client, owner_token, "P16 Org T")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    c_no_thread = "+19725551201"
    c_newer_than_read = "+19725551202"
    c_older_than_read = "+19725551203"

    # No thread at all: a missed call always counts as unread - there is no read cursor.
    t_call1 = FROZEN_NOW - timedelta(minutes=5)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_no_thread,
        direction="inbound",
        status="no_answer",
        created_at=t_call1,
        ended_at=t_call1,
    )

    # Thread read BEFORE the missed call happened -> the call is newer than last_read_at.
    t_msg_a = FROZEN_NOW - timedelta(minutes=25)
    t_read_a = FROZEN_NOW - timedelta(minutes=20)
    thread_newer = await _make_thread(
        session, org_id, A, c_newer_than_read, last_message_at=t_msg_a, last_read_at=t_read_a
    )
    await _make_message(
        session, org_id, thread_newer, direction="outbound", body="hi", created_at=t_msg_a
    )
    t_call2 = FROZEN_NOW - timedelta(minutes=10)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_newer_than_read,
        direction="inbound",
        status="busy",
        created_at=t_call2,
        ended_at=t_call2,
    )

    # Thread read AFTER the missed call happened (but the call is still the latest
    # event overall) -> already read, must not count as unread.
    t_msg_b = FROZEN_NOW - timedelta(minutes=25)
    t_read_b = FROZEN_NOW - timedelta(minutes=2)
    thread_older = await _make_thread(
        session, org_id, A, c_older_than_read, last_message_at=t_msg_b, last_read_at=t_read_b
    )
    await _make_message(
        session, org_id, thread_older, direction="outbound", body="hi", created_at=t_msg_b
    )
    t_call3 = FROZEN_NOW - timedelta(minutes=10)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=c_older_than_read,
        direction="inbound",
        status="canceled",
        created_at=t_call3,
        ended_at=t_call3,
    )

    r = await client.get("/api/v1/conversations", headers=h)
    assert r.status_code == 200, r.text
    by_contact = {item["contact_e164"]: item for item in r.json()["items"]}
    # Sanity: the call really did become each pair's latest event.
    assert by_contact[c_newer_than_read]["last_event_type"] == "call"
    assert by_contact[c_older_than_read]["last_event_type"] == "call"

    r_unread = await client.get(
        "/api/v1/conversations", params={"filter": "unread"}, headers=h
    )
    assert r_unread.status_code == 200, r_unread.text
    contacts = {item["contact_e164"] for item in r_unread.json()["items"]}
    assert contacts == {c_no_thread, c_newer_than_read}


async def test_filter_unread_dead_end_repro_paginates_to_both_unread(client, session):
    """P16 bugfix repro (Opus review, confirmed empirically): 20 read threads newer
    than 2 unread ones, limit=5, filter=unread. Before the fix, the per-source
    candidate window (limit * 3) was drawn BEFORE filter_/q were applied in Python, so
    the newest window could be entirely read threads and the endpoint returned
    items=[] AND next_cursor=None even though the 2 unread threads existed deeper -
    a silent dead end. filter=unread is now pushed into the thread SQL itself, so the
    window is drawn from matching rows; paging must surface both unread conversations
    and terminate with next_cursor=None."""
    owner_token = await register_and_login(client, "p16v@example.com")
    org = await create_org(client, owner_token, "P16 Org V")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    # The 2 unread threads are the OLDEST of all - buried behind the 20 read ones.
    unread_contacts = ["+19725558001", "+19725558002"]
    for idx, contact in enumerate(unread_contacts):
        t = FROZEN_NOW - timedelta(minutes=100 + idx)
        thread = await _make_thread(
            session, org_id, A, contact, last_message_at=t, last_read_at=None
        )
        await _make_message(
            session, org_id, thread, direction="inbound", body="unread", created_at=t
        )

    for idx in range(20):
        contact = f"+1972555{6000 + idx}"
        t = FROZEN_NOW - timedelta(minutes=idx)
        thread = await _make_thread(
            session, org_id, A, contact, last_message_at=t, last_read_at=t
        )
        await _make_message(
            session, org_id, thread, direction="inbound", body="read", created_at=t
        )

    found: set[str] = set()
    cursor = None
    pages = 0
    while True:
        params: dict = {"filter": "unread", "limit": 5}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/conversations", params=params, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        found.update(item["contact_e164"] for item in body["items"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        if pages > 20:
            raise AssertionError("too many pages - unread paging not converging")

    assert found == set(unread_contacts)


async def test_q_search_call_pair_found_via_paging_beyond_window(client, session):
    """P16 bugfix: q is pushed into the thread-source SQL, but call-derived pairs stay
    Python-filtered per the plan (tab=calls, unresponded and call-based unread are the
    only call-side logic named for SQL push-down; q is not). So a call-only pair whose
    contact matches q can sit entirely outside the first `limit * 3` calls window when
    enough newer, non-matching calls exist. The safety-net continuation cursor (built
    from the oldest examined candidate when a source's window came back full) must let
    the client keep paging until the match is found, rather than stopping at
    next_cursor=None on the first, empty-of-matches page."""
    owner_token = await register_and_login(client, "p16w@example.com")
    org = await create_org(client, owner_token, "P16 Org W")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    limit = 3
    # More non-matching call pairs than one window (limit * 3 == 9) holds.
    noise_contacts = [f"+1972555{1000 + i}" for i in range(12)]
    for idx, contact in enumerate(noise_contacts):
        t = FROZEN_NOW - timedelta(minutes=idx)
        await _make_call(
            session,
            org_id,
            our_e164=A,
            contact_e164=contact,
            direction="inbound",
            status="completed",
            created_at=t,
            ended_at=t,
        )

    target_contact = "+19725559999"
    t_target = FROZEN_NOW - timedelta(minutes=len(noise_contacts) + 5)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=target_contact,
        direction="inbound",
        status="completed",
        created_at=t_target,
        ended_at=t_target,
    )

    found_contacts: set[str] = set()
    cursor = None
    pages = 0
    while True:
        params: dict = {"q": "9999", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/conversations", params=params, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        found_contacts.update(item["contact_e164"] for item in body["items"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        if pages > 20:
            raise AssertionError("too many pages - safety-net cursor is not converging")

    assert found_contacts == {target_contact}
    # Proves the safety net actually kicked in past an initial window with no matches.
    assert pages > 1


async def test_unread_thread_between_two_full_source_frontiers_is_found_via_paging(client, session):
    """P16 second-round bugfix (Opus review BLOCKER): the safety-net continuation
    cursor must be the MAX (shallowest) of the PER-SOURCE oldest-examined frontiers,
    computed only for sources whose window came back full - never the overall min
    across both sources. Repro: 15 decoy threads pass the loose SQL unread predicate
    (last_read_at is null) but fail the exact Python check (their last message is
    outbound) at minutes 1-15; a REAL unread thread sits at minute 20, just past the
    15-row thread window (limit=5 -> limit*3=15, so the window - full - stops at the
    decoys and never reaches minute 20). Separately, 16 unrelated (non-missed) calls
    sit at minutes 30-45; the calls window (also full at 15 rows) bottoms out at
    minute 44. Taking the OVERALL min across both sources would pick the calls
    frontier (minute 44, deeper than the thread frontier) and the next request's
    `thread_order_expr <= (now - 44min)` bound would then exclude the minute-20
    thread forever, since it is NEWER than that bound. Using the per-source MAX
    (minute 15, the thread's own frontier) keeps the bound permissive enough to reach
    it. The real unread thread must be found by paging, and paging must still
    terminate."""
    owner_token = await register_and_login(client, "p16x@example.com")
    org = await create_org(client, owner_token, "P16 Org X")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    await _make_number(session, org_id, A)

    # 15 decoys: pass the SQL predicate (last_read_at is null) but their last message
    # is OUTBOUND, so the exact Python `unread` check rejects them. Newest 15 minutes.
    for idx in range(1, 16):
        contact = f"+1972555{2000 + idx}"
        t = FROZEN_NOW - timedelta(minutes=idx)
        thread = await _make_thread(
            session, org_id, A, contact, last_message_at=t, last_read_at=None
        )
        await _make_message(
            session, org_id, thread, direction="outbound", body="decoy", created_at=t
        )

    # The real unread thread - older than all 15 decoys, just past the thread window.
    real_unread_contact = "+19725553020"
    t_real = FROZEN_NOW - timedelta(minutes=20)
    real_thread = await _make_thread(
        session, org_id, A, real_unread_contact, last_message_at=t_real, last_read_at=None
    )
    await _make_message(
        session, org_id, real_thread, direction="inbound", body="real unread", created_at=t_real
    )

    # 16 unrelated, non-missed calls (never match filter=unread) at minutes 30-45 -
    # deeper than the real unread thread - whose own window is also full at 15 rows.
    for idx in range(30, 46):
        contact = f"+1972555{4000 + idx}"
        t = FROZEN_NOW - timedelta(minutes=idx)
        await _make_call(
            session,
            org_id,
            our_e164=A,
            contact_e164=contact,
            direction="inbound",
            status="completed",
            created_at=t,
            ended_at=t,
        )

    found: set[str] = set()
    cursor = None
    pages = 0
    while True:
        params: dict = {"filter": "unread", "limit": 5}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/conversations", params=params, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        found.update(item["contact_e164"] for item in body["items"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        if pages > 20:
            raise AssertionError("too many pages - per-source frontier paging not converging")

    assert real_unread_contact in found
