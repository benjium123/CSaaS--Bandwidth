"""AI SMS agent: per-thread turn engine (P10, plan DR-1..DR-7).

DR-1: the "shared brain" is the SERVICE layer. This module calls
``services.agent``'s helpers and ``services.kb.search`` in-process - never over the
LiveKit worker's HTTP seams, which exist only for the voice worker's out-of-process
latency reasons that do not apply to a request/response SMS turn.

DR-2: the trigger is post-commit, idempotent, and must never block the webhook.
``webhooks.py`` stashes the app's real ``event_bus``/``settings`` on ``session.info``
before the ingestion loop runs (the one change it is allowed); ``messaging._ingest_inbound``
reads them back off the session and hands them to ``spawn_from_ingest``. Either one being
absent (a caller that never populated ``session.info`` - direct service-layer calls, older
tests) falls back to a fresh ``load_settings()`` / a private process-local ``EventBus``
singleton, both exposed as monkeypatchable module functions (``_default_http_client``,
``_default_bus``) so tests can inject a mocked transport without touching the webhook route.

Idempotency (DR-2): ``agent_sms_turns.inbound_message_id`` is UNIQUE. The very first
thing this module does for a given inbound message is INSERT a placeholder row and
COMMIT it immediately - that row is the durable idempotency token, so a redelivery loser
fails fast on the unique constraint instead of blocking on a row lock for the rest of this
turn's work. Every subsequent decision UPDATEs that same row.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.compliance import service as compliance_svc
from app.compliance.keywords import classify_keyword
from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ComplianceBlockedError
from app.models import AgentSmsTurn, Appointment, Message, MessageThread
from app.services import agent as agent_svc
from app.services import kb as kb_svc
from app.services import llm_client
from app.services.messaging import AI_SEND_KEY, send_message
from app.services.outbox import record_platform_event

log = structlog.get_logger("sms_agent")

MAX_TOOL_ROUNDS = 3
MAX_HISTORY_MESSAGES = 20
FINAL_HANDOFF_MESSAGE = "Connecting you with a member of our team."

SYSTEM_PREAMBLE_TEMPLATE = (
    "\n\nYou are replying over SMS. Keep the reply under {limit} characters, plain text "
    "only (no markdown, no links formatting). Never mention opt-out keywords such as STOP "
    "or unsubscribe. If the person asks for a human, or you cannot help them, call the "
    "handoff_to_human tool instead of guessing."
)

_TOOL_SPECS = [
    llm_client.ToolSpec(
        name="book_appointment",
        description="Book an appointment for this contact.",
        parameters={
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "The requested date/time, verbatim."},
                "notes": {"type": "string", "description": "Any relevant notes."},
            },
            "required": ["when"],
        },
    ),
    llm_client.ToolSpec(
        name="kb_search",
        description="Search this org's knowledge base for an answer.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
    llm_client.ToolSpec(
        name="handoff_to_human",
        description="Hand this conversation off to a human team member.",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    ),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------------
# Background task tracking - same shape as voice_plane.service._DIAL_TASKS: held here so
# nothing garbage-collects an in-flight task mid-turn, and so tests can await every
# pending turn deterministically instead of sleeping/polling for one to land.
# ----------------------------------------------------------------------------------
_SMS_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _SMS_TASKS.add(task)
    task.add_done_callback(_SMS_TASKS.discard)
    return task


async def wait_for_pending_sms_tasks() -> None:
    """Test-only hook: await every in-flight background SMS-agent turn deterministically.
    Safe to call with nothing pending."""
    pending = list(_SMS_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ----------------------------------------------------------------------------------
# Runtime seams. See the module docstring for why these exist instead of `request.app`.
# Tests monkeypatch these functions directly, or pass bus/settings straight through
# spawn_from_ingest via session.info (the normal webhook path, since the review fix).
# ----------------------------------------------------------------------------------
_fallback_bus = None


def _default_bus():
    """A process-local EventBus, used only when nobody handed spawn_from_ingest a real
    one (see module docstring). Tests that care about the published event monkeypatch
    this function."""
    global _fallback_bus
    if _fallback_bus is None:
        from app.events.bus import EventBus

        _fallback_bus = EventBus()
    return _fallback_bus


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


async def org_could_reply(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """A fast, SAME-SESSION probe: could this org's agent conceivably act on the message
    that was just committed? Used by messaging._ingest_inbound to decide whether to
    spawn a background turn at all.

    This is not a correctness requirement of DR-2 (maybe_reply already re-checks
    everything from scratch, safely, in its own session) - it exists purely to avoid
    opening a SECOND, genuinely concurrent database session for the overwhelming
    majority of inbound messages, where no sms_enabled profile exists anywhere on the
    org. On SQLite's single shared test connection, a concurrent session doing its own
    commits can invalidate an unrelated, unfinished cursor on a completely unrelated
    test's session (observed: a media-pipeline test's own query corrupted by this
    background task's commit) - Postgres pools each connection independently and would
    never hit this, but skipping the spawn entirely when there is nothing to do is
    strictly cheaper AND removes the hazard, so it is worth doing regardless of backend.
    """
    profile = await agent_svc._pick_profile(session, org_id)
    return profile is not None and profile.sms_enabled


def spawn_from_ingest(
    *,
    inbound_message_id: uuid.UUID,
    carrier,  # noqa: ANN001 - MessagingCarrier protocol, may be None
    bus=None,  # noqa: ANN001 - EventBus; None falls back to _default_bus()
    settings: Settings | None = None,  # None falls back to a fresh load_settings()
) -> asyncio.Task:
    """Called by ``messaging._ingest_inbound``, post-commit (DR-2). Fire-and-forget: this
    function itself never raises, so it can never turn an inbound webhook into a 500 no
    matter what happens once the task actually runs."""

    async def _run() -> None:
        try:
            from app.db.session import get_sessionmaker

            resolved_settings = settings
            if resolved_settings is None:
                from app.config import load_settings

                resolved_settings = load_settings()
            resolved_bus = bus if bus is not None else _default_bus()
            await maybe_reply(
                get_sessionmaker(),
                resolved_settings,
                resolved_bus,
                inbound_message_id=inbound_message_id,
                carrier=carrier,
            )
        except Exception:  # noqa: BLE001 - background task: must never crash the loop
            log.exception("sms_agent_trigger_failed", inbound_message_id=str(inbound_message_id))

    return _spawn(_run())


# ----------------------------------------------------------------------------------
# Handoff keyword matching (plan: "case-insensitive whole-phrase match" - never a
# substring search, same anti-footer-injection discipline as compliance/keywords.py).
# ----------------------------------------------------------------------------------
def _matches_handoff_keyword(body: str | None, keywords: list[str] | None) -> str | None:
    normalized = (body or "").strip().casefold()
    if not normalized:
        return None
    for kw in keywords or []:
        candidate = str(kw or "").strip().casefold()
        if candidate and normalized == candidate:
            return str(kw)
    return None


async def _book_appointment_sms(
    session: AsyncSession, org_id: uuid.UUID, *, contact_e164: str, raw_when: str, notes: str
) -> Appointment:
    """``services.agent.book_appointment`` hard-requires a ``Call`` row (it reads
    ``call.org_id``/``call.id``), which an SMS turn does not have. ``Appointment.call_id``
    is nullable for exactly this reason, so this mirrors that function's row shape with
    ``call_id=None`` instead, reusing its date-parsing helper rather than duplicating it."""
    appt = Appointment(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=None,
        contact_e164=contact_e164,
        raw_when=raw_when,
        scheduled_for=agent_svc._parse_scheduled_for(raw_when),
        notes=notes,
        status="booked",
        created_by="ai",
    )
    session.add(appt)
    # P13 DR-4: outbox row commits with the appointment itself.
    record_platform_event(
        session,
        org_id,
        "appointment.booked",
        {
            "appointment_id": str(appt.id),
            "contact_e164": contact_e164,
            "raw_when": raw_when,
            "scheduled_for": appt.scheduled_for.isoformat() if appt.scheduled_for else None,
            "source": "sms",
        },
    )
    await session.flush()
    return appt


def _apply_turn_usage(turn: AgentSmsTurn, usage: list[tuple[int, int]]) -> None:
    """P13 DR-9: token totals across the turn's LLM rounds; untouched (NULL) when no
    round completed."""
    if usage:
        turn.tokens_in = sum(t for t, _ in usage)
        turn.tokens_out = sum(t for _, t in usage)


def _publish_handoff(bus, org_id: uuid.UUID, thread: MessageThread, *, reason: str) -> None:  # noqa: ANN001
    bus.publish(
        org_id,
        {
            "type": "sms.handoff",
            "thread_id": str(thread.id),
            "reason": reason,
            "contact": thread.contact_e164,
        },
    )


# ----------------------------------------------------------------------------------
# LLM turn: conversation history + tool loop.
# ----------------------------------------------------------------------------------
async def _load_history(session: AsyncSession, thread_id: uuid.UUID) -> list[llm_client.ChatTurn]:
    """The last MAX_HISTORY_MESSAGES messages, oldest first. A `rejected` outbound never
    reached the contact and a held one (`hold_until` set) has not gone out YET - neither
    is something the contact could be responding to, so both are excluded: showing the
    model a reply it thinks it already sent invites it to reference something the contact
    never received."""
    rows = (
        await session.execute(
            sa.select(Message)
            .where(
                Message.thread_id == thread_id,
                sa.or_(
                    Message.direction == "inbound",
                    sa.and_(
                        Message.direction == "outbound",
                        Message.status != "rejected",
                        Message.hold_until.is_(None),
                    ),
                ),
            )
            .order_by(Message.created_at.desc())
            .limit(MAX_HISTORY_MESSAGES)
        )
    ).scalars().all()
    ordered = list(reversed(rows))
    return [
        llm_client.ChatTurn(
            role="user" if m.direction == "inbound" else "assistant", content=m.body or ""
        )
        for m in ordered
    ]


class _ToolHandoff(Exception):
    """Internal signal: the model called handoff_to_human mid-loop."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def _run_tool_call(
    session: AsyncSession, org_id: uuid.UUID, thread: MessageThread, call: llm_client.ToolCall
) -> str:
    if call.name == "book_appointment":
        when = str(call.arguments.get("when") or "").strip() or "unspecified"
        notes = str(call.arguments.get("notes") or "")
        appt = await _book_appointment_sms(
            session, org_id, contact_e164=thread.contact_e164, raw_when=when, notes=notes
        )
        return f"Booked for {appt.raw_when}."
    if call.name == "kb_search":
        query = str(call.arguments.get("query") or "")
        hits = await kb_svc.search(session, org_id, query)
        if not hits:
            return "No matching knowledge base articles were found."
        return json.dumps([{"title": h["title"], "text": h["text"]} for h in hits])
    if call.name == "handoff_to_human":
        raise _ToolHandoff(str(call.arguments.get("reason") or "requested"))
    return "Unknown tool."


