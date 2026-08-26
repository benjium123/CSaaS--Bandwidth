"""Voice call lifecycle: outbound dispatch, inbound creation, webhook event application,
blind transfer.

The load-bearing decision (phase-5-plan): **a call is N legs**. Both carriers fire
callbacks per LEG (their "call id" is a leg id, not ours), and a transfer creates a new leg
while the old one dies without being mutated. So ``Call.status`` is never set directly from
a webhook - it is DERIVED from the legs every time one of them changes, using the same
monotonic-by-rank shape as messages, media and registration (D6: carriers retry unordered,
and a late webhook must never walk a fact backwards).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import (
    CarrierNotConfiguredError,
    ConflictError,
    FeatureUnavailableError,
    ValidationFailedError,
)
from app.models.voice import (
    CALL_STATUS_RANK,
    LEG_STATUS_RANK,
    TERMINAL_CALL_STATUSES,
    TERMINAL_LEG_STATUSES,
    Call,
    CallLeg,
)
from app.models.voice import (
    VoiceEvent as VoiceEventRow,
)
from app.providers.voice import Gather as GatherCommand
from app.providers.voice import Hangup as HangupCommand
from app.providers.voice import Speak as SpeakCommand
from app.providers.voice import Transfer as TransferCommand
from app.providers.voice import VoiceEvent, as_voice_carrier
from app.services import recordings as recordings_svc

log = structlog.get_logger("calls")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """Interpret a datetime as UTC.

    SQLite hands back NAIVE datetimes for DateTime(timezone=True) columns while Postgres
    hands back aware ones (same trap documented in services/inbox.py::_as_utc). Everything
    this module writes is already UTC, so a naive value read back is stamped, not converted.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------------------
# F8: hangup-cause -> terminal flavor mapping. Kept IN THE SERVICE (not the adapters) since
# it is carrier-neutral vocabulary both adapters already normalize onto `hangup_cause`.
# --------------------------------------------------------------------------------------
#: LEG_STATUS_RANK has no "busy"/"canceled" flavor of its own - a leg terminates as either
#: "hungup" (normal) or "failed" (everything else). The FLAVOR the user sees (busy vs.
#: canceled vs. plain failed) is a CALL-level derivation read back off hangup_cause in
#: derive_call_status, once every leg is terminal.
_BUSY_HANGUP_CAUSES = frozenset({"busy", "user_busy"})
_CANCEL_HANGUP_CAUSES = frozenset({"cancel", "originator_cancel"})
_REJECTED_HANGUP_CAUSES = frozenset({"rejected", "call_rejected"})


def _leg_terminal_status_for_cause(cause: str) -> str:
    """Busy/rejected/cancel causes all still land the leg at "failed"; anything else
    (normal-clearing and the like) is an ordinary "hungup"."""
    if (
        cause in _BUSY_HANGUP_CAUSES
        or cause in _CANCEL_HANGUP_CAUSES
        or cause in _REJECTED_HANGUP_CAUSES
    ):
        return "failed"
    return "hungup"


# --------------------------------------------------------------------------------------
# State machine (pure) - same monotonic-rank shape as services/registration.py.
# --------------------------------------------------------------------------------------
def advance_leg(leg: CallLeg, new_status: str) -> bool:
    """Move a leg forward, never backward. Returns whether anything changed.

    Two guards: rank never decreases, and a terminal leg is never replaced by a different
    terminal - a stale `call_answered` arriving after `call_hungup` must not resurrect it.
    """
    if new_status not in LEG_STATUS_RANK:
        raise ValidationFailedError(f"Unknown call leg status {new_status!r}")

    # A column default only applies at INSERT; an unflushed leg has status=None (same trap
    # as services/registration.py's `current = entity.status or "draft"`).
    current = leg.status or "created"
    if current == new_status:
        return False

    if current in TERMINAL_LEG_STATUSES:
        log.warning(
            "leg_terminal_status_ignored",
            leg_id=str(leg.id),
            current=current,
            attempted=new_status,
        )
        return False

    if LEG_STATUS_RANK[new_status] < LEG_STATUS_RANK[current]:
        log.warning(
            "leg_status_regression_ignored",
            leg_id=str(leg.id),
            current=current,
            attempted=new_status,
        )
        return False

    leg.status = new_status
    if new_status == "answered" and leg.answered_at is None:
        leg.answered_at = _now()
    if new_status in TERMINAL_LEG_STATUSES:
        leg.ended_at = _now()
    return True


