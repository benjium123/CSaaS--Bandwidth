"""Prepaid telephony: the per-org hard gate, and charging SMS/MMS, calls and number rental.

Fable-owned money code. ``services/credits.py`` stays the ONLY writer of credit_ledger;
this module only decides WHEN and HOW MUCH, then calls it.

Rules
- Enforced per org (``orgs.telephony_prepaid``). An org with it off is never read from,
  charged, or refused here - every public function returns early.
- Outbound is gated BEFORE it happens: an outbound SMS/MMS, an outbound dial and a number
  order are refused (402, TelephonyCreditsError) when the balance cannot cover them.
- Inbound SMS and inbound call minutes are CHARGED but never refused - a customer's message
  or caller is never dropped for our billing's sake. That can take the balance below zero;
  the next outbound attempt is then refused until the org tops up.
- An outbound call holds CALL_RESERVE_MINUTES at dial. While it runs, the sweeper keeps the
  hold ahead of the minutes used; when the balance can no longer extend it, the call is
  hung up. At the end the real minutes are charged and every hold released.
- Every charge is idempotent on its ledger reference (message id, call id, number + period)
  so a retried send, a replayed webhook or a re-run sweeper never charges twice.
- Only traffic after ``orgs.telephony_prepaid_since`` is billed - switching the gate on never
  retro-charges an org's past calls.
- Customer price = the org's explicit rate price (``provider_rates.price_micros``) when set,
  else rate-card cost x (1 + DEFAULT_TRAFFIC_MARKUP_BPS), rounded up. Customers never see
  cost or margin.
"""

from __future__ import annotations

import calendar
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import InsufficientCreditsError
from app.models import Call, CreditLedgerEntry, Message, Org, OrgNumber
from app.models.spend import ProviderRate
from app.services import audit as audit_svc
from app.services import credits, spend

log = structlog.get_logger("telephony_billing")

#: Platform default margin on telephony traffic when an org has no explicit rate price.
DEFAULT_TRAFFIC_MARKUP_BPS = 3000
#: Minutes held at dial, and the size of each top-up while an outbound call runs.
CALL_RESERVE_MINUTES = 5
#: Extend an outbound call's hold once the minutes used come within this of it.
CUTOFF_HEADROOM_MINUTES = 1
#: A finished call older than this is no longer picked up for billing.
BILLING_LOOKBACK = timedelta(days=7)
#: Rows handled per sweeper pass, per job.
BATCH = 500


class TelephonyCreditsError(InsufficientCreditsError):
    """402: the org's prepaid balance cannot cover this outbound SMS, call or number."""

    message = "Add credits to keep texting and calling"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _price_from_cost(cost_micros: int) -> int:
    return (int(cost_micros) * (10_000 + DEFAULT_TRAFFIC_MARKUP_BPS) + 9_999) // 10_000


def _next_month(day: date) -> date:
    """Same day next month, clamped to the month's length (Jan 31 -> Feb 28/29)."""
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


async def _org(session: AsyncSession, org_id: uuid.UUID) -> Org | None:
    return await session.get(Org, org_id)


async def is_prepaid(session: AsyncSession, org_id: uuid.UUID) -> bool:
    org = await _org(session, org_id)
    return bool(org is not None and org.telephony_prepaid)


async def unit_price(
    session: AsyncSession, org_id: uuid.UUID, provider: str, metric: str
) -> int:
    """Customer price in micros for one unit of `metric` on `provider`. 0 for a carrier
    with no rate card at all - never guess a price nobody set."""
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderRate.price_micros).where(
                ProviderRate.provider == provider,
                ProviderRate.metric == metric,
            )
        )
    ).first()
    if row is not None and row.price_micros is not None:
        return int(row.price_micros)
    cost, _is_override, is_known = await spend.resolve_rate(session, provider, metric)
    if not is_known:
        return 0
    return _price_from_cost(cost)


async def _require_balance(session: AsyncSession, org_id: uuid.UUID, price: int) -> None:
    # At least one micro: an empty (or negative) balance refuses even a zero-priced action.
    if await credits.balance(session, org_id) < max(int(price), 1):
        raise TelephonyCreditsError()


# ------------------------------------------------------------------------------------
# SMS / MMS
# ------------------------------------------------------------------------------------
def _sms_metric(*, is_mms: bool, outbound: bool) -> str:
    return f"{'mms' if is_mms else 'sms'}_{'out' if outbound else 'in'}"


