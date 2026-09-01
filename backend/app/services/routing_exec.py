"""P12 routing executors (DR-1): translate the pure flow-engine state machine
(``services/flow_engine.py``) into real commands/state on the two paths a call can take.

**Carrier executor** (``start_carrier_flow`` / ``continue_carrier_flow``, called from
``api/routes/webhooks.py`` where the P6 comment marked the spot) drives the engine to the
next point that needs caller input or ends the request, translating actions into the
neutral P5 ``VoiceCommand`` set (Speak / Play / Gather / StartRecording / Hangup / Pause /
Transfer). Two of DR-2's node types cannot be honestly expressed with that command set,
and this module does the one honest thing it CAN for each rather than fake a bridge:

* ``ring_group`` - ``Transfer`` needs an E.164 target; a ring-group member is a browser
  softphone user with no PSTN address to dial, and the neutral command set has no "ring
  this room" primitive. The carrier leg holds for real (``Speak`` + a bounded ``Pause``,
  capped at ``CARRIER_HOLD_CAP_SECONDS``) and then the engine is stepped with
  ``ring_result=no_answer`` - exactly what would happen if nobody picked up, because
  nobody genuinely can on this path. No participant is ever pretended to have joined.
* ``queue`` - indefinite hold needs the call to come back to us repeatedly (a redirect
  loop, or the media/agent worker); the neutral command set has neither. The carrier
  executor creates the real ``QueueEntry`` row, holds with ``Gather`` (a digit press -
  ``CALLBACK_DIGIT`` - captures a callback mid-wait per DR-6) bounded the same way. A bare
  Gather timeout re-issues the SAME hold Gather as long as the queue's real
  ``max_wait_seconds`` has not yet elapsed (B10); only once it genuinely has, or a caller
  presses any other digit, does the queue's configured ``overflow`` apply inline, in the
  SAME webhook response.

``transfer`` IS expressible directly: it maps to a plain P5 ``Transfer`` command (blind
transfer to a real E.164 number) - the carrier executor's only route to a live human until
the room path has a live SIP trunk of its own. The room executor below does not run the
flow graph at all (see its own section), so ``transfer`` is carrier-path only in this
build; a room-native flow-driven transfer would call
``voice_plane.service.transfer_room_call`` (forbidden to touch here).

**Room executor** (``routing_tick``, the ``QueueEntry``/claim functions below) implements
the real thing for a LiveKit room call: targeted ``call.ring`` (``ring_user_ids``),
sequential-group stepping and max-wait/abandon detection on a periodic sweeper tick
(``routing_tick``), and claim-to-connect. It cannot touch ``voice_plane/service.py``
(forbidden), so it never watches for the PSTN caller hanging up in real time the way the
webhook path does - ``routing_tick`` instead notices a queued call reached a terminal
``Call.status`` on its next pass and marks the entry ``abandoned`` then. That is a real,
if sweeper-cadence-bounded, detection - never a fabricated one. The OFFER branches
(``_offer_to_ring_group``) only ever fire for a call whose ``extra["via"] == "livekit"``
(B6) - a carrier-path queue entry is already being driven inline by the same webhook
response that enqueued it, and offering it to a room agent who has no way to answer a
PSTN-only call would just ring someone for nothing.

The one true "ring a room" claim mechanism this build has is the pre-existing
``POST /calls/{id}/answer`` route (``api/routes/calls.py``): it mints a token for ANY
LiveKit room call, publishes ``call.handoff.claimed``, AND (as of the B12 fix landed
alongside this module) resolves any matching ``QueueEntry`` to ``connected`` itself. This
module's own ``/queue-entries/{id}/claim`` (routes/flows.py) is the pull-based
counterpart for an agent working a queue directly; both use a single conditional
``UPDATE ... WHERE state IN ('waiting','offered')`` so first-answer-wins even under a real
race (B9) and publish the same ``call.handoff.claimed`` shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, NotFoundError
from app.models.callflow import (
    BusinessHours,
    CallFlow,
    CallQueue,
    QueueEntry,
    RingGroupDef,
)
from app.models.messaging import OrgNumber
from app.models.voice import TERMINAL_CALL_STATUSES, Call
from app.providers import voice
from app.services import flow_engine as fe
from app.services import flows as flows_svc
from app.services import voicemail as voicemail_svc

log = structlog.get_logger("routing_exec")

#: A carrier leg can only ever hold for real - never "as if" - so every inline wait this
#: module renders (ring_group / queue) is capped here even when the underlying
#: ring_timeout_seconds / max_wait_seconds configured is longer. See module docstring.
CARRIER_HOLD_CAP_SECONDS = 60
DEFAULT_RING_TIMEOUT_SECONDS = 20
#: v1 convention (no queue-node schema field exists for this - flow_engine.py is
#: hands-off): press this digit while on carrier-path hold to request a callback (DR-6).
CALLBACK_DIGIT = "9"
#: B4: hours<->hours (or any other inline-resolved action) can cycle forever at RUNTIME
#: even though validate_flow's reachability check (which only proves every node is
#: reachable, not that stepping through them terminates) lets it save clean. Capped, then
#: treated exactly like a FlowError - never an actual infinite loop in a webhook handler.
_MAX_DRIVE_ITERATIONS = 25


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _find_voicemail_node(definition: dict) -> str | None:
    nodes = definition.get("nodes") or {}
    for node_id, node in nodes.items():
        if isinstance(node, dict) and node.get("type") == "voicemail":
            return node_id
    return None


def _persist_flow_state(
    call: Call,
    flow: CallFlow,
    *,
    state: dict,
    awaiting: str | None,
    terminal: str | None,
    extra: dict | None = None,
) -> None:
    payload: dict = {
        "flow_id": str(flow.id),
        "version": flow.version,
        "state": state,
        "awaiting": awaiting,
        "terminal": terminal,
    }
    if extra is not None:
        payload["extra"] = extra
    call.extra = {**(call.extra or {}), "flow": payload}


# ========================================================================================
# CARRIER executor
# ========================================================================================
async def resolve_inbound_flow(session: AsyncSession, our_e164: str) -> CallFlow | None:
    """None means "keep today's default behaviour" - either the number isn't bound to a
    flow at all, or (defensively) the bound row has vanished."""
    org_number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == our_e164))
    ).scalar_one_or_none()
    if org_number is None or org_number.call_flow_id is None:
        return None
    return await session.get(CallFlow, org_number.call_flow_id)


async def start_carrier_flow(
    session: AsyncSession,
    bus,
    call: Call,
    flow: CallFlow,
    *,
    now: datetime | None = None,  # noqa: ANN001
) -> list[voice.VoiceCommand]:
    existing = (call.extra or {}).get("flow") or {}
    if existing.get("flow_id") == str(flow.id):
        # B5: a redelivered call_initiated for a call already pinned to this exact flow
        # row - NEVER re-enter the graph. fe.start() always begins at the entry node, so
        # re-running it here would re-create QueueEntry/Voicemail rows and re-record
        # outbox events for a call that has already progressed. Re-render the SAME
        # commands from what is already persisted instead.
        return await _rerender_from_state(session, call, flow, existing)
    try:
        result = fe.start(flow.definition)
    except fe.FlowError:
        log.exception("flow_start_failed", flow_id=str(flow.id), call_id=str(call.id))
        return await _fallback(session, bus, call, flow)
    return await _drive(session, bus, call, flow, result, now=now)


async def continue_carrier_flow(
    session: AsyncSession,
    bus,
    call: Call,
    event,
    *,
    now: datetime | None = None,  # noqa: ANN001
) -> list[voice.VoiceCommand] | None:
    """dtmf_received handler. Returns None when this call has no active flow (or isn't
    awaiting a digit) - the caller (webhooks.py) leaves `commands` untouched in that case,
    identical to pre-P12 behaviour."""
    flow_state = (call.extra or {}).get("flow")
    if not flow_state or not flow_state.get("flow_id"):
        return None
    flow = await session.get(CallFlow, uuid.UUID(flow_state["flow_id"]))
    if flow is None:
        return None

    if flow_state.get("awaiting") == "queue_wait":
        return await _continue_queue_wait(session, bus, call, flow, flow_state, event, now=now)

    if flow_state.get("awaiting") != "digit":
        return None

    digit = (event.digits or "")[:1]
    ev = {"kind": "digit", "digit": digit} if digit else {"kind": "timeout"}
    try:
        result = fe.step(flow.definition, flow_state["state"], ev)
    except fe.FlowError:
        log.exception("flow_step_failed", flow_id=str(flow.id), call_id=str(call.id))
        return await _fallback(session, bus, call, flow)
    return await _drive(session, bus, call, flow, result, now=now)


async def _drive(
    session: AsyncSession,
    bus,  # noqa: ANN001
    call: Call,
    flow: CallFlow,
    result: fe.StepResult,
    *,
    now: datetime | None = None,
) -> list[voice.VoiceCommand]:
    moment = now or _now()
    commands: list[voice.VoiceCommand] = []
    iterations = 0

    while True:
        iterations += 1
        if iterations > _MAX_DRIVE_ITERATIONS:
            log.error(
                "flow_drive_loop_exceeded",
                flow_id=str(flow.id),
                call_id=str(call.id),
                iterations=iterations,
            )
            return await _fallback(session, bus, call, flow)
        looped = False
        for action in result.actions:
            if isinstance(action, fe.Speak):
                commands.append(voice.Speak(text=action.text))
            elif isinstance(action, fe.GatherDigit):
                commands.append(
                    voice.Gather(
                        max_digits=action.max_digits,
                        timeout_seconds=action.timeout_seconds,
                        action_tag="flow_digit",
                    )
                )
            elif isinstance(action, fe.EvaluateHours):
                bh = await session.get(BusinessHours, uuid.UUID(action.business_hours_id))
                hours_result = flows_svc.evaluate_hours(bh, moment) if bh is not None else "closed"
                result = fe.step(
                    flow.definition, result.state, {"kind": "hours", "result": hours_result}
                )
                looped = True
                break
            elif isinstance(action, fe.RingGroup):
                rg = await session.get(RingGroupDef, uuid.UUID(action.ring_group_id))
                commands.append(voice.Speak(text="Please hold while we try to connect you."))
                wait_seconds = min(
                    rg.ring_timeout_seconds if rg is not None else DEFAULT_RING_TIMEOUT_SECONDS,
                    CARRIER_HOLD_CAP_SECONDS,
                )
                commands.append(voice.Pause(seconds=wait_seconds))
                result = fe.step(
                    flow.definition, result.state, {"kind": "ring_result", "result": "no_answer"}
                )
                looped = True
                break
            elif isinstance(action, fe.Enqueue):
                commands.extend(await _carrier_enqueue(session, call, flow, result, action))
                return commands
            elif isinstance(action, fe.RecordVoicemail):
                commands.append(voice.Speak(text=action.greeting))
                commands.append(voice.StartRecording())
            elif isinstance(action, fe.TransferTo):
                # Item 11: blind transfer to a real E.164 number - the one DR-2 node type
                # the neutral command set expresses directly. New-leg semantics on answer
                # are P5's problem; this just emits the command.
                commands.append(voice.Transfer(to=action.to, from_=call.our_e164))
            elif isinstance(action, fe.Hangup):
                commands.append(voice.Hangup())
        if not looped:
            break

    _persist_flow_state(
        call, flow, state=result.state, awaiting=result.awaiting, terminal=result.terminal
    )
    session.add(call)
    if result.terminal == "voicemail":
        node = (flow.definition.get("nodes") or {}).get(result.state.get("node"), {})
        greeting = node.get("greeting", "Please leave a message after the tone.")
        await voicemail_svc.create_from_flow(
            session, call, flow, node_id=result.state.get("node", ""), greeting=greeting
        )
    await session.commit()
    return commands


def _queue_hold_prompt(queue: CallQueue) -> voice.Play | voice.Speak:
    if queue.hold_audio_url:
        return voice.Play(url=queue.hold_audio_url)
    return voice.Speak(
        text=(
            f"Please hold, you're in the queue. Press {CALLBACK_DIGIT} at any time to "
            "request a callback instead."
        )
    )


async def _rerender_from_state(
    session: AsyncSession, call: Call, flow: CallFlow, flow_state: dict
) -> list[voice.VoiceCommand]:
    """B5: re-derive the SAME commands a redelivered call_initiated must produce, purely
    from what is already persisted in ``calls.extra['flow']`` - no engine re-entry, no new
    DB rows, no new outbox events. Read-only (never commits)."""
    awaiting = flow_state.get("awaiting")
    terminal = flow_state.get("terminal")
    state = flow_state.get("state") or {}
    node_id = state.get("node")
    raw_node = (flow.definition.get("nodes") or {}).get(node_id) if node_id else None
    node = raw_node if isinstance(raw_node, dict) else {}

    if awaiting == "digit":
        return [
            voice.Speak(text=node.get("prompt", "")),
            voice.Gather(max_digits=1, timeout_seconds=10, action_tag="flow_digit"),
        ]

    if awaiting == "queue_wait":
        extra = flow_state.get("extra") or {}
        queue = (
            await session.get(CallQueue, uuid.UUID(extra["queue_id"]))
            if extra.get("queue_id")
            else None
        )
        wait_seconds = (
            min(queue.max_wait_seconds, CARRIER_HOLD_CAP_SECONDS)
            if queue is not None
            else CARRIER_HOLD_CAP_SECONDS
        )
        hold_prompt = (
            _queue_hold_prompt(queue)
            if queue is not None
            else voice.Speak(text="Please hold, you're in the queue.")
        )
        return [
            voice.Gather(
                max_digits=1,
                timeout_seconds=wait_seconds,
                prompt=hold_prompt,
                action_tag="flow_queue_wait",
            )
        ]

    if terminal == "voicemail":
        greeting = node.get("greeting", "Please leave a message after the tone.")
        return [voice.Speak(text=greeting), voice.StartRecording()]

    if terminal == "transferred":
        return [voice.Transfer(to=node.get("to", ""), from_=call.our_e164)]

    if terminal == "callback_requested":
        return [
            voice.Speak(text="We'll call you back as soon as possible. Goodbye."),
            voice.Hangup(),
        ]

    # "hangup", or anything unexpected - the universal safe re-render for a redelivery.
    return [voice.Hangup()]


async def _carrier_enqueue(
    session: AsyncSession, call: Call, flow: CallFlow, result: fe.StepResult, action: fe.Enqueue
) -> list[voice.VoiceCommand]:
    queue = await session.get(CallQueue, uuid.UUID(action.queue_id))
    if queue is None:  # pragma: no cover - DR-4 validation gate prevents this at save time
        return await _fallback(session, None, call, flow)

    entry = QueueEntry(
        id=uuid.uuid4(),
        org_id=call.org_id,
        queue_id=queue.id,
        call_id=call.id,
        state="waiting",
        enqueued_at=_now(),
    )
    session.add(entry)
    await session.flush()

    hold_prompt = _queue_hold_prompt(queue)
    wait_seconds = min(queue.max_wait_seconds, CARRIER_HOLD_CAP_SECONDS)
    commands: list[voice.VoiceCommand] = [
        voice.Gather(
            max_digits=1,
            timeout_seconds=wait_seconds,
            prompt=hold_prompt,
            action_tag="flow_queue_wait",
        )
    ]
    _persist_flow_state(
        call,
        flow,
        state=result.state,
        awaiting="queue_wait",
        terminal=None,
        extra={"queue_id": str(queue.id), "queue_entry_id": str(entry.id)},
    )
    session.add(call)
    await session.commit()
    return commands


async def _continue_queue_wait(
    session: AsyncSession,
    bus,
    call: Call,
    flow: CallFlow,
    flow_state: dict,
    event,  # noqa: ANN001
    *,
    now: datetime | None = None,
) -> list[voice.VoiceCommand]:
    moment = now or _now()
    extra = flow_state.get("extra") or {}
    entry = (
        await session.get(QueueEntry, uuid.UUID(extra["queue_entry_id"]))
        if extra.get("queue_entry_id")
        else None
    )
    queue = (
        await session.get(CallQueue, uuid.UUID(extra["queue_id"]))
        if extra.get("queue_id")
        else None
    )
    digit = (event.digits or "")[:1]

    if digit == CALLBACK_DIGIT and entry is not None and entry.state == "waiting":
        return await _capture_callback(session, bus, call, flow, flow_state, entry, now=moment)

    if entry is not None and queue is not None:
        elapsed = (moment - _as_utc(entry.enqueued_at)).total_seconds()
        if elapsed < queue.max_wait_seconds:
            # B10: this webhook is a Gather TIMEOUT (or some other digit) - not the queue's
            # real max_wait_seconds elapsing. Re-issue the SAME hold Gather for whatever
            # time genuinely remains (bounded the same way as the initial one) instead of
            # overflowing early. State stays "queue_wait" - nothing to persist or commit.
            remaining = queue.max_wait_seconds - elapsed
            wait_seconds = max(1, int(min(remaining, CARRIER_HOLD_CAP_SECONDS)))
            return [
                voice.Gather(
                    max_digits=1,
                    timeout_seconds=wait_seconds,
                    prompt=_queue_hold_prompt(queue),
                    action_tag="flow_queue_wait",
                )
            ]

    return await _apply_carrier_queue_overflow(
        session, bus, call, flow, flow_state, queue, entry, now=moment
    )


async def _capture_callback(
    session: AsyncSession,
    bus,
    call: Call,
    flow: CallFlow,
    flow_state: dict,
    entry: QueueEntry,  # noqa: ANN001
    *,
    now: datetime | None = None,
) -> list[voice.VoiceCommand]:
    moment = now or _now()
    entry.state = "callback_requested"
    entry.callback_e164 = call.contact_e164
    entry.resolved_at = moment
    if bus is not None:
        bus.publish(
            call.org_id,
            {
                "type": "queue.callback_requested",
                "call_id": str(call.id),
                "queue_id": str(entry.queue_id),
            },
        )
    _persist_flow_state(
        call, flow, state=flow_state["state"], awaiting=None, terminal="callback_requested"
    )
    session.add(call)
    await session.commit()
    return [voice.Speak(text="We'll call you back as soon as possible. Goodbye."), voice.Hangup()]


async def _apply_carrier_queue_overflow(
    session: AsyncSession,
    bus,  # noqa: ANN001
    call: Call,
    flow: CallFlow,
    flow_state: dict,
    queue: CallQueue | None,
    entry: QueueEntry | None,
    *,
    now: datetime | None = None,
) -> list[voice.VoiceCommand]:
    moment = now or _now()

    if queue is not None and queue.overflow == "callback":
        if entry is not None:
            entry.state = "callback_requested"
            entry.callback_e164 = call.contact_e164
            entry.resolved_at = moment
        if bus is not None:
            bus.publish(
                call.org_id,
                {
                    "type": "queue.callback_requested",
                    "call_id": str(call.id),
                    "queue_id": str(queue.id),
                },
            )
        _persist_flow_state(
            call, flow, state=flow_state["state"], awaiting=None, terminal="callback_requested"
        )
        session.add(call)
        await session.commit()
        return [
            voice.Speak(text="We'll call you back as soon as possible. Goodbye."),
            voice.Hangup(),
        ]

    if queue is not None and queue.overflow == "voicemail":
        node_id = _find_voicemail_node(flow.definition)
        if node_id is not None:
            if entry is not None:
                entry.state = "overflowed"
                entry.resolved_at = moment
            greeting = (flow.definition["nodes"][node_id] or {}).get(
                "greeting", "Please leave a message after the tone."
            )
            _persist_flow_state(
                call,
                flow,
                state={"node": node_id, "retries": 0},
                awaiting=None,
                terminal="voicemail",
            )
            session.add(call)
            await voicemail_svc.create_from_flow(
                session, call, flow, node_id=node_id, greeting=greeting
            )
            await session.commit()
            return [voice.Speak(text=greeting), voice.StartRecording()]

    if entry is not None:
        entry.state = "overflowed"
        entry.resolved_at = moment
    _persist_flow_state(call, flow, state=flow_state["state"], awaiting=None, terminal="hangup")
    session.add(call)
    await session.commit()
    return [voice.Speak(text="No one is available right now. Goodbye."), voice.Hangup()]


async def _fallback(
    session: AsyncSession, bus, call: Call, flow: CallFlow
) -> list[voice.VoiceCommand]:  # noqa: ANN001
    """DR-4: a runtime engine error falls back to the flow's voicemail node if one exists
    ANYWHERE in the flow, else a polite hangup - never dead air."""
    node_id = _find_voicemail_node(flow.definition)
    if node_id is not None:
        greeting = (flow.definition["nodes"][node_id] or {}).get(
            "greeting", "Please leave a message after the tone."
        )
        _persist_flow_state(
            call, flow, state={"node": node_id, "retries": 0}, awaiting=None, terminal="voicemail"
        )
        session.add(call)
        await voicemail_svc.create_from_flow(
            session, call, flow, node_id=node_id, greeting=greeting
        )
        await session.commit()
        return [voice.Speak(text=greeting), voice.StartRecording()]

    _persist_flow_state(call, flow, state={}, awaiting=None, terminal="hangup")
    session.add(call)
    await session.commit()
    return [
        voice.Speak(text="Sorry, we're experiencing technical difficulties. Goodbye."),
        voice.Hangup(),
    ]


# ========================================================================================
# ROOM executor / shared QueueEntry state machine (DR-5, DR-6)
# ========================================================================================
async def list_queue_entries(
    session: AsyncSession, queue_id: uuid.UUID, *, states: list[str] | None = None
) -> list[QueueEntry]:
    stmt = (
        sa.select(QueueEntry)
        .where(QueueEntry.queue_id == queue_id)
        .order_by(QueueEntry.enqueued_at)
    )
    if states:
        stmt = stmt.where(QueueEntry.state.in_(states))
    return list((await session.execute(stmt)).scalars().all())


async def queue_position(session: AsyncSession, entry: QueueEntry) -> int:
    """DR-6: derived, never stored - the count of earlier still-waiting entries."""
    count = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(QueueEntry)
            .where(
                QueueEntry.queue_id == entry.queue_id,
                QueueEntry.state == "waiting",
                QueueEntry.enqueued_at < entry.enqueued_at,
            )
        )
    ).scalar_one()
    return int(count)


async def claim_next(
    session: AsyncSession, org_id: uuid.UUID, queue_id: uuid.UUID, user_id: uuid.UUID
) -> QueueEntry | None:
    """An agent pulls the next entry (waiting, or already offered to someone else) - the
    offer goes only to the pulling agent (DR-6).

    B9: the SELECT below only picks a CANDIDATE id - claiming it is the single conditional
    UPDATE that follows, exactly like ``claim_entry``. A plain read-then-write here would
    let two concurrent callers both read the same row as claimable and both "win"."""
    candidate_id = (
        await session.execute(
            sa.select(QueueEntry.id)
            .where(QueueEntry.queue_id == queue_id, QueueEntry.state.in_(("waiting", "offered")))
            .order_by(QueueEntry.enqueued_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate_id is None:
        return None

    result = await session.execute(
        sa.update(QueueEntry)
        .where(QueueEntry.id == candidate_id, QueueEntry.state.in_(("waiting", "offered")))
        .values(state="connected", offered_user_id=user_id, resolved_at=_now())
        # We always re-SELECT with populate_existing below, so the ORM's own Python-side
        # identity-map reconciliation would be redundant work at best; disabling it also
        # sidesteps any UnevaluatableError the "evaluate" strategy could raise on a WHERE
        # clause combining the tenant filter with an IN(...).
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount == 0:
        # Lost the race to a concurrent claim between the SELECT and this UPDATE - NOT
        # "queue empty" (silently returning None here would read that way) - raise so the
        # caller knows to retry rather than believing nothing was available.
        raise ConflictError("This queue entry was claimed by someone else first")
    return await _reload_entry(session, candidate_id)


async def claim_entry(session: AsyncSession, entry_id: uuid.UUID, user_id: uuid.UUID) -> QueueEntry:
    """B9: one conditional UPDATE, not read-then-write - first-answer-wins under a real
    race, not just under a single-threaded test."""
    result = await session.execute(
        sa.update(QueueEntry)
        .where(QueueEntry.id == entry_id, QueueEntry.state.in_(("waiting", "offered")))
        .values(state="connected", offered_user_id=user_id, resolved_at=_now())
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount == 0:
        # Read-only follow-up ONLY to report why - never re-used to decide the write above.
        existing = await session.get(QueueEntry, entry_id)
        if existing is None:
            raise NotFoundError("Queue entry not found")
        raise ConflictError(f"Queue entry is already {existing.state}")
    return await _reload_entry(session, entry_id)


async def _reload_entry(session: AsyncSession, entry_id: uuid.UUID) -> QueueEntry:
    """Force a fresh read of a row this function JUST bulk-UPDATE'd - `synchronize_session
    =False` above means the ORM identity map is NOT reconciled by the update itself, so a
    plain `session.get()` could return a stale cached instance. `populate_existing`
    overwrites it with what was actually just committed."""
    return (
        await session.execute(
            sa.select(QueueEntry)
            .where(QueueEntry.id == entry_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _next_sequential_member(
    ring_group: RingGroupDef, current_user_id: uuid.UUID | None
) -> str | None:
    members = list(ring_group.member_user_ids or [])
    if not members:
        return None
    if current_user_id is None:
        return members[0]
    try:
        idx = members.index(str(current_user_id))
    except ValueError:
        return members[0]
    return members[(idx + 1) % len(members)]


async def _offer_to_ring_group(
    session: AsyncSession,
    bus,
    call: Call | None,
    queue: CallQueue,
    entry: QueueEntry,
    moment: datetime,  # noqa: ANN001
) -> None:
    ring_group = (
        await session.get(RingGroupDef, queue.ring_group_id) if queue.ring_group_id else None
    )
    ring_user_ids: list[str] = []
    offered_user_id: uuid.UUID | None = None

    if ring_group is not None and ring_group.member_user_ids:
        if ring_group.strategy == "sequential":
            nxt = _next_sequential_member(ring_group, entry.offered_user_id)
            if nxt:
                ring_user_ids = [nxt]
                offered_user_id = uuid.UUID(nxt)
        else:
            ring_user_ids = [str(m) for m in ring_group.member_user_ids]

    entry.state = "offered"
    entry.offered_user_id = offered_user_id
    entry.offered_at = moment

    if bus is not None and call is not None:
        bus.publish(
            entry.org_id,
            {
                "type": "call.ring",
                "call_id": str(call.id),
                "queue_id": str(queue.id),
                "ring_user_ids": ring_user_ids,
                # P15: the softphone fan-out gate needs this to know which number's
                # inbox the ring belongs to - without it a non-admin never sees it at
                # all (fail-closed default for a call.ring with no resolvable "to").
                "to": call.our_e164,
            },
        )


async def _apply_room_queue_overflow(
    session: AsyncSession,
    bus,
    call: Call | None,
    queue: CallQueue,
    entry: QueueEntry,
    moment: datetime,  # noqa: ANN001
) -> None:
    entry.resolved_at = moment
    if queue.overflow == "callback":
        entry.state = "callback_requested"
        entry.callback_e164 = call.contact_e164 if call is not None else None
        if bus is not None and call is not None:
            bus.publish(
                entry.org_id,
                {
                    "type": "queue.callback_requested",
                    "call_id": str(call.id),
                    "queue_id": str(queue.id),
                },
            )
    else:
        # "voicemail"/"hangup": disconnecting a LIVE room participant needs
        # voice_plane/service.py (forbidden here) - this records the terminal queue state
        # honestly; actually tearing the room down for these two is an OPEN_ISSUES gap.
        entry.state = "overflowed"


async def routing_tick(
    session: AsyncSession, bus, *, now: datetime | None = None
) -> dict[str, int]:  # noqa: ANN001
    """Sweeper-driven (services/sweeper.py). Every ORG's waiting/offered queue entries in
    one pass - offers the head of each queue, advances/re-offers a stale offer, applies
    max_wait overflow, and notices an abandoned (caller hung up) call.

    B1: commits INSIDE the loop, immediately after THIS entry's mutations and before the
    next entry's ``set_org_context`` - same pattern as ``services/dialer.py``'s
    ``_requeue_stale_dialing``. A single trailing commit would autoflush a still-pending
    mutation under the NEXT entry's (possibly different) org context, raising
    ``MissingTenantContextError`` - which the sweeper's outer try/except then silently
    swallows, dropping every remaining entry in the pass, not just the offending one.
    """
    moment = now or _now()
    counts = {"offered": 0, "abandoned": 0, "overflowed": 0, "callback_requested": 0}

    entries = (
        (
            await session.execute(
                sa.select(QueueEntry)
                .where(QueueEntry.state.in_(("waiting", "offered")))
                .order_by(QueueEntry.enqueued_at)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )

    for entry in entries:
        set_org_context(session, entry.org_id)
        call = await session.get(Call, entry.call_id)
        queue = await session.get(CallQueue, entry.queue_id)
        if queue is None:
            continue

        if call is not None and call.status in TERMINAL_CALL_STATUSES:
            entry.state = "abandoned"
            entry.resolved_at = moment
            counts["abandoned"] += 1
            await session.commit()
            continue

        elapsed = (moment - _as_utc(entry.enqueued_at)).total_seconds()
        if elapsed >= queue.max_wait_seconds:
            await _apply_room_queue_overflow(session, bus, call, queue, entry, moment)
            counts[
                "callback_requested" if entry.state == "callback_requested" else "overflowed"
            ] += 1
            await session.commit()
            continue

        # B6: only a genuine LiveKit room call can be OFFERED to a room agent - a
        # carrier-path queue entry is driven inline by the SAME webhook response that
        # enqueued it (see _carrier_enqueue / _continue_queue_wait above); offering it
        # here would ring an agent who has no way to answer a PSTN-only call. Abandon and
        # max_wait-overflow detection above still apply to every entry regardless of via.
        is_livekit = call is not None and (call.extra or {}).get("via") == "livekit"

        if entry.state == "waiting":
            if is_livekit:
                await _offer_to_ring_group(session, bus, call, queue, entry, moment)
                counts["offered"] += 1
                await session.commit()
        elif entry.state == "offered" and entry.offered_at is not None and is_livekit:
            ring_group = (
                await session.get(RingGroupDef, queue.ring_group_id)
                if queue.ring_group_id
                else None
            )
            timeout = (
                ring_group.ring_timeout_seconds
                if ring_group is not None
                else DEFAULT_RING_TIMEOUT_SECONDS
            )
            offered_elapsed = (moment - _as_utc(entry.offered_at)).total_seconds()
            if offered_elapsed >= timeout:
                await _offer_to_ring_group(session, bus, call, queue, entry, moment)
                counts["offered"] += 1
                await session.commit()

    return counts