def _advance_call(call: Call, new_status: str) -> bool:
    if new_status not in CALL_STATUS_RANK:
        raise ValidationFailedError(f"Unknown call status {new_status!r}")

    current = call.status or "queued"
    if current == new_status:
        return False
    if current in TERMINAL_CALL_STATUSES:
        return False
    if CALL_STATUS_RANK[new_status] < CALL_STATUS_RANK[current]:
        return False

    call.status = new_status
    if new_status in TERMINAL_CALL_STATUSES:
        call.ended_at = _now()
        call.duration_seconds = (
            int((_as_utc(call.ended_at) - _as_utc(call.answered_at)).total_seconds())
            if call.answered_at is not None
            else None
        )
    return True


#: Pre-answer call status, keyed by the most-advanced leg's rank. Answered/terminal are
#: handled separately in derive_call_status because they depend on ALL legs, not just one.
_PRE_ANSWER_CALL_STATUS = {0: "queued", 10: "initiated", 20: "ringing"}


def derive_call_status(call: Call, legs: list[CallLeg]) -> bool:
    """Recompute ``call.status`` / ``answered_at`` / ``ended_at`` / ``duration_seconds``
    from its legs. Returns whether anything changed.

    Answered when ANY leg answers (call.answered_at is set from the FIRST leg to answer,
    once, and never overwritten). Terminal only when EVERY leg is terminal - the surviving
    leg of a transfer keeps the call alive while the old leg hangs up. `bridged` is NOT
    produced here: it is a call_bridged-event-only transition (see apply_voice_event) that
    this function's monotonic guard simply never regresses.
    """
    changed = False

    if call.answered_at is None:
        # Normalize before comparing: a leg answered earlier in THIS request carries a
        # fresh aware timestamp while one loaded straight from SQLite comes back naive
        # (same trap as services/inbox.py) - min() on a mix of the two raises TypeError.
        answered_ats = [_as_utc(leg.answered_at) for leg in legs if leg.answered_at is not None]
        if answered_ats:
            call.answered_at = min(answered_ats)
            changed = True

    if not legs:
        return changed

    all_terminal = all(leg.status in TERMINAL_LEG_STATUSES for leg in legs)
    any_answered = call.answered_at is not None

    if all_terminal:
        if any_answered:
            new_status = "completed"
        else:
            # F8: read the flavor back off every terminal leg's hangup_cause. "Every leg"
            # on purpose - a mixed transfer where one leg was simply never answered and the
            # other was actually rejected should not masquerade as a clean busy/cancel.
            causes = [leg.hangup_cause or "" for leg in legs]
            if all(cause in _BUSY_HANGUP_CAUSES for cause in causes):
                new_status = "busy"
            elif all(cause in _CANCEL_HANGUP_CAUSES for cause in causes):
                new_status = "canceled"
            elif all(leg.status == "failed" for leg in legs):
                new_status = "failed"
            else:
                new_status = "no_answer"
    elif any_answered:
        new_status = "answered"
    else:
        best_rank = max(LEG_STATUS_RANK.get(leg.status, 0) for leg in legs)
        new_status = _PRE_ANSWER_CALL_STATUS.get(best_rank, "initiated")

    if _advance_call(call, new_status):
        changed = True
    return changed


def active_leg(legs: list[CallLeg]) -> CallLeg | None:
    """The leg currently carrying the conversation: the most recently created leg that is
    not yet terminal. During a transfer this is the new leg; before one, the original."""
    candidates = [leg for leg in legs if leg.status not in TERMINAL_LEG_STATUSES]
    if not candidates:
        return None
    return max(candidates, key=lambda leg: leg.created_at)


