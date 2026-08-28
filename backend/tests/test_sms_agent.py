"""P10: the AI SMS agent turn engine.

Every scripted "LLM call" here goes through an httpx.MockTransport - there is never a
live API call in this file. Almost every test drives the REAL webhook route: since the
post-commit trigger (messaging._ingest_inbound -> sms_agent.spawn_from_ingest) now reads
the app's REAL settings/event_bus off session.info (populated by webhooks.py), the
auto-triggered background task is the actual production path, not a stand-in - so
exercising it directly is both simpler and more representative than calling
``maybe_reply`` by hand. The one exception is the claim-insert idempotency test, which
needs two competing invocations of ``maybe_reply`` for the exact same message.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from app.compliance import service as compliance_svc
from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.models import AgentSmsTurn, Appointment, Message, MessageThread
from app.services import messaging as messaging_svc
from app.services import sms_agent
from tests.conftest import (
    auth_headers,
    fixture_bytes,
    make_org_with_number,
    make_settings,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
CONTACT = "+19725550199"


# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _drain_sms_tasks():
    """Mirrors test_voice_plane.py's _no_leaked_dial_tasks: a background turn that
    outlives its test could touch rows underneath the next one."""
    yield
    await sms_agent.wait_for_pending_sms_tasks()


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, dict]] = []

    def publish(self, org_id: uuid.UUID, event: dict) -> None:
        self.events.append((org_id, event))


def _settings(**overrides):
    overrides.setdefault("anthropic_api_key", "test-anthropic-key")
    return make_settings(**overrides)


def _enable_llm_key(application, *, anthropic: str = "test-anthropic-key") -> None:
    """The REAL app.state.settings (built by app_with_carrier's webhook_settings) has no
    LLM key configured. The auto-trigger now always uses this exact settings object (via
    session.info, populated by webhooks.py), so any test expecting the LLM call to
    actually proceed past llm_client.chat()'s api_key check must set one here first."""
    application.state.settings.anthropic_api_key = SecretStr(anthropic)


def _script_next_turn(monkeypatch, bodies: list) -> None:
    """Point the NEXT auto-triggered turn that reaches the LLM at a fresh scripted
    MockTransport. Call again before each subsequent inbound that needs its own script -
    _default_http_client() is invoked once per turn, so each call gets a fresh queue."""
    monkeypatch.setattr(sms_agent, "_default_http_client", lambda: _anthropic_client(bodies))


def _anthropic_client(bodies: list) -> httpx.AsyncClient:
    """A MockTransport that pops one scripted Anthropic-shaped response per request.
    An int in the list is treated as an HTTP error status (simulates a 5xx)."""
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            return httpx.Response(500, json={"error": {"message": "no scripted response left"}})
        item = queue.pop(0)
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=item)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _text_reply(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]}


async def _inbound(client, body: str, msg_id: str) -> None:
    payload = json.loads(fixture_bytes("message-received.json"))
    payload[0]["message"]["id"] = msg_id
    payload[0]["message"]["text"] = body
    r = await client.post(
        HOOK, content=json.dumps(payload).encode(), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text


async def _inbound_and_drain(client, body: str, msg_id: str) -> None:
    await _inbound(client, body, msg_id)
    await sms_agent.wait_for_pending_sms_tasks()


async def _make_sms_org(client, email: str, org_name: str, **profile_overrides):
    """register -> org -> number -> an sms_enabled default agent profile."""
    token, org, _ = await make_org_with_number(client, email, org_name, OUR)
    h = auth_headers(token, org["id"])
    body = {"name": "Main", "sms_enabled": True, **profile_overrides}
    created = await client.post("/api/v1/agent/profiles", json=body, headers=h)
    assert created.status_code == 201, created.text
    profile = created.json()
    made_default = await client.post(
        f"/api/v1/agent/profiles/{profile['id']}/default", headers=h
    )
    assert made_default.status_code == 200, made_default.text
    return token, org, h, profile


async def _thread(session, org_id: uuid.UUID) -> MessageThread:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR, MessageThread.contact_e164 == CONTACT
            )
        )
    ).scalar_one()