async def _run_llm_turn(
    client: httpx.AsyncClient,
    *,
    provider: str,
    model: str,
    api_key: str,
    system: str,
    history: list[llm_client.ChatTurn],
    session: AsyncSession,
    org_id: uuid.UUID,
    thread: MessageThread,
    usage_sink: list[tuple[int, int]] | None = None,
) -> tuple[str, str | None]:
    """Returns (reply_text, handoff_reason). ``handoff_reason`` is set (and reply_text is
    "") when the model called handoff_to_human. Raises on any LLM/transport failure - the
    caller decides how that becomes a turn/thread state change.

    ``usage_sink`` (P13 DR-9) collects (tokens_in, tokens_out) per completed LLM round -
    a sink rather than a return value so rounds already paid for survive a mid-turn
    exception."""
    turns = list(history)
    last_text = ""
    for _round in range(MAX_TOOL_ROUNDS):
        result = await llm_client.chat(
            client,
            provider=provider,
            model=model,
            api_key=api_key,
            system=system,
            turns=turns,
            tools=_TOOL_SPECS,
        )
        if usage_sink is not None:
            usage_sink.append((result.tokens_in, result.tokens_out))
        last_text = result.text
        if not result.tool_calls:
            return last_text, None

        turns.append(
            llm_client.ChatTurn(role="assistant", content=result.text, tool_calls=result.tool_calls)
        )
        for call in result.tool_calls:
            try:
                tool_text = await _run_tool_call(session, org_id, thread, call)
            except _ToolHandoff as exc:
                return "", exc.reason
            turns.append(
                llm_client.ChatTurn(
                    role="tool", tool_call_id=call.id, tool_name=call.name, content=tool_text
                )
            )
    return last_text, None


