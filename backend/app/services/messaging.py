"""Messaging service: state machine, send path, webhook ingestion.

The two things that make this module non-obvious, both forced by Bandwidth's real
behaviour (docs/research/bandwidth.md):

  1. **Retries are unordered and parallel.** `delivered` WILL arrive before `sending`.
     The state machine is therefore monotonic-by-rank, and events are ledgered even when
     they do not transition — so the audit trail stays complete while status never regresses.
  2. **There is no event id.** Idempotency is a DB unique constraint on
     (carrier, provider_message_id, event_type), enforced by INSERTing first and treating
     IntegrityError as "already seen". Only a constraint is safe under parallel retries.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import gate
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ComplianceBlockedError, ValidationFailedError
from app.models.messaging import (
    EVENT_TO_STATUS,
    STATUS_RANK,
    TERMINAL_STATUSES,
    Message,
    MessageEvent,
    MessageThread,
    OrgNumber,
    WebhookDeadLetter,
)
from app.providers.domain import (
    CarrierEvent,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    UnknownEvent,
)
from app.providers.segments import estimate
from app.services.contacts import resolve_or_create_contact

log = structlog.get_logger("messaging")

CARRIER_DEFAULT = "bandwidth"
#: Where the active carrier is stashed for code reached from ingestion (auto-replies).
CARRIER_SESSION_KEY = "carrier"


class Outcome(enum.Enum):
    DONE = "done"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# State machine (pure)
# --------------------------------------------------------------------------------------
def apply_event(current: str, event_type: str) -> str | None:
    """Return the new status, or None if this event must not change status.

    Rules (phase-1-plan DR-5):
      - terminal states are immutable — the FIRST terminal wins;
      - a transition must strictly increase rank;
      - a late `sending` after `delivered` therefore returns None: the status does not
        regress, but the caller still ledgers the event.
    """
    new = EVENT_TO_STATUS.get(event_type)
    if new is None:
        return None
    if current in TERMINAL_STATUSES:
        return None
    if STATUS_RANK[new] <= STATUS_RANK.get(current, 0):
        return None
    return new


def is_conflicting_terminal(current: str, event_type: str) -> bool:
    """True when a terminal event arrives for an already-terminal message with a different
    outcome — worth a WARNING, never an overwrite."""
    new = EVENT_TO_STATUS.get(event_type)
    if new is None:
        return False
    return current in TERMINAL_STATUSES and new in TERMINAL_STATUSES and new != current


# --------------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------------
async def upsert_thread(
    session: AsyncSession, org_id: uuid.UUID, our_e164: str, contact_e164: str
) -> MessageThread:
    """Insert-or-select, race-safe on both dialects."""
    stmt = sa.select(MessageThread).where(
        MessageThread.our_e164 == our_e164, MessageThread.contact_e164 == contact_e164
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=our_e164, contact_e164=contact_e164
    )
    session.add(thread)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        set_org_context(session, org_id)
        return (await session.execute(stmt)).scalar_one()
    return thread


# --------------------------------------------------------------------------------------
# Send path
# --------------------------------------------------------------------------------------
async def send_message(
    session: AsyncSession,
    org_id: uuid.UUID,
    carrier,  # noqa: ANN001 - MessagingCarrier protocol
    *,
    to_e164: str,
    from_e164: str,
    body: str,
    exemption: str | None = None,
) -> Message:
    """Create and dispatch one outbound message.

    Order matters: the message row is COMMITTED before the carrier is called, so a DLR that
    races back in beats nothing — it finds the row, or (if it truly beats the commit) is
    told to retry.
    """
    # Pass `exemption` ONLY when set. The seam's contract (P1/P2) is that a plain
    # three-argument stand-in for check_outbound keeps working - test spies and any future
    # wrapper are written that way - so a normal send must not force a keyword onto them.
    gate_kwargs = {"exemption": exemption} if exemption else {}
    verdict = await gate.check_outbound(
        session,
        org_id,
        gate.OutboundDraft(to_e164=to_e164, from_e164=from_e164, body=body),
        **gate_kwargs,
    )
    deferred_until = getattr(verdict, "defer_until", None)
    if not verdict.allowed and deferred_until is None:
        raise ComplianceBlockedError(verdict.reason or "Blocked by compliance policy")

    est = estimate(body)
    thread = await upsert_thread(session, org_id, from_e164, to_e164)
    thread.last_message_at = _now()
    if thread.contact_id is None:
        thread.contact_id = (await resolve_or_create_contact(session, org_id, to_e164)).id

    message = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="outbound",
        status="queued",
        from_e164=from_e164,
        to_e164=to_e164,
        body=body,
        media=[],
        carrier=getattr(carrier, "name", CARRIER_DEFAULT),
        segment_count_est=est.segments,
    )
    if deferred_until is not None:
        # QUIET HOURS: defer, do not drop. The row exists and is queued; the sweeper
        # releases it and RE-RUNS THE FULL GATE, so an opt-out arriving during the hold
        # still kills the send. Gate at dispatch, never only at enqueue.
        message.hold_until = deferred_until
        session.add(message)
        await session.commit()
        return message

    session.add(message)
    await session.commit()

    return await _dispatch_to_carrier(session, org_id, carrier, message)


async def _dispatch_to_carrier(
    session: AsyncSession,
    org_id: uuid.UUID,
    carrier,  # noqa: ANN001
    message: Message,
) -> Message:
    """Hand one persisted message to the carrier and record the outcome.

    Factored out so release_held_messages shares the accepted/rejected handling verbatim -
    a held message must behave exactly like an immediate one once it is released.
    """
    result = await carrier.send_message(
        OutboundMessage(
            to=message.to_e164,
            from_=message.from_e164,
            text=message.body or "",
            tag=str(message.id),
        )
    )

    set_org_context(session, org_id)
    message = await session.get(Message, message.id)
    if result.status == "accepted":
        message.status = "accepted"
        message.provider_message_id = result.provider_message_id
        message.hold_until = None
    else:
        # Carrier rejection is DATA, not an HTTP error (DR-7). The client reads one uniform
        # resource whether the carrier accepted, refused, or was unreachable.
        message.status = "rejected"
        message.hold_until = None
        if result.error:
            message.error_code = (result.error.carrier_code or result.error.category)[:32]
            message.error_detail = result.error.detail[:255] or None
    await session.commit()
    return message


async def release_held_messages(
    session: AsyncSession,
    carrier,  # noqa: ANN001
    now: datetime | None = None,
) -> int:
    """Release messages whose quiet-hours hold has expired.

    Re-runs the FULL gate before dispatching. That is the contract P11's scheduler
    inherits: an opt-out that lands while a message is held must still stop it.
    """
    moment = now or _now()
    bind_moment = moment.replace(tzinfo=None) if _is_sqlite(session) else moment
    stmt = (
        sa.select(Message)
        .where(
            Message.status == "queued",
            Message.hold_until.is_not(None),
            Message.hold_until <= bind_moment,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    held = list((await session.execute(stmt)).scalars().all())

    released = 0
    for message in held:
        org_id = message.org_id
        set_org_context(session, org_id)
        verdict = await gate.check_outbound(
            session,
            org_id,
            gate.OutboundDraft(
                to_e164=message.to_e164,
                from_e164=message.from_e164,
                body=message.body or "",
            ),
        )
        defer_until = getattr(verdict, "defer_until", None)
        if not verdict.allowed and defer_until is None:
            message.status = "rejected"
            message.hold_until = None
            message.error_code = f"{verdict.reason or 'blocked'}_while_held"[:32]
            await session.commit()
            continue
        if defer_until is not None:
            # Released on a boundary that is still quiet somewhere. Keep waiting.
            message.hold_until = defer_until
            await session.commit()
            continue

        await _dispatch_to_carrier(session, org_id, carrier, message)
        released += 1
    return released


def _is_sqlite(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "sqlite"


async def resolve_from_number(
    session: AsyncSession, org_id: uuid.UUID, requested: str | None
) -> str:
    """REPLACED IN P2 by ``services.sender.select_sender`` (sticky-sender contract, DR-4).

    Kept as a loud shim rather than deleted so that any caller missed during the refactor
    fails immediately and visibly instead of silently picking a number.
    """
    raise ValidationFailedError(
        "resolve_from_number was replaced by select_sender in P2; call that instead"
    )



# --------------------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------------------
async def dead_letter(session: AsyncSession, carrier: str, reason: str, payload: str) -> None:
    session.add(
        WebhookDeadLetter(id=uuid.uuid4(), carrier=carrier, reason=reason, payload=payload[:65535])
    )
    await session.commit()


async def ingest_event(
    session: AsyncSession,
    carrier_name: str,
    event: CarrierEvent,
    raw_body: str,
    carrier=None,  # noqa: ANN001 - needed only so inbound keywords can auto-reply
) -> Outcome:
    """Process ONE event in its own transaction. One bad event must not poison a batch."""
    if isinstance(event, UnknownEvent):
        await dead_letter(session, carrier_name, "unknown_event_type", raw_body)
        return Outcome.DEAD_LETTER
    if isinstance(event, InboundMessage):
        return await _ingest_inbound(session, carrier_name, event, raw_body, carrier)
    if isinstance(event, DeliveryReceipt):
        return await _ingest_dlr(session, carrier_name, event)
    await dead_letter(session, carrier_name, "unhandled_event", raw_body)
    return Outcome.DEAD_LETTER


async def _ingest_inbound(
    session: AsyncSession,
    carrier_name: str,
    event: InboundMessage,
    raw_body: str,
    carrier=None,  # noqa: ANN001
) -> Outcome:
    # JUSTIFIED allow_unscoped: pre-tenant-resolution. An inbound webhook carries no org;
    # this lookup IS what resolves it, and it is constrained to one exact number.
    stmt = (
        sa.select(OrgNumber)
        .where(OrgNumber.e164 == event.our_number)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    org_number = (await session.execute(stmt)).scalar_one_or_none()
    if org_number is None:
        await dead_letter(session, carrier_name, "unknown_number", raw_body)
        return Outcome.DEAD_LETTER

    org_id = org_number.org_id
    set_org_context(session, org_id)

    thread = await upsert_thread(session, org_id, event.our_number, event.from_)
    thread.last_message_at = event.event_time or _now()
    # Reopen-on-inbound, set inside the SAME transaction as the deduped insert - so a
    # replayed webhook cannot observably re-reopen anything: the IntegrityError path
    # rolls this back too.
    thread.status = "open"
    if thread.contact_id is None:
        thread.contact_id = (
            await resolve_or_create_contact(session, org_id, event.from_)
        ).id

    message = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="inbound",
        status="received",
        from_e164=event.from_,
        to_e164=event.our_number,
        body=event.text,
        media=list(event.media),
        carrier=carrier_name,
        provider_message_id=event.provider_message_id,
        segment_count_carrier=event.segment_count,
    )
    session.add(message)
    session.add(
        MessageEvent(
            id=uuid.uuid4(),
            org_id=org_id,
            message_id=message.id,
            carrier=carrier_name,
            provider_message_id=event.provider_message_id,
            event_type="message-received",
            payload=event.raw,
            event_time=event.event_time,
            processed_at=_now(),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Duplicate inbound (the carrier replayed it). Both the messages unique index and
        # the events dedupe constraint can fire here; either way it is a no-op.
        await session.rollback()
        return Outcome.DONE

    # The carrier travels on the session, NOT in the signature. on_inbound's three-argument
    # shape is pinned by the P1/P2 seam tests, and those spies must keep working unmodified
    # - that is the evidence the seam held. session.info is already how org context flows.
    if carrier is not None:
        session.info[CARRIER_SESSION_KEY] = carrier
    await gate.on_inbound(session, org_id, message.id)
    await session.commit()
    return Outcome.DONE


async def _ingest_dlr(
    session: AsyncSession, carrier_name: str, event: DeliveryReceipt
) -> Outcome:
    # JUSTIFIED allow_unscoped: a DLR carries no org; this lookup resolves it, constrained
    # to one exact (carrier, provider_message_id) pair.
    stmt = (
        sa.select(Message)
        .where(
            Message.carrier == carrier_name,
            Message.provider_message_id == event.provider_message_id,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    message = (await session.execute(stmt)).scalar_one_or_none()
    if message is None:
        # THE SEND RACE. The webhook can beat our own commit. Answer 500 on purpose so
        # Bandwidth's 24h retry re-delivers once our transaction has landed. A genuinely
        # alien id stops costing anything after 24h.
        log.info("dlr_unknown_message_retry", provider_message_id=event.provider_message_id)
        return Outcome.RETRY

    org_id = message.org_id
    set_org_context(session, org_id)

    session.add(
        MessageEvent(
            id=uuid.uuid4(),
            org_id=org_id,
            message_id=message.id,
            carrier=carrier_name,
            provider_message_id=event.provider_message_id,
            event_type=event.event_type,
            payload=event.raw,
            event_time=event.event_time,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return Outcome.DONE  # already ingested, possibly by a parallel retry

    _apply_dlr_to_message(message, event)
    await session.execute(
        sa.update(MessageEvent)
        .where(
            MessageEvent.carrier == carrier_name,
            MessageEvent.provider_message_id == event.provider_message_id,
            MessageEvent.event_type == event.event_type,
        )
        .values(processed_at=_now())
    )
    await session.commit()
    return Outcome.DONE


def _apply_dlr_to_message(message: Message, event: DeliveryReceipt) -> None:
    if is_conflicting_terminal(message.status, event.event_type):
        log.warning(
            "conflicting_terminal_event_ignored",
            current=message.status,
            incoming=event.event_type,
            provider_message_id=event.provider_message_id,
        )

    new_status = apply_event(message.status, event.event_type)
    if new_status is not None:
        message.status = new_status
        if new_status == "failed":
            message.error_code = (event.error_code or "unknown")[:32]
            message.error_detail = (event.error_description or "")[:255] or None

    if event.segment_count is not None:
        # The carrier's count is truth; ours was only an estimate.
        if (
            message.segment_count_est is not None
            and message.segment_count_est != event.segment_count
        ):
            log.debug(
                "segment_estimate_mismatch",
                estimated=message.segment_count_est,
                carrier=event.segment_count,
            )
        message.segment_count_carrier = event.segment_count


async def reprocess_pending(session: AsyncSession) -> int:
    """Re-drive every event whose processing did not complete.

    Unscheduled in P1 by design — P3+ runs it behind Redis. Its existence and its test are
    the seam that keeps ingestion a 2xx-fast, DB-only path.
    """
    stmt = (
        sa.select(MessageEvent)
        .where(MessageEvent.processed_at.is_(None))
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    pending = list((await session.execute(stmt)).scalars().all())
    count = 0
    for row in pending:
        msg_stmt = (
            sa.select(Message)
            .where(Message.id == row.message_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
        message = (await session.execute(msg_stmt)).scalar_one_or_none()
        if message is None:
            continue
        set_org_context(session, message.org_id)
        _apply_dlr_to_message(
            message,
            DeliveryReceipt(
                provider_message_id=row.provider_message_id,
                event_type=row.event_type,
                error_code=(row.payload or {}).get("errorCode"),
                segment_count=((row.payload or {}).get("message") or {}).get("segmentCount"),
                event_time=row.event_time,
                raw=row.payload or {},
            ),
        )
        row.processed_at = _now()
        row.processing_error = None
        count += 1
    await session.commit()
    return count