async def _turns(session, org_id: uuid.UUID, thread_id: uuid.UUID) -> list[AgentSmsTurn]:
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(AgentSmsTurn)
            .where(AgentSmsTurn.thread_id == thread_id)
            .order_by(AgentSmsTurn.created_at)
        )
    ).scalars().all()
    return list(rows)


async def _latest_inbound_id(session, org_id: uuid.UUID, thread_id: uuid.UUID) -> uuid.UUID:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(Message.id)
            .where(Message.thread_id == thread_id, Message.direction == "inbound")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one()


# --------------------------------------------------------------------------------------
# sms_enabled=false (default) -> nothing happens anywhere. messaging._ingest_inbound's
# same-session org_could_reply() probe means NO background task is even spawned for
# these orgs at all (no turn row, "skipped" or otherwise) - the overwhelming majority of
# inbound messages system-wide never touch the SMS agent, so this also matters for
# every OTHER test file in the suite that drives an inbound webhook: it is what keeps a
# second, genuinely concurrent database session from ever being opened for them.
# --------------------------------------------------------------------------------------
async def test_sms_disabled_profile_does_nothing(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "sms1@example.com", "Org 1", OUR)
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/agent/profiles", json={"name": "Main"}, headers=h  # sms_enabled defaults False
    )
    assert created.status_code == 201, created.text
    await client.post(f"/api/v1/agent/profiles/{created.json()['id']}/default", headers=h)

    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "Hi there", "sms1-1")

    org_id = uuid.UUID(org["id"])
    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns == []
    assert thread.ai_state == "off"
    assert thread.ai_armed_at is None
    assert len(fake.sent) == before_sent


async def test_no_profile_at_all_is_skipped(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "sms2@example.com", "Org 2", OUR)

    await _inbound_and_drain(client, "Hi there", "sms2-1")

    org_id = uuid.UUID(org["id"])
    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns == []


# --------------------------------------------------------------------------------------
# Happy path: fresh thread arms itself, one reply, exactly one outbound, through the gate.
# --------------------------------------------------------------------------------------
async def test_first_inbound_arms_thread_and_replies_through_the_gate(
    app_with_carrier, session, monkeypatch
):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms3@example.com", "Org 3")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)
    _script_next_turn(monkeypatch, [_text_reply("Yes! Still available.")])

    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "Hi, are you still selling?", "sms3-1")

    thread = await _thread(session, org_id)
    assert thread.ai_state == "active"
    assert len(fake.sent) == before_sent + 1
    assert fake.sent[-1].text == "Yes! Still available."
    assert fake.sent[-1].to == CONTACT
    assert fake.sent[-1].from_ == OUR

    turns = await _turns(session, org_id, thread.id)
    assert len(turns) == 1
    assert turns[0].status == "replied"
    assert turns[0].outbound_message_id is not None


# --------------------------------------------------------------------------------------
# Idempotency.
# --------------------------------------------------------------------------------------
async def test_webhook_redelivery_never_produces_a_second_reply(
    app_with_carrier, session, monkeypatch
):
    """A replayed webhook (identical provider message id) is caught by
    messaging._ingest_inbound's OWN dedupe before the trigger is even spawned a second
    time - so this also proves the trigger only ever fires once per genuinely new message."""
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms4@example.com", "Org 4")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)
    _script_next_turn(monkeypatch, [_text_reply("Hello!")])

    await _inbound_and_drain(client, "First message", "sms4-dup")
    sent_after_first = len(fake.sent)

    await _inbound_and_drain(client, "First message", "sms4-dup")  # identical webhook

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert len(turns) == 1
    assert len(fake.sent) == sent_after_first


async def test_claim_insert_integrity_error_loser_path_is_a_clean_noop(
    app_with_carrier, session, monkeypatch
):
    """SHOULD-FIX 7: the claim INSERT is the durable idempotency token. Drive one real
    turn to completion through the auto-trigger, then race a SECOND, direct invocation of
    maybe_reply for the exact same inbound_message_id - it must lose the unique
    constraint and do nothing at all, not even attempt the (differently-scripted) LLM
    call that would prove it ran."""
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms-idem@example.com", "Org Idem")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)
    _script_next_turn(monkeypatch, [_text_reply("Hello!")])

    await _inbound_and_drain(client, "First message", "sms-idem-1")
    thread = await _thread(session, org_id)
    msg_id = await _latest_inbound_id(session, org_id, thread.id)
    sent_before = len(fake.sent)
    turns_before = len(await _turns(session, org_id, thread.id))

    loser_client = _anthropic_client([_text_reply("should never be reachable")])
    try:
        await sms_agent.maybe_reply(
            get_sessionmaker(),
            _settings(),
            FakeBus(),
            inbound_message_id=msg_id,
            carrier=fake,
            http_client=loser_client,
        )
    finally:
        await loser_client.aclose()

    assert len(fake.sent) == sent_before
    assert len(await _turns(session, org_id, thread.id)) == turns_before