def _sms_units(segments: int | None, *, is_mms: bool) -> int:
    return 1 if is_mms else max(int(segments or 1), 1)


def _is_mms(message: Message) -> bool:
    return bool(message.media)


async def sms_price(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    carrier: str,
    segments: int | None,
    is_mms: bool,
    outbound: bool = True,
) -> int:
    metric = _sms_metric(is_mms=is_mms, outbound=outbound)
    per_unit = await unit_price(session, org_id, carrier, metric)
    return per_unit * _sms_units(segments, is_mms=is_mms)


async def require_sms_credit(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    carrier: str,
    segments: int | None,
    is_mms: bool,
) -> None:
    """Refuse an outbound SMS/MMS the balance cannot cover. No-op when not prepaid."""
    if not await is_prepaid(session, org_id):
        return
    price = await sms_price(session, org_id, carrier=carrier, segments=segments, is_mms=is_mms)
    await _require_balance(session, org_id, price)


async def can_send_sms(session: AsyncSession, org_id: uuid.UUID, message: Message) -> bool:
    """Dispatch-time re-check (held, scheduled and campaign sends reach the carrier without
    passing through send_message's early check). True when not prepaid."""
    if not await is_prepaid(session, org_id):
        return True
    price = await sms_price(
        session,
        org_id,
        carrier=message.carrier,
        segments=message.segment_count_est,
        is_mms=_is_mms(message),
    )
    return await credits.balance(session, org_id) >= max(price, 1)


async def charge_sms(session: AsyncSession, org_id: uuid.UUID, message: Message) -> None:
    """Charge one message once: outbound when the carrier accepts it, inbound on receipt.
    Does not commit - it rides the caller's transaction."""
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return
    outbound = message.direction == "outbound"
    segments = message.segment_count_carrier or message.segment_count_est
    price = await sms_price(
        session,
        org_id,
        carrier=message.carrier,
        segments=segments,
        is_mms=_is_mms(message),
        outbound=outbound,
    )
    if price <= 0:
        return
    await credits.charge_usage(
        session,
        org_id,
        price,
        reference=f"sms:{message.id}",
        note=f"{'MMS' if _is_mms(message) else 'SMS'} {'sent' if outbound else 'received'}",
    )


