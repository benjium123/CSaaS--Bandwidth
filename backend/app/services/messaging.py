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
from app.models.compliance import MediaAsset
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
from app.services.outbox import record_platform_event

log = structlog.get_logger("messaging")

CARRIER_DEFAULT = "bandwidth"
#: Where the active carrier is stashed for code reached from ingestion (auto-replies).
CARRIER_SESSION_KEY = "carrier"
#: P10 DR-4/DR-5: set on the session by the SMS agent around its own send_message call.
#: send_message reads this to know a send is AI-originated (never adding an exemption -
#: the gate still runs unchanged) and the human-takeover check below reads it to tell an
#: AI reply apart from a human operator typing in the same thread.
AI_SEND_KEY = "ai_originated_send"
#: P11 DR-3: set on the session by the outbound campaign scheduler around its own
#: send_message call. A bulk campaign send is not a human takeover, so it must not flip
#: an active AI thread to handed_off — same shape as AI_SEND_KEY, gate still unchanged.
BULK_SEND_KEY = "bulk_originated_send"


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
    media_ids: list[uuid.UUID] | None = None,
    media_urls: list[str] | None = None,
    registry=None,  # noqa: ANN001 - CarrierRegistry; enables failover when given
    plan=None,  # noqa: ANN001 - routing.RoutePlan
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

    assets: list[MediaAsset] = []
    if media_ids:
        if len(media_ids) > 10:
            raise ValidationFailedError("At most 10 media attachments per message")
        assets = list(
            (
                await session.execute(
                    sa.select(MediaAsset).where(
                        MediaAsset.id.in_(media_ids), MediaAsset.status == "stored"
                    )
                )
            ).scalars().all()
        )
        if len(assets) != len(set(media_ids)):
            raise ValidationFailedError("One or more media attachments were not found")

    est = estimate(body)
    thread = await upsert_thread(session, org_id, from_e164, to_e164)
    thread.last_message_at = _now()
    if thread.contact_id is None:
        thread.contact_id = (await resolve_or_create_contact(session, org_id, to_e164)).id
    if (
        thread.ai_state == "active"
        and exemption is None
        and not session.info.get(AI_SEND_KEY)
        and not session.info.get(BULK_SEND_KEY)
    ):
        # P10 DR-5: a human operator sending in an active thread is an implicit takeover -
        # the bot must go silent without a second click. An AI-originated send (flagged via
        # AI_SEND_KEY) must never trip this on itself - and neither must a compliance
        # AUTO-REPLY (HELP/START confirmations pass `exemption`): that is the keyword
        # engine answering on the gate's own behalf, not an operator taking the thread over.
        # P11 DR-3: a bulk campaign send (BULK_SEND_KEY) is not a takeover either.
        thread.ai_state = "handed_off"

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

    # Both present = phase-3b routing with failover. Absent = the P1 single-carrier path,
    # unchanged, which is what the seam tests exercise.
    if registry is not None and plan is not None:
        return await dispatch_with_failover(
            session, org_id, registry, plan, message, media_urls or []
        )
    return await _dispatch_to_carrier(session, org_id, carrier, message, media_urls or [])


async def dispatch_with_failover(
    session: AsyncSession,
    org_id: uuid.UUID,
    registry,  # noqa: ANN001 - CarrierRegistry
    plan,  # noqa: ANN001 - routing.RoutePlan
    message: Message,
    media_urls: list[str] | None = None,
) -> Message:
    """Try each route in turn, stopping at the first acceptance.

    Only a RETRYABLE rejection moves to the next route. An invalid request or an
    unregistered number will be rejected identically by every carrier, so retrying it
    elsewhere just multiplies the damage across brands instead of surfacing the bug.

    The route that actually sent is recorded on the message, because "which number did this
    go out on" is the first question asked about any surprising delivery.
    """
    routes = plan.all_routes()
    last: Message = message
    for index, route in enumerate(routes):
        carrier = registry.get(route.carrier_name)
        if carrier is None:
            continue
        breaker = registry.health.breaker(route.carrier_name)
        if not breaker.allows_send() and index < len(routes) - 1:
            # Skip an open breaker unless this is the last thing we could try; refusing to
            # send at all is worse than one probe against a carrier we think is unwell.
            continue

        set_org_context(session, org_id)
        last = await session.get(Message, message.id)
        if last.from_e164 != route.from_e164 or last.carrier != route.carrier_name:
            log.info(
                "route_switched",
                message_id=str(message.id),
                to_carrier=route.carrier_name,
                to_number=route.from_e164,
                reason=route.reason,
            )
            last.from_e164 = route.from_e164
            last.carrier = route.carrier_name
            await session.commit()

        last = await _dispatch_to_carrier(session, org_id, carrier, last, media_urls)
        if last.status == "accepted":
            breaker.record_success()
            return last

        # The REAL error object, carried out of dispatch rather than reconstructed from
        # the persisted columns - a rebuilt error loses `retryable`, and guessing it wrong
        # either loops a permanent failure across every carrier or gives up on a blip.
        error = getattr(last, "last_carrier_error", None)
        breaker.record_failure(error)
        if error is None or not error.retryable:
            return last

    return last