# --------------------------------------------------------------------------------------
# Compliance: STOP/HELP/START are the keyword engine's job, never the AI's - and must
# never arm (or otherwise touch) the thread's ai_state.
# --------------------------------------------------------------------------------------
async def test_stop_keyword_is_never_answered_by_the_agent(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms6@example.com", "Org 6")
    org_id = uuid.UUID(org["id"])

    # No scripted LLM responses at all, and no key: if the agent tried to call the LLM
    # this would fail loudly rather than silently succeeding.
    await _inbound_and_drain(client, "STOP", "sms6-1")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert len(turns) == 1
    assert turns[0].status == "skipped"
    assert turns[0].detail.startswith("compliance_keyword")
    # Only the STOP confirmation went out - never a second, AI-authored reply.
    assert len(fake.sent) == 1
    assert "unsubscrib" in fake.sent[0].text.lower()


async def test_fresh_thread_receiving_stop_never_arms(app_with_carrier, session):
    """SHOULD-FIX 5: the keyword check runs BEFORE the off->active transition - a
    contact's very first message being STOP must never arm the thread."""
    client, fake, _ = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms-arm-stop@example.com", "Org ArmStop")
    org_id = uuid.UUID(org["id"])

    await _inbound_and_drain(client, "STOP", "sms-arm-stop-1")

    thread = await _thread(session, org_id)
    assert thread.ai_state == "off"
    assert thread.ai_armed_at is None


async def test_opted_out_contact_is_blocked_with_no_llm_call(app_with_carrier, session):
    """SHOULD-FIX 6: opted-out is recorded as `blocked` (one status for one real-world
    condition), not `skipped` - but it is still a short-circuit with no LLM call."""
    client, fake, _ = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms7@example.com", "Org 7")
    org_id = uuid.UUID(org["id"])

    await _inbound_and_drain(client, "STOP", "sms7-1")  # opts CONTACT out
    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "Actually wait, one more question", "sms7-2")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    last_turn = turns[-1]
    assert last_turn.status == "blocked"
    assert last_turn.detail == "opted_out"
    assert len(fake.sent) == before_sent  # no AI reply landed


async def test_gate_blocks_an_ai_reply_and_records_blocked_with_no_retry(
    app_with_carrier, session, monkeypatch
):
    """A DNC entry (not caught by the early opted-out skip) proves the AI send path runs
    through the SAME compliance gate as any human send - no exemption, ever (DR-4)."""
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms8@example.com", "Org 8")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    set_org_context(session, org_id)
    await compliance_svc.add_dnc(session, org_id, CONTACT)
    await session.commit()

    _script_next_turn(monkeypatch, [_text_reply("Hi! How can I help?")])
    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "Hello?", "sms8-1")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "blocked"
    assert len(fake.sent) == before_sent  # the gate stopped it before the carrier was touched
    assert thread.ai_state == "active"  # DR-4: a blocked send does not retry, stays active


# --------------------------------------------------------------------------------------
# Handoff: keyword, tool call, turn ceiling, LLM error.
# --------------------------------------------------------------------------------------
async def test_handoff_keyword_hands_off_with_no_ai_reply_and_publishes(
    app_with_carrier, session
):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms9@example.com", "Org 9")
    org_id = uuid.UUID(org["id"])

    bus = FakeBus()
    application.state.event_bus = bus
    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "human", "sms9-1")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "handoff"
    assert turns[-1].detail.startswith("keyword:")
    assert thread.ai_state == "handed_off"
    assert len(fake.sent) == before_sent  # no farewell for a keyword handoff
    assert len(bus.events) == 1
    published_org, event = bus.events[0]
    assert published_org == org_id
    assert event == {
        "type": "sms.handoff",
        "thread_id": str(thread.id),
        "reason": "keyword",
        "contact": CONTACT,
    }