async def load_legs(session: AsyncSession, call_id: uuid.UUID) -> list[CallLeg]:
    stmt = sa.select(CallLeg).where(CallLeg.call_id == call_id).order_by(CallLeg.created_at)
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------------------
async def create_outbound_call(
    session: AsyncSession,
    registry,  # noqa: ANN001 - CarrierRegistry
    org_id: uuid.UUID,
    *,
    to: str,
    from_: str,
    carrier_name: str,
    machine_detection: str = "off",
    tag: str = "",
    record: bool = False,
) -> tuple[Call, CallLeg]:
    carrier_obj = registry.get(carrier_name) if registry is not None else None
    if carrier_obj is None:
        raise CarrierNotConfiguredError(f"Carrier {carrier_name!r} is not configured")
    voice_carrier = as_voice_carrier(carrier_obj)

    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164=to,
        our_e164=from_,
        carrier=carrier_name,
        status="queued",
        tag=tag or None,
        # F3a: the ONLY place this flag is read is the outbound-answer webhook handler
        # (webhooks.py::_outbound_answer_commands) deciding whether to StartRecording.
        extra={"record": True} if record else {},
    )
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        to_e164=to,
        from_e164=from_,
        status="created",
        reason="original",
    )
    session.add(call)
    session.add(leg)

    result = await voice_carrier.create_call(
        to=to, from_=from_, machine_detection=machine_detection, tag=tag
    )
    if result.status == "accepted":
        leg.provider_call_id = result.provider_call_id
        advance_leg(leg, "dialing")
        _advance_call(call, "initiated")
    else:
        leg.extra = {**(leg.extra or {}), "error_detail": result.error_detail}
        advance_leg(leg, "failed")
        _advance_call(call, "failed")

    await session.commit()
    return call, leg


# --------------------------------------------------------------------------------------
# Inbound
# --------------------------------------------------------------------------------------
async def create_inbound_call(
    session: AsyncSession, org_id: uuid.UUID, event: VoiceEvent, carrier_name: str
) -> tuple[Call, CallLeg]:
    """A call_initiated event for a provider_call_id we have never seen, whose `to` matched
    one of the org's numbers. Creates the Call + its first leg; the caller (the webhook
    route) still runs the event through apply_voice_event afterwards so it gets ledgered
    like every other event."""
    set_org_context(session, org_id)

    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=event.from_,
        our_e164=event.to,
        carrier=carrier_name,
        status="initiated",
        tag=event.tag or None,
    )
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id=event.provider_call_id or None,
        to_e164=event.to,
        from_e164=event.from_,
        status="dialing",
        reason="original",
    )
    session.add(call)
    session.add(leg)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent delivery of the same "initiate" beat us to the unique
        # provider_call_id. Not our row to own - fetch what already exists.
        await session.rollback()
        set_org_context(session, org_id)
        existing_leg = (
            await session.execute(
                sa.select(CallLeg).where(CallLeg.provider_call_id == event.provider_call_id)
            )
        ).scalar_one_or_none()
        if existing_leg is None:
            raise
        existing_call = await session.get(Call, existing_leg.call_id)
        return existing_call, existing_leg
    return call, leg


async def adopt_transfer_leg(
    session: AsyncSession, org_id: uuid.UUID, event: VoiceEvent
) -> CallLeg | None:
    """F1b: a blind transfer's B-leg exists in our DB (created by start_blind_transfer)
    before the carrier has assigned it a provider_call_id. The FIRST event naming that leg
    (a transfer B-leg and a race-losing outbound answer both carry OUR number in `from`,
    never `to` - see F1a/F9a in the webhook resolver) is what tells us WHICH pending
    transfer this is; adopting it here is what stops it from being mistaken for a brand
    new inbound call.

    Caller is expected to have already called set_org_context(session, org_id).
    """
    if not event.to or not event.provider_call_id:
        return None

    stmt = (
        sa.select(CallLeg)
        .join(Call, Call.id == CallLeg.call_id)
        .where(
            CallLeg.org_id == org_id,
            CallLeg.reason == "transfer",
            CallLeg.provider_call_id.is_(None),
            CallLeg.to_e164 == event.to,
            Call.status.notin_(TERMINAL_CALL_STATUSES),
        )
        .order_by(CallLeg.created_at)
        .limit(1)
    )
    candidate = (await session.execute(stmt)).scalar_one_or_none()
    if candidate is None:
        return None

    candidate.provider_call_id = event.provider_call_id
    try:
        await session.flush()
    except IntegrityError:
        # Two deliveries raced the adoption (or another leg already claimed this carrier
        # id) - not our row to own; re-select the way every other resolver here does on
        # conflict.
        await session.rollback()
        set_org_context(session, org_id)
        return (
            await session.execute(
                sa.select(CallLeg).where(CallLeg.provider_call_id == event.provider_call_id)
            )
        ).scalar_one_or_none()
    return candidate