async def _dispatch_to_carrier(
    session: AsyncSession,
    org_id: uuid.UUID,
    carrier,  # noqa: ANN001
    message: Message,
    media_urls: list[str] | None = None,
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
            media=tuple(media_urls or message.media or []),
        )
    )

    set_org_context(session, org_id)
    message = await session.get(Message, message.id)
    # Transient, not a mapped column: the failover loop needs the full error (its
    # `retryable` flag above all), and the DB only keeps a code and a string.
    message.last_carrier_error = result.error
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
    # The ENTIRE persist sequence is inside one try: the duplicate collision can surface
    # at the parent flush or at the commit depending on which constraint fires first, and
    # both must be handled as a dedupe rather than escaping as a 500.
    try:
        session.add(message)
        # Flush the parent BEFORE its children. A mixed flush is not guaranteed to order
        # messages ahead of media_assets, and with foreign keys enforced that violation
        # surfaced as an IntegrityError the dedupe handler mistook for a duplicate -
        # silently dropping a real inbound MMS behind a 200 OK.
        await session.flush()

        # One pending asset per inbound media URL. Created here so a replayed webhook
        # rolls them back with everything else - but NOT fetched here: the webhook path
        # stays DB-only and 2xx-fast. The pending rows ARE the queue.
        for url in event.media:
            session.add(
                MediaAsset(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    message_id=message.id,
                    direction="inbound",
                    source_url=url,
                    status="pending",
                )
            )
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
        # P13 DR-4: durable outbox row commits (or rolls back on the dedupe path) with
        # the message itself - the webhook deliverer fans it out later.
        record_platform_event(
            session,
            org_id,
            "message.received",
            {
                "message_id": str(message.id),
                "thread_id": str(thread.id),
                "from": event.from_,
                "to": event.our_number,
                "body": event.text,
            },
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        set_org_context(session, org_id)
        # Only a REAL duplicate counts as done. Confirm the row we would have written
        # already exists; anything else is a genuine failure and must not be reported as
        # success, or we would silently lose inbound messages behind a 200 OK.
        existing = (
            await session.execute(
                sa.select(Message)
                .where(
                    Message.carrier == carrier_name,
                    Message.provider_message_id == event.provider_message_id,
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if existing is not None:
            return Outcome.DONE
        log.exception(
            "inbound_persist_failed",
            provider_message_id=event.provider_message_id,
            carrier=carrier_name,
        )
        # RETRY: the carrier redelivers, and we get another chance rather than dropping it.
        return Outcome.RETRY

    # The carrier travels on the session, NOT in the signature. on_inbound's three-argument
    # shape is pinned by the P1/P2 seam tests, and those spies must keep working unmodified
    # - that is the evidence the seam held. session.info is already how org context flows.
    if carrier is not None:
        session.info[CARRIER_SESSION_KEY] = carrier
    await gate.on_inbound(session, org_id, message.id)
    await session.commit()

    # P10 DR-2: post-commit, fire-and-forget. Imported locally to avoid a module cycle -
    # sms_agent imports this module at its own top level for send_message/AI_SEND_KEY, so
    # this module must not import sms_agent until AFTER it is fully loaded.
    from app.services.sms_agent import org_could_reply, spawn_from_ingest

    # A same-session, no-new-connection probe: only spawn the background turn (a SECOND,
    # genuinely concurrent database session) when this org could conceivably act on it.
    # For every org with no sms_enabled profile - the overwhelming majority - this is the
    # end of the story, with zero extra sessions ever opened.
    if await org_could_reply(session, org_id):
        # webhooks.py stashes the app's real bus/settings on the session (the only two
        # lines it is allowed to add) precisely so a handoff published from THIS
        # background path reaches real websocket subscribers instead of a disconnected
        # fallback bus.
        spawn_from_ingest(
            inbound_message_id=message.id,
            carrier=carrier,
            bus=session.info.get("event_bus"),
            settings=session.info.get("settings"),
        )
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

    prior_status = message.status
    _apply_dlr_to_message(message, event)
    if message.status != prior_status and message.status in ("delivered", "failed"):
        # P13 DR-4: the message reached its carrier-terminal outcome in THIS transaction.
        record_platform_event(
            session,
            org_id,
            "message.finalized",
            {
                "message_id": str(message.id),
                "thread_id": str(message.thread_id),
                "status": message.status,
                "to": message.to_e164,
                "from": message.from_e164,
                "error_code": message.error_code,
            },
        )
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