async def test_handoff_to_human_tool_call_behaves_like_the_keyword_path(
    app_with_carrier, session, monkeypatch
):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms10@example.com", "Org 10")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    bus = FakeBus()
    application.state.event_bus = bus
    _script_next_turn(
        monkeypatch, [_tool_call("handoff_to_human", {"reason": "wants a human"})]
    )
    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "I want to speak to someone real", "sms10-1")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "handoff"
    assert turns[-1].detail == "tool:wants a human"
    assert thread.ai_state == "handed_off"
    assert len(fake.sent) == before_sent
    assert bus.events[0][1]["reason"] == "tool"


async def test_turn_ceiling_hands_off_with_a_final_message(
    app_with_carrier, session, monkeypatch
):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(
        client, "sms11@example.com", "Org 11", sms_turn_ceiling=2
    )
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(monkeypatch, [_text_reply("Answer one")])
    await _inbound_and_drain(client, "Question one", "sms11-1")
    _script_next_turn(monkeypatch, [_text_reply("Answer two")])
    await _inbound_and_drain(client, "Question two", "sms11-2")

    bus = FakeBus()
    application.state.event_bus = bus
    before_sent = len(fake.sent)
    # No scripted LLM response for this one: the ceiling must trip BEFORE any LLM call.
    await _inbound_and_drain(client, "Question three", "sms11-3")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "handoff"
    assert turns[-1].detail == "turn_ceiling"
    assert thread.ai_state == "handed_off"
    assert len(fake.sent) == before_sent + 1
    assert fake.sent[-1].text == sms_agent.FINAL_HANDOFF_MESSAGE
    assert bus.events[-1][1]["reason"] == "turn_ceiling"


async def test_ceiling_counts_reset_after_manual_takeover_and_rearm(
    app_with_carrier, session, monkeypatch
):
    """SHOULD-FIX 4: the ceiling counts replies SINCE THE LAST (RE)ARM, not all-time. A
    manual takeover + re-arm must reset the count, not inherit it and trip immediately."""
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(
        client, "sms-rearm-ceiling@example.com", "Org RearmCeiling", sms_turn_ceiling=2
    )
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(monkeypatch, [_text_reply("Answer one")])
    await _inbound_and_drain(client, "Question one", "sms-rc-1")
    _script_next_turn(monkeypatch, [_text_reply("Answer two")])
    await _inbound_and_drain(client, "Question two", "sms-rc-2")

    thread = await _thread(session, org_id)
    assert thread.ai_state == "active"

    # Manual takeover.
    taken_over = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "Jane here, taking over", "from": OUR},
        headers=h,
    )
    assert taken_over.status_code == 201, taken_over.text
    await session.refresh(thread)
    assert thread.ai_state == "handed_off"

    # Re-arm.
    rearmed = await client.post(
        f"/api/v1/threads/{thread.id}/ai", json={"state": "active"}, headers=h
    )
    assert rearmed.status_code == 200, rearmed.text

    _script_next_turn(monkeypatch, [_text_reply("Answer three")])
    await _inbound_and_drain(client, "Question three", "sms-rc-3")

    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "replied", "the re-armed thread must answer, not hand off again"
    assert fake.sent[-1].text == "Answer three"


async def test_llm_error_hands_off_without_crashing(app_with_carrier, session, monkeypatch):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms12@example.com", "Org 12")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    bus = FakeBus()
    application.state.event_bus = bus
    _script_next_turn(monkeypatch, [500])
    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "Tell me about pricing", "sms12-1")

    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "error"
    assert thread.ai_state == "handed_off"
    assert len(fake.sent) == before_sent
    assert bus.events[-1][1]["reason"] == "error"