# ----------------------------------------------------------------------------------
# Main entry point.
# ----------------------------------------------------------------------------------
async def maybe_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    bus,  # noqa: ANN001 - EventBus
    *,
    inbound_message_id: uuid.UUID,
    carrier=None,  # noqa: ANN001 - MessagingCarrier protocol, may be None
    http_client: httpx.AsyncClient | None = None,
) -> None:
    async with sessionmaker() as session:
        try:
            await _maybe_reply_inner(
                session,
                settings,
                bus,
                inbound_message_id=inbound_message_id,
                carrier=carrier,
                http_client=http_client,
            )
        except Exception:  # noqa: BLE001 - background task: must never crash the loop
            log.exception("sms_agent_turn_crashed", inbound_message_id=str(inbound_message_id))
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                pass


async def _maybe_reply_inner(
    session: AsyncSession,
    settings: Settings,
    bus,  # noqa: ANN001
    *,
    inbound_message_id: uuid.UUID,
    carrier,  # noqa: ANN001
    http_client: httpx.AsyncClient | None,
) -> None:
    # Captured ONCE, up front - if this turn ends up arming the thread (below), this is
    # the value stamped as ai_armed_at. The ORM's created_at default for `turn` (built and
    # flushed a few lines down) fires strictly AFTER this statement runs, so
    # `turn.created_at >= now` always holds - which is what lets THIS turn's own eventual
    # `replied` row count toward the ceiling for every later message. Computing ai_armed_at
    # fresh at arm time instead (chronologically after the claim) would make it always
    # exclude this turn, since claiming happens before arming can even be decided.
    now = _now()

    message = await session.get(
        Message, inbound_message_id, execution_options={ALLOW_UNSCOPED_KEY: True}
    )
    if message is None or message.direction != "inbound":
        return
    org_id = message.org_id
    set_org_context(session, org_id)

    thread = await session.get(MessageThread, message.thread_id)
    if thread is None:
        return

    # DR-2: claim ownership of this inbound message FIRST, and COMMIT immediately - the
    # row is the durable idempotency token, not a hope held open across the rest of this
    # turn's work. A redelivered webhook (or a racing manual re-invocation in tests) then
    # loses the IntegrityError race and fails FAST on the unique constraint rather than
    # blocking on a row lock for however long this turn's LLM call takes.
    turn = AgentSmsTurn(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        inbound_message_id=inbound_message_id,
        status="skipped",
        detail="",
    )
    try:
        async with session.begin_nested():
            session.add(turn)
            await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return
    # Re-establish org context: nothing actually clears session.info across a commit, but
    # every other commit boundary in this codebase re-affirms it rather than relying on
    # that being true forever.
    set_org_context(session, org_id)

    async def _finish(status: str, detail: str = "") -> None:
        turn.status = status
        turn.detail = detail[:255]
        await session.commit()

    profile = await agent_svc._pick_profile(session, org_id)
    if profile is None or not profile.sms_enabled:
        await _finish("skipped", "no_profile" if profile is None else "sms_disabled")
        return

    # Keyword/opt-out checks run BEFORE the off->active arming transition below, so a
    # contact's very first message being STOP (or any compliance keyword) never arms the
    # thread - the keyword engine, not the AI, owns that message; arming on it would be
    # arming on a message the agent was never going to answer anyway.
    hit = classify_keyword(message.body)
    if hit is not None:
        await _finish("skipped", f"compliance_keyword:{hit.kind}")
        return

    if await compliance_svc.is_opted_out(session, thread.contact_e164):
        # One status for one real-world condition: this contact cannot be sent anything,
        # which is exactly what "blocked" already means elsewhere in this ledger - it is
        # not merely a scheduling skip.
        await _finish("blocked", "opted_out")
        return

    if thread.ai_state == "off":
        # A fresh thread wakes up the first time an sms_enabled profile sees it. Stamped
        # with `now` (captured up top, BEFORE this turn was even claimed) rather than a
        # fresh timestamp here - see that capture's comment for why.
        thread.ai_state = "active"
        thread.ai_armed_at = now
    elif thread.ai_state == "handed_off":
        await _finish("skipped", "handed_off")
        return

    keyword_hit = _matches_handoff_keyword(message.body, profile.sms_handoff_keywords or [])
    if keyword_hit is not None:
        thread.ai_state = "handed_off"
        turn.status = "handoff"
        turn.detail = f"keyword:{keyword_hit}"[:255]
        await session.commit()
        _publish_handoff(bus, org_id, thread, reason="keyword")
        return

    replied_count = await _count_replies_since_armed(session, thread.id, thread.ai_armed_at)
    if replied_count >= profile.sms_turn_ceiling:
        thread.ai_state = "handed_off"
        # handoff_on_error=False: the handoff decision here is unconditional (DR-7), not
        # contingent on the farewell actually going out - so we publish it ourselves
        # below rather than letting _try_send's own failure path do it (which would
        # double-fire the bus event).
        await _try_send(
            session,
            org_id,
            carrier,
            bus,
            thread=thread,
            body=FINAL_HANDOFF_MESSAGE,
            turn=turn,
            on_success_status="handoff",
            on_success_detail="turn_ceiling",
            handoff_on_error=False,
        )
        _publish_handoff(bus, org_id, thread, reason="turn_ceiling")
        return

    provider = (profile.llm_provider or "anthropic").strip().lower()
    if provider not in ("anthropic", "openai"):
        provider = "anthropic"
    api_key = (
        settings.anthropic_api_key.get_secret_value()
        if provider == "anthropic"
        else settings.openai_api_key.get_secret_value()
    )
    system = (profile.system_prompt or "") + SYSTEM_PREAMBLE_TEMPLATE.format(
        limit=profile.sms_max_reply_chars
    )
    history = await _load_history(session, thread.id)

    owns_client = http_client is None
    client = http_client or _default_http_client()
    usage: list[tuple[int, int]] = []
    try:
        reply_text, handoff_reason = await _run_llm_turn(
            client,
            provider=provider,
            model=profile.llm_model or "",
            api_key=api_key,
            system=system,
            history=history,
            session=session,
            org_id=org_id,
            thread=thread,
            usage_sink=usage,
        )
    except Exception as exc:  # noqa: BLE001 - LLMError and anything else: error + handoff
        _apply_turn_usage(turn, usage)
        thread.ai_state = "handed_off"
        turn.status = "error"
        turn.detail = str(exc)[:255]
        await session.commit()
        _publish_handoff(bus, org_id, thread, reason="error")
        return
    finally:
        if owns_client:
            await client.aclose()
    _apply_turn_usage(turn, usage)

    if handoff_reason is not None:
        thread.ai_state = "handed_off"
        turn.status = "handoff"
        turn.detail = f"tool:{handoff_reason}"[:255]
        await session.commit()
        _publish_handoff(bus, org_id, thread, reason="tool")
        return

    reply_text = (reply_text or "").strip()[: profile.sms_max_reply_chars]
    if not reply_text:
        thread.ai_state = "handed_off"
        turn.status = "error"
        turn.detail = "empty_reply"
        await session.commit()
        _publish_handoff(bus, org_id, thread, reason="error")
        return

    await _try_send(session, org_id, carrier, bus, thread=thread, body=reply_text, turn=turn)