# --------------------------------------------------------------------------------------
# Webhook event application
# --------------------------------------------------------------------------------------
async def apply_voice_event(
    session: AsyncSession,
    carrier_name: str,
    event: VoiceEvent,
    org_id: uuid.UUID,
    *,
    carrier=None,  # noqa: ANN001 - VoiceCarrier; unused since F6 moved recording fetch to the sweeper
    store=None,  # noqa: ANN001 - ObjectStore; unused since F6 moved recording fetch to the sweeper
) -> tuple[Call | None, CallLeg | None, bool]:
    """The webhook workhorse: ledger the event exactly once, then apply it.

    Dedupe is a DB constraint on (carrier, provider_event_id), never an application check -
    the same reasoning as messaging (D6): carriers redeliver, sometimes in parallel, and only
    a constraint is safe under that.
    """
    set_org_context(session, org_id)

    # F10: lock the leg (and its call) FOR UPDATE before reading them. SQLite renders
    # with_for_update() as a no-op - fine locally - but under Postgres READ COMMITTED two
    # webhooks for the same leg delivered in parallel would otherwise both read the
    # pre-update row and race applying their transitions; the lock serializes them.
    leg: CallLeg | None = None
    if event.provider_call_id:
        leg = (
            await session.execute(
                sa.select(CallLeg)
                .where(CallLeg.provider_call_id == event.provider_call_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    call: Call | None = None
    if leg is not None:
        call = (
            await session.execute(
                sa.select(Call).where(Call.id == leg.call_id).with_for_update()
            )
        ).scalar_one_or_none()

    voice_event = VoiceEventRow(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id if call is not None else None,
        leg_id=leg.id if leg is not None else None,
        carrier=carrier_name,
        provider_event_id=event.provider_event_id,
        event_type=event.event_type,
        payload=dict(event.raw or {}),
        occurred_at=event.occurred_at,
    )
    try:
        async with session.begin_nested():
            session.add(voice_event)
            await session.flush()
    except IntegrityError:
        log.info(
            "voice_event_duplicate_ignored",
            carrier=carrier_name,
            provider_event_id=event.provider_event_id,
        )
        await session.commit()
        return call, leg, False

    if leg is None or call is None:
        # Genuinely unmatched: stored and 200'd, NEVER a 404 - Bandwidth retries any
        # non-2xx for 24h, and a dial we never placed will never resolve.
        await session.commit()
        return None, None, False

    changed = await _apply_event_to_leg_and_call(
        session, carrier, store, event, call, leg
    )
    await session.commit()
    return call, leg, changed


async def _apply_event_to_leg_and_call(
    session: AsyncSession,
    carrier,  # noqa: ANN001
    store,  # noqa: ANN001
    event: VoiceEvent,
    call: Call,
    leg: CallLeg,
) -> bool:
    changed = False
    et = event.event_type

    if et == "call_initiated":
        changed = advance_leg(leg, "dialing") or changed
    elif et == "call_ringing":
        changed = advance_leg(leg, "ringing") or changed
    elif et == "call_answered":
        changed = advance_leg(leg, "answered") or changed
    elif et == "call_bridged":
        # The leg itself does not move - only the call may advance, and only forward
        # (nothing un-bridges a call short of ending it, so a stale bridged after a
        # terminal event is silently ignored by the monotonic guard below).
        changed = _advance_call(call, "bridged") or changed
    elif et == "call_hungup":
        if event.hangup_cause:
            leg.hangup_cause = event.hangup_cause
        # F8: busy/rejected/cancel causes land the leg at "failed" rather than a plain
        # "hungup" - derive_call_status reads hangup_cause back off it to pick the flavor.
        changed = advance_leg(leg, _leg_terminal_status_for_cause(event.hangup_cause)) or changed
        if event.duration_seconds is not None:
            leg.extra = {**(leg.extra or {}), "carrier_duration_seconds": event.duration_seconds}
    elif et in ("machine_detected", "human_detected"):
        # F14: AMD is monotonic - the FIRST verdict recorded on a leg wins; a redelivered
        # or contradictory async AMD callback must never overwrite it.
        if leg.amd_result is None:
            leg.amd_result = "machine" if et == "machine_detected" else "human"
            changed = True
    elif et == "dtmf_received":
        pass  # digits are ledgered on the VoiceEvent row only; no state change (P11's job).
    elif et == "transfer_completed":
        pass  # informational this phase; the new leg's own events drive state.
    elif et == "recording_ready":
        # F6/F7/F16: upsert ONLY - no network I/O in the webhook path. The sweeper's
        # fetch_pending_recordings does the actual download, re-deriving the carrier URL
        # from the VoiceEvent row just ledgered above.
        await recordings_svc.on_recording_ready(session, event, call, leg)

    legs = await load_legs(session, call.id)
    if derive_call_status(call, legs):
        changed = True
    return changed


# --------------------------------------------------------------------------------------
# Transfer / hangup
# --------------------------------------------------------------------------------------
async def start_blind_transfer(
    session: AsyncSession, registry, call: Call, to: str  # noqa: ANN001 - CarrierRegistry
) -> CallLeg:
    """Blind transfer: a NEW leg models it (it never mutates the old one - D6/R7).

    Telnyx executes the Transfer command against the active leg immediately. Bandwidth
    cannot deliver a command mid-stream - BXML only rides on webhook responses - so its
    adapter raises FeatureUnavailableError, which the API route translates into a 422
    explaining the limitation. That error is never swallowed here.
    """
    legs = await load_legs(session, call.id)
    current = active_leg(legs)
    if current is None:
        raise ConflictError("This call has no active leg to transfer")

    carrier_obj = registry.get(call.carrier) if registry is not None else None
    voice_carrier = as_voice_carrier(carrier_obj)

    new_leg = CallLeg(
        id=uuid.uuid4(),
        org_id=call.org_id,
        call_id=call.id,
        to_e164=to,
        from_e164=call.our_e164,
        status="created",
        reason="transfer",
    )
    session.add(new_leg)
    await session.flush()

    try:
        await voice_carrier.execute_commands(
            current.provider_call_id, [TransferCommand(to=to, from_=call.our_e164)]
        )
    except FeatureUnavailableError:
        await session.rollback()
        raise

    await session.commit()
    return new_leg


async def hangup_active_leg(session: AsyncSession, registry, call: Call) -> CallLeg:  # noqa: ANN001
    """Hang up whichever leg is currently carrying the call. Telnyx only - Bandwidth raises
    FeatureUnavailableError (mid-stream commands need BXML delivered via a webhook
    response), which the API route translates into a 422."""
    legs = await load_legs(session, call.id)
    leg = active_leg(legs)
    if leg is None:
        raise ConflictError("This call has no active leg to hang up")

    carrier_obj = registry.get(call.carrier) if registry is not None else None
    voice_carrier = as_voice_carrier(carrier_obj)
    await voice_carrier.execute_commands(leg.provider_call_id, [HangupCommand()])
    return leg


async def start_gather(
    session: AsyncSession,
    registry,  # noqa: ANN001 - CarrierRegistry
    call: Call,
    *,
    max_digits: int = 1,
    terminating_digit: str = "#",
    timeout_seconds: int = 10,
    prompt_text: str = "",
    action_tag: str = "",
) -> CallLeg:
    """F3e: DTMF gather on whichever leg is currently carrying the call. Telnyx only -
    Bandwidth raises FeatureUnavailableError (mid-stream commands need BXML delivered via a
    webhook response), which the API route translates into a 422, same pattern as transfer
    and hangup above."""
    legs = await load_legs(session, call.id)
    leg = active_leg(legs)
    if leg is None:
        raise ConflictError("This call has no active leg to gather on")

    carrier_obj = registry.get(call.carrier) if registry is not None else None
    voice_carrier = as_voice_carrier(carrier_obj)
    prompt = SpeakCommand(text=prompt_text) if prompt_text else None
    await voice_carrier.execute_commands(
        leg.provider_call_id,
        [
            GatherCommand(
                max_digits=max_digits,
                terminating_digit=terminating_digit,
                timeout_seconds=timeout_seconds,
                prompt=prompt,
                action_tag=action_tag,
            )
        ],
    )
    return leg