# --------------------------------------------------------------------------------------
# Tool call round trip + reply clamping.
# --------------------------------------------------------------------------------------
async def test_book_appointment_tool_round_trip(app_with_carrier, session, monkeypatch):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms13@example.com", "Org 13")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(
        monkeypatch,
        [
            _tool_call("book_appointment", {"when": "tomorrow at 3pm", "notes": "call back"}),
            _text_reply("You're booked for tomorrow at 3pm!"),
        ],
    )
    await _inbound_and_drain(client, "Can we schedule a call for tomorrow at 3pm?", "sms13-1")

    thread = await _thread(session, org_id)
    set_org_context(session, org_id)
    appts = (
        await session.execute(sa.select(Appointment).where(Appointment.contact_e164 == CONTACT))
    ).scalars().all()
    assert len(appts) == 1
    assert appts[0].raw_when == "tomorrow at 3pm"
    assert appts[0].call_id is None
    assert appts[0].created_by == "ai"

    assert fake.sent[-1].text == "You're booked for tomorrow at 3pm!"
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "replied"


async def test_reply_is_clamped_to_sms_max_reply_chars(app_with_carrier, session, monkeypatch):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(
        client, "sms14@example.com", "Org 14", sms_max_reply_chars=20
    )
    _enable_llm_key(application)

    long_text = "This is a very long reply that definitely exceeds twenty characters."
    _script_next_turn(monkeypatch, [_text_reply(long_text)])
    await _inbound_and_drain(client, "Give me a long answer please", "sms14-1")

    assert fake.sent[-1].text == long_text[:20]
    assert len(fake.sent[-1].text) == 20


# --------------------------------------------------------------------------------------
# Human takeover + re-arm.
# --------------------------------------------------------------------------------------
async def test_human_manual_send_takes_over_and_silences_the_bot(
    app_with_carrier, session, monkeypatch
):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms15@example.com", "Org 15")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(monkeypatch, [_text_reply("Hello!")])
    await _inbound_and_drain(client, "Hi", "sms15-1")
    thread = await _thread(session, org_id)
    assert thread.ai_state == "active"

    # A human operator replies manually in the same thread.
    sent = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "This is Jane, following up personally", "from": OUR},
        headers=h,
    )
    assert sent.status_code == 201, sent.text
    await session.refresh(thread)
    assert thread.ai_state == "handed_off"

    # The bot must stay silent on the next inbound.
    before_sent = len(fake.sent)
    await _inbound_and_drain(client, "Thanks Jane!", "sms15-2")
    assert len(fake.sent) == before_sent

    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "skipped"
    assert turns[-1].detail == "handed_off"


async def test_help_auto_reply_does_not_take_over_an_active_thread(
    app_with_carrier, session, monkeypatch
):
    """BLOCKER 2: the compliance keyword engine's HELP/START confirmations are sent with
    an exemption - that must not look like a human operator taking the thread over."""
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms-help@example.com", "Org Help")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(monkeypatch, [_text_reply("Hello!")])
    await _inbound_and_drain(client, "Hi", "sms-help-1")
    thread = await _thread(session, org_id)
    assert thread.ai_state == "active"

    sent_before = len(fake.sent)
    await _inbound_and_drain(client, "HELP", "sms-help-2")

    assert len(fake.sent) == sent_before + 1, "HELP must still be auto-answered"
    assert "help" in fake.sent[-1].text.lower()
    await session.refresh(thread)
    assert thread.ai_state == "active", "an exempted compliance auto-reply must not take over"


async def test_rearm_endpoint_lets_the_bot_answer_again(app_with_carrier, session, monkeypatch):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms16@example.com", "Org 16")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(monkeypatch, [_tool_call("handoff_to_human", {"reason": "x"})])
    await _inbound_and_drain(client, "Hi", "sms16-1")
    thread = await _thread(session, org_id)
    assert thread.ai_state == "handed_off"

    got = await client.get(f"/api/v1/threads/{thread.id}/ai", headers=h)
    assert got.status_code == 200
    assert got.json()["ai_state"] == "handed_off"

    rearmed = await client.post(
        f"/api/v1/threads/{thread.id}/ai", json={"state": "active"}, headers=h
    )
    assert rearmed.status_code == 200, rearmed.text
    assert rearmed.json()["ai_state"] == "active"

    _script_next_turn(monkeypatch, [_text_reply("Yes, still here!")])
    await _inbound_and_drain(client, "Still there?", "sms16-2")

    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "replied"
    assert fake.sent[-1].text == "Yes, still here!"