async def _count_replies_since_armed(
    session: AsyncSession, thread_id: uuid.UUID, ai_armed_at: datetime | None
) -> int:
    """AI replies in this thread since the last (re)arm (plan DR-7): every `replied` turn
    at or after `ai_armed_at`, or every `replied` turn if the thread has never been armed
    (ai_armed_at is only NULL for a thread that predates this column - treat it as "count
    everything", the old behaviour, rather than as an unbounded free pass)."""
    stmt = sa.select(sa.func.count()).select_from(AgentSmsTurn).where(
        AgentSmsTurn.thread_id == thread_id, AgentSmsTurn.status == "replied"
    )
    if ai_armed_at is not None:
        # >=, not >: ai_armed_at is stamped from a timestamp captured BEFORE the arming
        # message's own AgentSmsTurn row is even claimed (see _maybe_reply_inner's `now`),
        # so that row's created_at is always >= ai_armed_at, never strictly greater by a
        # comfortable margin - a strict > risks excluding it on a coarse clock.
        stmt = stmt.where(AgentSmsTurn.created_at >= ai_armed_at)
    return (await session.execute(stmt)).scalar_one()


async def _try_send(
    session: AsyncSession,
    org_id: uuid.UUID,
    carrier,  # noqa: ANN001
    bus,  # noqa: ANN001
    *,
    thread: MessageThread,
    body: str,
    turn: AgentSmsTurn,
    on_success_status: str = "replied",
    on_success_detail: str = "",
    handoff_on_error: bool = True,
) -> uuid.UUID | None:
    """Send one AI-originated reply through the normal, ungated-by-nothing send path
    (plan DR-4: no exemption, ever).

    The turn's status is set OPTIMISTICALLY to ``on_success_status`` before send_message
    is even called, then a failure branch below OVERWRITES it - so that whichever way
    this ends up, there is exactly ONE commit for the "attempted a send" path, covering
    both "the message went out" (send_message's own commit) and "this is what we sent /
    why it didn't go out" (the turn row) as a single unit. That closes the crash window a
    two-commit version would leave open, where the message really sent but the turn
    ledger still read its "skipped" claim placeholder.

    ``ComplianceBlockedError`` -> turn "blocked", thread stays active, no retry (DR-4).
    Any other failure -> turn "error"; when ``handoff_on_error`` the thread is also handed
    off and the handoff is published here. The turn-ceiling caller passes
    ``handoff_on_error=False`` because IT already owns that decision unconditionally and
    would otherwise double-publish the handoff event.
    """
    if carrier is None:
        turn.status = "error"
        turn.detail = "no_carrier_configured"
        if handoff_on_error:
            thread.ai_state = "handed_off"
        await session.commit()
        if handoff_on_error:
            _publish_handoff(bus, org_id, thread, reason="error")
        return None

    turn.status = on_success_status
    turn.detail = on_success_detail
    result_id: uuid.UUID | None = None
    should_publish_error = False

    session.info[AI_SEND_KEY] = True
    try:
        sent = await send_message(
            session,
            org_id,
            carrier,
            to_e164=thread.contact_e164,
            from_e164=thread.our_e164,
            body=body,
        )
    except ComplianceBlockedError as exc:
        turn.status = "blocked"
        turn.detail = str(exc)[:255]
    except Exception as exc:  # noqa: BLE001 - carrier/transport failure: error, not a crash
        turn.status = "error"
        turn.detail = str(exc)[:255]
        if handoff_on_error:
            thread.ai_state = "handed_off"
            should_publish_error = True
    else:
        # A quiet-hours hold means the message has NOT actually reached the contact yet -
        # the sweeper releases it later. That is worth its own detail rather than looking
        # identical to an immediate send.
        turn.detail = "deferred" if sent.hold_until is not None else on_success_detail
        turn.outbound_message_id = sent.id
        result_id = sent.id
    finally:
        session.info.pop(AI_SEND_KEY, None)

    await session.commit()
    if should_publish_error:
        _publish_handoff(bus, org_id, thread, reason="error")
    return result_id
