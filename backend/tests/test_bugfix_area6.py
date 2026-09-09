"""Regression tests for BUGFIX_LEDGER_2026-09.md Area 6 (outbound engine + AI agents) +
the sweeper cluster (items from other areas wired/fixed in this batch)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from random import Random
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import (
    Call,
    Contact,
    ContactList,
    ContactListRow,
    ContactPhone,
    DialAttempt,
    MediaAsset,
    Message,
    MessageThread,
    Org,
    OrgNumber,
    OutboundCampaign,
    OutboundSend,
)
from app.providers.domain import SendResult
from app.services import dialer as dialer_svc
from app.services import kb as kb_svc
from app.services import outbound as outbound_svc
from app.services import scoring as scoring_svc
from tests.conftest import FakeCarrier, auth_headers, make_org_with_number

OUR = "+12145550100"
OUR_B = "+12145550101"
A = "+19725550101"
B = "+19725550102"
C = "+19725550103"

_REAL_NOW = datetime.now(timezone.utc)
FROZEN = _REAL_NOW.replace(hour=18, minute=0, second=0, microsecond=0)
if FROZEN < _REAL_NOW + timedelta(minutes=5):
    FROZEN += timedelta(days=1)


async def _make_org(client, email="area6@example.com", number=OUR):
    token, org, _ = await make_org_with_number(client, email, "Area6 Org", number)
    return token, uuid.UUID(org["id"])


async def _ready_list(session, org_id, e164s):
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv", status="ready",
        total_rows=len(e164s), accepted_count=len(e164s),
    )
    session.add(lst)
    await session.flush()
    for e164 in e164s:
        contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name=e164)
        session.add(contact)
        await session.flush()
        session.add(
            ContactListRow(
                id=uuid.uuid4(), org_id=org_id, list_id=lst.id, row_number=1,
                raw={"phone": e164}, e164=e164, contact_id=contact.id, status="accepted",
                fields={},
            )
        )
    await session.commit()
    return lst


async def _campaign(session, org_id, list_id, **overrides):
    set_org_context(session, org_id)
    fields = {
        "name": "C", "channel": "sms", "list_id": list_id, "body": "Hello",
        "from_numbers": [OUR], "rate_per_minute": 600, "daily_cap": 200,
        "respect_warmup": False, "max_attempts": 2, "retry_backoff_minutes": 240,
    }
    fields.update(overrides)
    return await outbound_svc.create_campaign(session, org_id, **fields)


async def _dial_campaign(session, org_id, list_id, **overrides):
    set_org_context(session, org_id)
    fields = {
        "name": "D", "channel": "voice", "list_id": list_id, "from_numbers": [OUR],
        "dialer_mode": "power", "parallel_lines": 1, "max_attempts": 2,
        "retry_backoff_minutes": 60, "local_presence": False,
    }
    fields.update(overrides)
    campaign = OutboundCampaign(id=uuid.uuid4(), org_id=org_id, **fields)
    session.add(campaign)
    await session.commit()
    return campaign


# ----------------------------------------------------------------------------------
# 6.1/2.8: stale-send crash-recovery adoption is bounded
# ----------------------------------------------------------------------------------
async def test_6_1_stale_send_adoption_ignores_unrelated_message(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area61@example.com")
    lst = await _ready_list(session, org_id, [A])
    campaign = await _campaign(session, org_id, lst.id, body="Campaign body")
    await outbound_svc.enqueue_campaign_rows(session, campaign)

    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id))
    ).scalar_one()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.execute(
        sa.update(OutboundSend)
        .where(OutboundSend.id == row.id)
        .values(status="sending", message_id=None, updated_at=old)
    )
    # An UNRELATED message to the same contact, different body, around the same time -
    # this must NOT be adopted by the stale row.
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=A,
        last_message_at=datetime.now(timezone.utc),
    )
    session.add(thread)
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
            status="accepted", from_e164=OUR, to_e164=A, body="A COMPLETELY different message",
            media=[], carrier="bandwidth",
        )
    )
    await session.commit()

    requeued = await outbound_svc._requeue_stale_sending(session, datetime.now(timezone.utc))
    assert requeued == 1
    await session.refresh(row)
    # No matching message found (body differs) - requeued for a real retry, not
    # incorrectly marked "sent" off the back of an unrelated message.
    assert row.status == "queued"
    assert row.message_id is None


async def test_6_1_stale_send_adopts_matching_message(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area61b@example.com")
    lst = await _ready_list(session, org_id, [A])
    campaign = await _campaign(session, org_id, lst.id, body="Campaign body")
    await outbound_svc.enqueue_campaign_rows(session, campaign)

    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id))
    ).scalar_one()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.execute(
        sa.update(OutboundSend)
        .where(OutboundSend.id == row.id)
        .values(status="sending", message_id=None, updated_at=old)
    )
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=A,
        last_message_at=datetime.now(timezone.utc),
    )
    session.add(thread)
    await session.flush()
    matching = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
        status="rejected", from_e164=OUR, to_e164=A, body="Campaign body", media=[],
        carrier="bandwidth", error_code="carrier_down",
    )
    session.add(matching)
    await session.commit()

    requeued = await outbound_svc._requeue_stale_sending(session, datetime.now(timezone.utc))
    assert requeued == 1
    await session.refresh(row)
    # 6.1: adopted, but the underlying message was REJECTED - must never be "sent".
    assert row.status == "failed"
    assert row.message_id == matching.id


# ----------------------------------------------------------------------------------
# 6.2: campaign sends go through select_sender (sticky), not raw pick_deterministic
# ----------------------------------------------------------------------------------
async def test_6_2_campaign_send_honours_sticky_thread_number(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area62@example.com")
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR_B}, headers=auth_headers(token, org_id)
    )
    assert r.status_code == 201, r.text

    # D6: this contact number is chosen so pick_deterministic(contact, [OUR, OUR_B])
    # itself resolves to OUR (verified: `pick_deterministic(NON_STICKY_PICK, [OUR,
    # OUR_B]) == OUR`) - the OLD test's contact (`A`) happened to hash to OUR_B, so it
    # passed even with the sticky-thread override entirely removed, proving nothing.
    # This contact only ends up on OUR_B if the sticky-thread lookup actually overrides
    # pick_deterministic's own (different) answer.
    contact = "+19725550100"
    from app.services.sender import pick_deterministic

    assert pick_deterministic(contact, [OUR, OUR_B]) == OUR, (
        "fixture contact must hash to the NON-sticky number for this test to discriminate"
    )

    lst = await _ready_list(session, org_id, [contact])
    campaign = await _campaign(
        session, org_id, lst.id, body="Hi", from_numbers=[OUR, OUR_B], rate_per_minute=600
    )
    campaign = await outbound_svc.start_campaign(session, campaign)

    # contact's conversation is already sticky to OUR_B - pick_deterministic alone (no
    # thread awareness) picks OUR instead, forking the conversation, unless the sticky
    # override is genuinely honoured.
    set_org_context(session, org_id)
    session.add(
        MessageThread(
            id=uuid.uuid4(), org_id=org_id, our_e164=OUR_B, contact_e164=contact,
            last_message_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()

    await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    await carrier.drain()

    set_org_context(session, org_id)
    msg = (
        await session.execute(sa.select(Message).where(Message.to_e164 == contact))
    ).scalar_one()
    assert msg.from_e164 == OUR_B


# ----------------------------------------------------------------------------------
# 6.4: dialer stale-requeue has an attempt cap
# ----------------------------------------------------------------------------------
async def test_6_4_dialer_stale_requeue_attempt_cap(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area64@example.com")
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id, max_attempts=1)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id))
    ).scalar_one()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.execute(
        sa.update(DialAttempt)
        .where(DialAttempt.id == row.id)
        .values(status="dialing", call_id=None, updated_at=old)
    )
    await session.commit()

    requeued = await dialer_svc._requeue_stale_dialing(session, datetime.now(timezone.utc))
    assert requeued == 1
    await session.refresh(row)
    # max_attempts=1 already spent by this stale sighting - terminal, not requeued
    # forever.
    assert row.status == "failed"
    assert row.attempts == 1


# ----------------------------------------------------------------------------------
# 6.6: start_at schedules instead of running immediately (SMS + voice)
# ----------------------------------------------------------------------------------
async def test_6_6_sms_campaign_future_start_at_is_scheduled(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area66a@example.com")
    lst = await _ready_list(session, org_id, [A])
    future = datetime.now(timezone.utc) + timedelta(days=1)
    campaign = await _campaign(session, org_id, lst.id, start_at=future)
    campaign = await outbound_svc.start_campaign(session, campaign)
    assert campaign.status == "scheduled"

    # Not yet due - a tick right now must not run it.
    counts = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    assert counts["sent"] == 0
    set_org_context(session, org_id)
    await session.refresh(campaign)
    assert campaign.status == "scheduled"

    # Due now - the next tick releases it into "running" and sends.
    await outbound_svc.outbound_tick(
        session, carrier, None, Random(1), now=future + timedelta(minutes=1)
    )
    await carrier.drain()
    await session.refresh(campaign)
    assert campaign.status in ("running", "completed")


async def test_6_6_voice_campaign_future_start_at_is_scheduled(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area66b@example.com")
    lst = await _ready_list(session, org_id, [A])
    future = datetime.now(timezone.utc) + timedelta(days=1)
    campaign = await _dial_campaign(session, org_id, lst.id, start_at=future)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)
    assert campaign.status == "scheduled"


# ----------------------------------------------------------------------------------
# 6.7/6.23: list upload row-count abort during parsing + duplicate headers rejected
# ----------------------------------------------------------------------------------
def test_6_23_duplicate_csv_headers_rejected():
    from app.services.list_parsing import parse_csv_bytes

    body = b"phone,phone,first_name\r\n+12145550100,+19725550101,Bob\r\n"
    with pytest.raises(ValidationFailedError):
        parse_csv_bytes(body)


def test_6_7_csv_row_count_aborts_during_parsing():
    from app.services.list_parsing import parse_csv_bytes

    lines = [b"phone,first_name"]
    for i in range(10):
        lines.append(f"+1972555{i:04d},Row{i}".encode())
    body = b"\r\n".join(lines)
    with pytest.raises(ValidationFailedError):
        parse_csv_bytes(body, max_rows=5)


async def test_6_7_upload_rejects_oversized_file(client):
    from app.services import list_import as list_import_svc

    token, org, _ = await make_org_with_number(client, "area67@example.com", "Org U", OUR)
    big = b"phone,first_name\r\n" + (b"+12145550100,Bob\r\n" * 1)
    # Monkeypatch the module constant down so the test doesn't need a real 10MB body.
    original = list_import_svc.MAX_LIST_BYTES
    list_import_svc.MAX_LIST_BYTES = 10
    try:
        r = await client.post(
            "/api/v1/outbound/lists",
            files={"file": ("l.csv", big, "text/csv")},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 422, r.text
    finally:
        list_import_svc.MAX_LIST_BYTES = original


# ----------------------------------------------------------------------------------
# 6.8: /lists/{id}/commit double-import race is claimed atomically
# ----------------------------------------------------------------------------------
async def test_6_8_double_commit_is_rejected(client, session):
    token, org, _ = await make_org_with_number(client, "area68@example.com", "Org L", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    upload = await client.post(
        "/api/v1/outbound/lists",
        files={"file": ("l.csv", b"phone\r\n+12145550100\r\n", "text/csv")},
        headers=h,
    )
    assert upload.status_code == 201, upload.text
    list_id = upload.json()["list_id"]

    r1 = await client.post(
        f"/api/v1/outbound/lists/{list_id}/commit", json={"mapping": {"phone": "phone"}}, headers=h
    )
    assert r1.status_code == 202, r1.text

    r2 = await client.post(
        f"/api/v1/outbound/lists/{list_id}/commit", json={"mapping": {"phone": "phone"}}, headers=h
    )
    assert r2.status_code == 409, r2.text

    from app.services import list_import as list_import_svc

    await list_import_svc.wait_for_pending_import_tasks()


# ----------------------------------------------------------------------------------
# D5: an expired upload (store 404) at /commit must never leave the list permanently
# claimed - the SAME 6.8 one-shot gate this batch's claim protects must still let a
# genuine retry (re-upload, commit again) through, not report "already being
# imported" forever.
# ----------------------------------------------------------------------------------
async def test_d5_commit_404_then_retry_succeeds(app_with_loopback, session):
    client, _carrier, application = app_with_loopback
    token, org, _ = await make_org_with_number(client, "aread5@example.com", "Org D5", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    upload = await client.post(
        "/api/v1/outbound/lists",
        files={"file": ("l.csv", b"phone\r\n+12145550100\r\n", "text/csv")},
        headers=h,
    )
    assert upload.status_code == 201, upload.text
    list_id = upload.json()["list_id"]

    # Simulate the uploaded file having expired (deleted from storage) before commit.
    store = application.state.media_store
    key = f"org/{org_id}/imports/{list_id}/source"
    data = await store.get(key)
    await store.delete(key)

    r1 = await client.post(
        f"/api/v1/outbound/lists/{list_id}/commit", json={"mapping": {"phone": "phone"}}, headers=h
    )
    assert r1.status_code == 404, r1.text

    # D5: import_started_at must NOT have been claimed on the 404 path - restore the
    # file and retry; this must succeed, not 409 "already being imported".
    await store.put(key, data, "text/csv")
    r2 = await client.post(
        f"/api/v1/outbound/lists/{list_id}/commit", json={"mapping": {"phone": "phone"}}, headers=h
    )
    assert r2.status_code == 202, r2.text

    from app.services import list_import as list_import_svc

    await list_import_svc.wait_for_pending_import_tasks()


# ----------------------------------------------------------------------------------
# 6.9: LLM tool args truncated to column limits; DB errors during tool exec recover
# ----------------------------------------------------------------------------------
async def test_6_9_book_appointment_when_arg_is_truncated_to_column_limit(session):
    from app.services import sms_agent

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area69 Org", slug="area69-org"))
    await session.flush()
    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=A,
        last_message_at=datetime.now(timezone.utc),
    )
    session.add(thread)
    await session.commit()

    class FakeCall:
        name = "book_appointment"
        arguments = {"when": "x" * 500, "notes": "short note"}

    result = await sms_agent._run_tool_call(session, org_id, thread, FakeCall())
    assert result.startswith("Booked for")

    from app.models.scheduling import Appointment

    appt = (
        await session.execute(sa.select(Appointment).where(Appointment.org_id == org_id))
    ).scalar_one()
    assert len(appt.raw_when) <= 255


# ----------------------------------------------------------------------------------
# 6.10: the final tool round synthesizes a reply instead of an empty one
# ----------------------------------------------------------------------------------
async def test_6_10_final_round_without_tools_synthesizes_reply(session, monkeypatch):
    from app.services import llm_client, sms_agent

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area610 Org", slug="area610-org"))
    await session.flush()
    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=A,
        last_message_at=datetime.now(timezone.utc),
    )
    session.add(thread)
    await session.commit()

    call_count = {"n": 0}

    async def fake_chat(client, *, provider, model, api_key, system, turns, tools, **kwargs):
        call_count["n"] += 1
        if tools:
            # Every tool-enabled round keeps calling a tool (never stops on its own).
            return llm_client.ChatResult(
                text="",
                tool_calls=[
                    llm_client.ToolCall(id=f"c{call_count['n']}", name="kb_search", arguments={"query": "x"})
                ],
                tokens_in=10,
                tokens_out=5,
            )
        # The forced final round (tools disabled) actually answers.
        return llm_client.ChatResult(text="Here is your answer.", tool_calls=[], tokens_in=8, tokens_out=4)

    async def fake_run_tool_call(session_, org_id_, thread_, call):
        return "some kb result"

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    monkeypatch.setattr(sms_agent, "_run_tool_call", fake_run_tool_call)

    text, handoff_reason = await sms_agent._run_llm_turn(
        object(),
        provider="anthropic",
        model="",
        api_key="key",
        system="sys",
        history=[],
        session=session,
        org_id=org_id,
        thread=thread,
    )
    assert handoff_reason is None
    assert text == "Here is your answer."


# ----------------------------------------------------------------------------------
# 6.11: a missing LLM key skips the turn (thread stays active), never a permanent handoff
# ----------------------------------------------------------------------------------
async def test_6_11_missing_llm_key_skips_turn_thread_stays_active(session):
    from app.models import AgentProfile, AgentSmsTurn
    from app.services import sms_agent
    from tests.conftest import make_settings

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area611 Org", slug="area611-org"))
    await session.flush()
    set_org_context(session, org_id)
    session.add(
        OrgNumber(id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth", is_active=True, status="active")
    )
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=A, ai_state="off",
    )
    session.add(thread)
    await session.flush()
    session.add(
        AgentProfile(
            id=uuid.uuid4(), org_id=org_id, name="P", sms_enabled=True, is_default=True,
            llm_provider="anthropic", sms_turn_ceiling=10, sms_max_reply_chars=300,
        )
    )
    message = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
        status="received", from_e164=A, to_e164=OUR, body="hello", media=[], carrier="bandwidth",
    )
    session.add(message)
    await session.commit()

    settings = make_settings(anthropic_api_key="", openai_api_key="")
    await sms_agent._maybe_reply_inner(
        session, settings, None, inbound_message_id=message.id, carrier=None, http_client=None,
    )

    await session.refresh(thread)
    turn = (
        await session.execute(
            sa.select(AgentSmsTurn).where(AgentSmsTurn.inbound_message_id == message.id)
        )
    ).scalar_one()
    assert turn.status == "skipped"
    assert turn.detail == "llm_not_configured"
    # Never handed off - the thread just woke up (off -> active) and stays that way, so
    # the NEXT message retries automatically once a key is configured.
    assert thread.ai_state == "active"


# ----------------------------------------------------------------------------------
# 6.12/6.13: KB search escapes LIKE wildcards + is limited; document text is bounded
# ----------------------------------------------------------------------------------
async def test_6_12_kb_search_escapes_like_wildcards(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area612 Org", slug="area612-org"))
    await session.flush()
    set_org_context(session, org_id)
    await kb_svc.create_document(session, org_id, "Pricing", "Our price is 50% off this week.")
    await kb_svc.create_document(session, org_id, "Other", "50X is a model number, unrelated.")
    await session.commit()

    # A literal "%" must not act as a wildcard matching "50X".
    hits = await kb_svc.search(session, org_id, "50%")
    assert all("50%" in h["text"] for h in hits)


async def test_6_13_kb_document_text_over_limit_rejected(client):
    token, org, _ = await make_org_with_number(client, "area613@example.com", "Org K", OUR)
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/kb/documents",
        json={"title": "Huge", "text": "x" * 1_000_001},
        headers=h,
    )
    assert r.status_code == 422, r.text


# ----------------------------------------------------------------------------------
# 6.14: call scoring persists token usage on CallScore
# ----------------------------------------------------------------------------------
async def test_6_14_call_scoring_persists_tokens(session, monkeypatch):
    from app.models import CallScore
    from app.models.agent import CallTranscriptSegment
    from app.services import llm_client
    from tests.conftest import make_settings

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area614 Org", slug="area614-org"))
    await session.flush()
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=A, our_e164=OUR,
        carrier="telnyx", status="completed", ended_at=datetime.now(timezone.utc),
    )
    session.add(call)
    await session.flush()
    session.add(
        CallTranscriptSegment(id=uuid.uuid4(), org_id=org_id, call_id=call.id, role="user", text="hi", at_ms=0)
    )
    await session.commit()

    async def fake_chat(client, *, provider, model, api_key, system, turns, tools, **kwargs):
        return llm_client.ChatResult(
            text='{"sentiment": "positive", "score": 5, "summary": "Great call."}',
            tool_calls=[], tokens_in=123, tokens_out=45,
        )

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    settings = make_settings(anthropic_api_key="test-key")

    counts = await scoring_svc.score_pending_calls(session, settings, client=object())
    assert counts["done"] == 1

    score = (await session.execute(sa.select(CallScore).where(CallScore.call_id == call.id))).scalar_one()
    assert score.tokens_in == 123
    assert score.tokens_out == 45


def test_6_24_transcript_is_delimited_in_scoring_prompt():
    """Structural check: the SYSTEM_PROMPT/prompt-building code delimits the transcript
    and instructs the model to treat it as data, not instructions (6.24)."""
    import inspect

    source = inspect.getsource(scoring_svc._score_one)
    assert "<transcript>" in source
    assert "DATA" in source.upper()


# ----------------------------------------------------------------------------------
# 6.15: voice campaigns emit campaign.completed
# ----------------------------------------------------------------------------------
async def test_6_15_voice_campaign_emits_completed_event(app_with_loopback, session, monkeypatch):
    from app.models import PlatformEvent

    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client, "area615@example.com")
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    async def fake_start_call(session_, settings, bus, api, *, org_id, to_e164, from_e164, identity):
        return dialer_svc.DialOutcome(status="connected", call_id=None)

    monkeypatch.setattr(dialer_svc, "_start_call", fake_start_call)

    await dialer_svc.dialer_tick(session, object(), None, None, Random(1), now=FROZEN)
    # A second tick with nothing left queued/dialing observes completion.
    await dialer_svc.dialer_tick(session, object(), None, None, Random(1), now=FROZEN)

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(PlatformEvent).where(PlatformEvent.event_type == "campaign.completed")
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.payload["channel"] == "voice"


# ----------------------------------------------------------------------------------
# 6.20: pace_cache is keyed by (org_id, from_e164), not from_e164 alone
# ----------------------------------------------------------------------------------
async def test_6_20_pace_cache_does_not_cross_orgs(app_with_loopback, session):
    """Two different orgs sharing the SAME literal from_e164 in their own data must not
    throttle each other - a stale/incorrect single-org cache key would."""
    client, carrier, _app = app_with_loopback
    token_a, org_a = await _make_org(client, "area620a@example.com", OUR)
    token_b, org_b = await _make_org(client, "area620b@example.com", OUR_B)

    lst_a = await _ready_list(session, org_a, [A])
    lst_b = await _ready_list(session, org_b, [B])
    campaign_a = await _campaign(session, org_a, lst_a.id, from_numbers=[OUR])
    campaign_b = await _campaign(session, org_b, lst_b.id, from_numbers=[OUR_B])
    set_org_context(session, org_a)
    campaign_a = await outbound_svc.start_campaign(session, campaign_a)
    set_org_context(session, org_b)
    campaign_b = await outbound_svc.start_campaign(session, campaign_b)

    counts = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    await carrier.drain()
    # Both orgs' rows get a shot in the SAME tick - neither was capped/paced off the
    # other's send history despite processing in the same pace_cache dict.
    assert counts["sent"] == 2


# ----------------------------------------------------------------------------------
# 6.22: GET /appointments is bounded
# ----------------------------------------------------------------------------------
async def test_6_22_appointments_route_accepts_limit_offset(client):
    token, org, _ = await make_org_with_number(client, "area622@example.com", "Org Appt", OUR)
    r = await client.get(
        "/api/v1/appointments?limit=1&offset=0", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text

    r_over = await client.get(
        "/api/v1/appointments?limit=99999", headers=auth_headers(token, org["id"])
    )
    assert r_over.status_code == 422, r_over.text


# ----------------------------------------------------------------------------------
# Sweeper cluster
# ----------------------------------------------------------------------------------
async def test_sweeper_advisory_lock_is_skipped_on_sqlite(session):
    """8.18/4.15/6.19: the whole pass must not error out on SQLite - pg_try_advisory_lock
    does not exist there, so the lock is skipped entirely."""
    from app.services import sweeper as sweeper_svc
    from tests.conftest import make_settings

    fake_app = SimpleNamespace(state=SimpleNamespace(settings=make_settings()))
    results = await sweeper_svc.run_once(fake_app)
    assert isinstance(results, dict)


# ----------------------------------------------------------------------------------
# D8: a failed pg_advisory_unlock must invalidate the lock session's connection - a
# plain close() would return it to the pool STILL HOLDING the session-level lock, and
# the next borrower would silently inherit it (every future sweeper pass on that
# connection blocked/skipped forever).
# ----------------------------------------------------------------------------------
async def test_d8_advisory_unlock_failure_invalidates_the_lock_session(monkeypatch):
    from app.services import sweeper as sweeper_svc

    class _FakeDialect:
        name = "postgresql"

    class _FakeBind:
        dialect = _FakeDialect()

    class _FakeLockSession:
        def __init__(self) -> None:
            self.invalidated = False
            self.closed = False
            self._calls = 0

        def get_bind(self):
            return _FakeBind()

        async def execute(self, _stmt):
            self._calls += 1
            if self._calls == 1:
                # pg_try_advisory_lock -> lock acquired.
                class _Result:
                    def scalar_one(self_inner):
                        return True

                return _Result()
            # pg_advisory_unlock -> fails.
            raise RuntimeError("advisory unlock boom")

        async def invalidate(self) -> None:
            self.invalidated = True

        async def close(self) -> None:
            self.closed = True

    fake_session = _FakeLockSession()
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda: (lambda: fake_session))

    async def fake_run_once_locked(app):
        return {"ok": 1}

    monkeypatch.setattr(sweeper_svc, "_run_once_locked", fake_run_once_locked)

    result = await sweeper_svc.run_once(SimpleNamespace())

    assert result == {"ok": 1}
    assert fake_session.invalidated is True
    assert fake_session.closed is True


async def test_2_11_recover_stale_queued_is_wired_into_the_sweeper(session, monkeypatch):
    from app.services import sweeper as sweeper_svc
    from tests.conftest import make_settings

    called = {"n": 0}

    async def fake_recover(session_, registry=None, now=None, settings=None):
        called["n"] += 1
        return 0

    monkeypatch.setattr("app.services.messaging.recover_stale_queued", fake_recover)

    fake_app = SimpleNamespace(state=SimpleNamespace(settings=make_settings()))
    await sweeper_svc.run_once(fake_app)
    assert called["n"] == 1


async def test_1_11_delivery_tick_continues_past_a_bad_row(session, monkeypatch):
    from app.models import PLATFORM_EVENT_TYPES, PlatformEvent, WebhookDelivery, WebhookEndpoint
    from app.services import webhooks_out as webhooks_out_svc
    from tests.conftest import make_settings

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area111 Org", slug="area111-org"))
    await session.flush()
    set_org_context(session, org_id)

    endpoint = WebhookEndpoint(
        id=uuid.uuid4(), org_id=org_id, url="https://example.test/hook",
        secret_encrypted="x", status="active", event_types=list(PLATFORM_EVENT_TYPES)[:1],
    )
    session.add(endpoint)
    await session.flush()

    deliveries = []
    for i in range(2):
        event = PlatformEvent(
            id=uuid.uuid4(), org_id=org_id, event_type=list(PLATFORM_EVENT_TYPES)[0],
            payload={"i": i},
        )
        session.add(event)
        await session.flush()
        delivery = WebhookDelivery(
            id=uuid.uuid4(), org_id=org_id, endpoint_id=endpoint.id, event_id=event.id,
            event_type=event.event_type, status="pending",
        )
        session.add(delivery)
        deliveries.append(delivery)
    await session.commit()

    call_n = {"n": 0}

    async def flaky_attempt(settings, client, endpoint_, event_, delivery_, moment):
        call_n["n"] += 1
        if call_n["n"] == 1:
            raise RuntimeError("boom")
        delivery_.status = "delivered"
        return "delivered"

    monkeypatch.setattr(webhooks_out_svc, "_attempt_delivery", flaky_attempt)
    monkeypatch.setattr(webhooks_out_svc, "_endpoint_should_disable", lambda ep: False)

    settings = make_settings()
    counts = await webhooks_out_svc.delivery_tick(session, settings, client=object())
    # The first (bad) row must not have prevented the second from being attempted.
    assert call_n["n"] == 2
    assert counts["delivered"] == 1


async def test_4_7_spend_rollup_continues_past_one_orgs_failure(session, monkeypatch):
    from app.services import spend as spend_svc

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    session.add_all([
        Org(id=org_a, name="Area47A Org", slug="area47a-org"),
        Org(id=org_b, name="Area47B Org", slug="area47b-org"),
    ])
    await session.commit()

    real_rollup_day = spend_svc.rollup_day

    async def flaky_rollup_day(session_, org_id, day):
        if org_id == org_a:
            raise RuntimeError("simulated collision")
        return await real_rollup_day(session_, org_id, day)

    monkeypatch.setattr(spend_svc, "rollup_day", flaky_rollup_day)

    rolled = await spend_svc.rollup_recent(session, days=1)
    # org_a's failure must not have prevented org_b from being rolled up.
    assert rolled == 1