async def test_take_over_endpoint_flips_state_manually(app_with_carrier, session, monkeypatch):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms17@example.com", "Org 17")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)

    _script_next_turn(monkeypatch, [_text_reply("Hello!")])
    await _inbound_and_drain(client, "Hi", "sms17-1")
    thread = await _thread(session, org_id)
    assert thread.ai_state == "active"

    taken_over = await client.post(
        f"/api/v1/threads/{thread.id}/ai", json={"state": "handed_off"}, headers=h
    )
    assert taken_over.status_code == 200
    await session.refresh(thread)
    assert thread.ai_state == "handed_off"


# --------------------------------------------------------------------------------------
# BLOCKER 3: a handoff published from the AUTO-triggered path reaches the REAL event bus
# (no monkeypatching of sms_agent._default_bus anywhere in this test).
# --------------------------------------------------------------------------------------
async def test_auto_trigger_handoff_reaches_the_real_event_bus(
    app_with_carrier, session, monkeypatch
):
    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms18@example.com", "Org 18")
    org_id = uuid.UUID(org["id"])
    _enable_llm_key(application)
    _script_next_turn(monkeypatch, [_tool_call("handoff_to_human", {"reason": "real bus test"})])

    async with application.state.event_bus.subscribe(org_id) as queue:
        await _inbound(client, "I need a real person", "sms18-1")
        await sms_agent.wait_for_pending_sms_tasks()
        event = await asyncio.wait_for(queue.get(), timeout=5)

    thread = await _thread(session, org_id)
    assert event == {
        "type": "sms.handoff",
        "thread_id": str(thread.id),
        "reason": "tool",
        "contact": CONTACT,
    }
    assert thread.ai_state == "handed_off"


# --------------------------------------------------------------------------------------
# NIT (c): a rejected or held (not-yet-sent) outbound never appears in what the LLM sees.
# --------------------------------------------------------------------------------------
async def test_load_history_excludes_rejected_and_held_outbounds(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token, org, h, _ = await _make_sms_org(client, "sms-hist@example.com", "Org Hist")
    org_id = uuid.UUID(org["id"])

    await _inbound_and_drain(client, "Hi", "sms-hist-1")
    thread = await _thread(session, org_id)

    set_org_context(session, org_id)
    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread.id,
            direction="outbound",
            status="rejected",
            from_e164=OUR,
            to_e164=CONTACT,
            body="never delivered",
        )
    )
    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread.id,
            direction="outbound",
            status="queued",
            from_e164=OUR,
            to_e164=CONTACT,
            body="still queued for quiet hours",
            hold_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    await session.commit()

    history = await sms_agent._load_history(session, thread.id)
    bodies = [turn.content for turn in history]
    assert "never delivered" not in bodies
    assert "still queued for quiet hours" not in bodies
    assert "Hi" in bodies


# --------------------------------------------------------------------------------------
# NIT (d): a deferred (quiet-hours-held) send is recorded with its own detail.
# --------------------------------------------------------------------------------------
async def test_try_send_records_deferred_detail_when_the_send_is_held(
    app_with_carrier, session, monkeypatch
):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "sms-defer@example.com", "Org Defer", OUR)
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, CONTACT)
    await session.commit()

    inbound = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="inbound",
        status="received",
        from_e164=CONTACT,
        to_e164=OUR,
        body="hi",
    )
    session.add(inbound)
    await session.commit()

    turn = AgentSmsTurn(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        inbound_message_id=inbound.id,
        status="skipped",
        detail="",
    )
    session.add(turn)
    await session.commit()

    held_hold_until = datetime.now(timezone.utc) + timedelta(hours=1)

    async def fake_send_message(*args, **kwargs):
        # A real, persisted row - AgentSmsTurn.outbound_message_id is a real FK.
        outbound = Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread.id,
            direction="outbound",
            status="queued",
            from_e164=OUR,
            to_e164=CONTACT,
            body="reply",
            hold_until=held_hold_until,
        )
        session.add(outbound)
        await session.flush()
        return outbound

    monkeypatch.setattr(sms_agent, "send_message", fake_send_message)

    sent_id = await sms_agent._try_send(
        session, org_id, fake, FakeBus(), thread=thread, body="reply", turn=turn
    )

    assert sent_id is not None
    assert turn.status == "replied"
    assert turn.detail == "deferred"
    assert turn.outbound_message_id == sent_id