async def charge_segment_correction(
    session: AsyncSession, org_id: uuid.UUID, message: Message
) -> None:
    """The carrier's segment count is the truth. If it reports MORE segments than the
    estimate an outbound SMS was charged for, charge the difference once. Never refunds -
    a lower count was already the carrier's own rounding in the customer's favour."""
    if message.direction != "outbound" or _is_mms(message):
        return
    carrier_count = message.segment_count_carrier
    estimated = message.segment_count_est
    if carrier_count is None or estimated is None or carrier_count <= estimated:
        return
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return
    set_org_context(session, org_id)
    charged = (
        await session.execute(
            sa.select(CreditLedgerEntry.id)
            .where(
                CreditLedgerEntry.entry_type == "usage",
                CreditLedgerEntry.reference == f"sms:{message.id}",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if charged is None:
        # The original send was never charged (prepaid switched on after it) - nothing to
        # correct.
        return
    per_segment = await unit_price(session, org_id, message.carrier, "sms_out")
    delta = (carrier_count - estimated) * per_segment
    if delta > 0:
        await credits.charge_usage(
            session,
            org_id,
            delta,
            reference=f"sms:{message.id}:segments",
            note=f"{carrier_count - estimated} extra segment(s) reported by the carrier",
        )


# ------------------------------------------------------------------------------------
# Calls
# ------------------------------------------------------------------------------------
def call_hold_reference(call_id: uuid.UUID, n: int = 0) -> str:
    """The ONE place call-hold references are built (the ledger's reference uniqueness is
    what makes a repeated hold a no-op)."""
    return f"callres:{call_id}" if n == 0 else f"callres:{call_id}:{n}"


def _call_metric(direction: str) -> str:
    return "voice_min_out" if direction == "outbound" else "voice_min_in"


async def require_call_credit(session: AsyncSession, org_id: uuid.UUID, call: Call) -> None:
    """At an outbound dial: refuse unless at least one minute is covered, then hold up to
    CALL_RESERVE_MINUTES. No-op when not prepaid. Does not commit."""
    if not await is_prepaid(session, org_id):
        return
    per_minute = await unit_price(session, org_id, call.carrier, "voice_min_out")
    current = await credits.balance(session, org_id)
    if current < max(per_minute, 1):
        raise TelephonyCreditsError()
    hold = min(per_minute * CALL_RESERVE_MINUTES, current)
    if hold > 0:
        await credits.reserve(session, org_id, hold, reference=call_hold_reference(call.id))


async def _hold_rows(
    session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID
) -> list[tuple[str, str | None, int]]:
    prefix = call_hold_reference(call_id)
    set_org_context(session, org_id)
    return [
        (entry_type, reference, int(amount))
        for entry_type, reference, amount in (
            await session.execute(
                sa.select(
                    CreditLedgerEntry.entry_type,
                    CreditLedgerEntry.reference,
                    CreditLedgerEntry.amount_micros,
                ).where(
                    CreditLedgerEntry.entry_type.in_(("reserve", "release")),
                    sa.or_(
                        CreditLedgerEntry.reference == prefix,
                        CreditLedgerEntry.reference.like(prefix + ":%"),
                    ),
                )
            )
        ).all()
    ]


async def _held_for_call(
    session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID
) -> tuple[int, int]:
    """(micros still held, number of holds ever taken) for one call."""
    rows = await _hold_rows(session, org_id, call_id)
    # A reserve row is -amount and its release +amount, so the unreleased hold is minus
    # the sum over both.
    held = -sum(amount for _t, _r, amount in rows)
    holds = sum(1 for entry_type, _r, _a in rows if entry_type == "reserve")
    return max(held, 0), holds


async def _release_call_holds(session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID) -> None:
    for entry_type, reference, _amount in await _hold_rows(session, org_id, call_id):
        if entry_type == "reserve" and reference:
            await credits.release(session, org_id, reference=reference)


def _billable_org_filter():
    return sa.and_(
        Org.telephony_prepaid.is_(True),
        Org.telephony_prepaid_since.is_not(None),
        Call.created_at >= Org.telephony_prepaid_since,
    )


async def bill_finished_calls(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Charge the real minutes of every finished call of a prepaid org, release its holds
    and stamp billed_at. One commit per call, so one bad row never blocks the rest and a
    re-run never charges twice."""
    cutoff = (now or _now()) - BILLING_LOOKBACK
    # JUSTIFIED: a sweeper pass legitimately spans every tenant.
    rows = (
        await session.execute(
            sa.select(Call.id, Call.org_id)
            .join(Org, Org.id == Call.org_id)
            .where(
                Call.ended_at.is_not(None),
                Call.billed_at.is_(None),
                Call.ended_at >= cutoff,
                _billable_org_filter(),
            )
            .order_by(Call.ended_at)
            .limit(BATCH)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()

    billed = 0
    for call_id, org_id in rows:
        try:
            set_org_context(session, org_id)
            call = await session.get(Call, call_id)
            if call is None or call.billed_at is not None:
                continue
            minutes = spend._ceil_minutes(call.duration_seconds)
            price = minutes * await unit_price(
                session, org_id, call.carrier, _call_metric(call.direction)
            )
            if price > 0:
                await credits.charge_usage(
                    session,
                    org_id,
                    price,
                    reference=f"call:{call.id}:voice",
                    note=f"{minutes} min {call.direction} call",
                )
            await _release_call_holds(session, org_id, call.id)
            call.billed_at = _now()
            await session.commit()
            billed += 1
        except Exception:
            await session.rollback()
            log.exception("telephony_billing.bill_call_failed", call_id=str(call_id))
    return billed


async def enforce_active_calls(
    session: AsyncSession,
    *,
    hangup: Any,
    now: datetime | None = None,
) -> int:
    """Keep each running OUTBOUND call's hold ahead of the minutes it has used. When the
    balance cannot extend it, hang the call up via ``hangup(session, call)``. Inbound calls
    are never cut off (they are charged at the end instead). Returns calls cut off."""
    moment = now or _now()
    rows = (
        await session.execute(
            sa.select(Call.id, Call.org_id)
            .join(Org, Org.id == Call.org_id)
            .where(
                Call.direction == "outbound",
                Call.answered_at.is_not(None),
                Call.ended_at.is_(None),
                _billable_org_filter(),
            )
            .limit(BATCH)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()

    cut = 0
    for call_id, org_id in rows:
        try:
            set_org_context(session, org_id)
            call = await session.get(Call, call_id)
            if call is None or call.ended_at is not None or call.answered_at is None:
                continue
            per_minute = await unit_price(session, org_id, call.carrier, "voice_min_out")
            if per_minute <= 0:
                continue
            elapsed = max((moment - _as_utc(call.answered_at)).total_seconds(), 0)
            used_minutes = math.ceil(elapsed / 60) if elapsed > 0 else 0
            held, holds = await _held_for_call(session, org_id, call.id)
            if (used_minutes + CUTOFF_HEADROOM_MINUTES) * per_minute <= held:
                continue
            try:
                await credits.reserve(
                    session,
                    org_id,
                    per_minute * CALL_RESERVE_MINUTES,
                    reference=call_hold_reference(call.id, holds),
                )
                await session.commit()
            except InsufficientCreditsError:
                await session.rollback()
                set_org_context(session, org_id)
                call = await session.get(Call, call_id)
                if call is None or call.ended_at is not None:
                    continue
                audit_svc.record(
                    session,
                    org_id,
                    action="call.ended_out_of_credits",
                    target_type="call",
                    target_id=str(call.id),
                    detail={"used_minutes": used_minutes},
                )
                await session.commit()
                await hangup(session, call)
                cut += 1
        except Exception:
            await session.rollback()
            log.exception("telephony_billing.enforce_call_failed", call_id=str(call_id))
    return cut


# ------------------------------------------------------------------------------------
# Number rental
# ------------------------------------------------------------------------------------
async def require_number_credit(
    session: AsyncSession, org_id: uuid.UUID, carrier: str
) -> None:
    """Refuse a number order the balance cannot cover (first month + setup)."""
    if not await is_prepaid(session, org_id):
        return
    price = await unit_price(session, org_id, carrier, "number_mrc") + await unit_price(
        session, org_id, carrier, "number_setup"
    )
    await _require_balance(session, org_id, price)


async def _charge_rental_period(
    session: AsyncSession, org_id: uuid.UUID, number: OrgNumber, period_start: date
) -> None:
    price = await unit_price(session, org_id, number.carrier, "number_mrc")
    if price > 0:
        await credits.charge_usage(
            session,
            org_id,
            price,
            reference=f"num:{number.id}:{period_start.isoformat()}",
            note=f"Monthly rental for {number.e164}",
        )
    number.rental_paid_through = _next_month(period_start)


async def charge_new_number(
    session: AsyncSession, org_id: uuid.UUID, number: OrgNumber, *, today: date | None = None
) -> None:
    """At order: charge setup (once) and the first month. Does not commit."""
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return
    setup = await unit_price(session, org_id, number.carrier, "number_setup")
    if setup > 0:
        await credits.charge_usage(
            session,
            org_id,
            setup,
            reference=f"num:{number.id}:setup",
            note=f"Setup for {number.e164}",
        )
    await _charge_rental_period(session, org_id, number, today or _now().date())


async def renew_number_rentals(session: AsyncSession, *, today: date | None = None) -> int:
    """Charge the next month for every active number of a prepaid org whose rental has run
    out (or was never charged since the gate was switched on). Charged even when it takes
    the balance negative - the number keeps working, and outbound traffic stops until the
    org tops up. One commit per number."""
    day = today or _now().date()
    rows = (
        await session.execute(
            sa.select(OrgNumber.id, OrgNumber.org_id)
            .join(Org, Org.id == OrgNumber.org_id)
            .where(
                Org.telephony_prepaid.is_(True),
                OrgNumber.released_at.is_(None),
                OrgNumber.status == "active",
                sa.or_(
                    OrgNumber.rental_paid_through.is_(None),
                    OrgNumber.rental_paid_through <= day,
                ),
            )
            .limit(BATCH)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()

    renewed = 0
    for number_id, org_id in rows:
        try:
            set_org_context(session, org_id)
            number = await session.get(OrgNumber, number_id)
            if number is None:
                continue
            period_start = number.rental_paid_through or day
            await _charge_rental_period(session, org_id, number, period_start)
            await session.commit()
            renewed += 1
        except Exception:
            await session.rollback()
            log.exception("telephony_billing.renew_number_failed", number_id=str(number_id))
    return renewed


# ------------------------------------------------------------------------------------
# Sweeper entry point
# ------------------------------------------------------------------------------------
async def telephony_tick(
    session: AsyncSession, *, hangup: Any, now: datetime | None = None
) -> dict[str, int]:
    return {
        "calls_billed": await bill_finished_calls(session, now=now),
        "calls_cut_off_no_credit": await enforce_active_calls(session, hangup=hangup, now=now),
        "number_rentals_charged": await renew_number_rentals(
            session, today=(now or _now()).date()
        ),
    }
