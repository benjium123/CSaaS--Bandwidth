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
  hold ahead of the seconds used; when the balance can no longer extend it, the call is
  hung up. At the end the real seconds are charged per second and every hold released.
- Every charge is idempotent on its ledger reference (message id, call id, number + period)
  so a retried send, a replayed webhook or a re-run sweeper never charges twice.
- Only traffic after ``orgs.telephony_prepaid_since`` is billed - switching the gate on never
  retro-charges an org's past calls, and never bills a number for a stretch we were not
  charging for (the rental clock is stamped forward instead).
- Customer price = the org's explicit rate price (``provider_rates.price_micros``) when set,
  else the flat, carrier-independent ``PLATFORM_PRICE_MICROS`` table. Those metrics cost the
  same whichever carrier carries them and are priced (not free) even on an unrated/unknown
  carrier; only metrics outside the table still fall back to rate-card cost x
  (1 + DEFAULT_TRAFFIC_MARKUP_BPS), rounded up. Customers never see cost or margin.
"""

from __future__ import annotations

import calendar
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
from app.services import credits, plans, spend

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

#: Flat, carrier-independent platform price per unit, in micros - the fallback when
#: ``platform_prices`` (operator-editable, migration 0064) has no row for the metric.
#: Billing v2: SMS $0.015/segment and MMS $0.035 in and out when no bundle covers them;
#: calls $0.012/minute in and out, whole minutes rounded up; fax $0.10/page; number $15/mo.
PLATFORM_PRICE_MICROS: dict[str, int] = {
    "sms_out": 15_000,
    "sms_in": 15_000,
    "mms_out": 35_000,
    "mms_in": 35_000,
    "voice_min_out": 12_000,
    "voice_min_in": 12_000,
    "fax_page_out": 100_000,
    "fax_page_in": 100_000,
    "number_mrc": 15_000_000,
    "number_setup": 0,
    #: Per bundle: 1,000 SMS / 100 MMS / 1,000 call minutes (services/bundles.UNITS_PER_BUNDLE).
    "sms_bundle": 13_000_000,
    "mms_bundle": 3_000_000,
    "voice_bundle": 10_000_000,
}
#: An inbound call is billed from arrival (LiveKit builds the room as soon as it rings),
#: with this minimum even when nobody answers.
INBOUND_MIN_SECONDS = 60


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


async def platform_price(session: AsyncSession, metric: str) -> int:
    """The operator's list price for `metric`: the ``platform_prices`` row, else the
    constant. Raises KeyError for a metric in neither."""
    from app.models import PlatformPrice

    row = await session.get(PlatformPrice, metric)
    if row is not None:
        return int(row.price_micros)
    return PLATFORM_PRICE_MICROS[metric]


async def unit_price(session: AsyncSession, org_id: uuid.UUID, provider: str, metric: str) -> int:
    """Customer price in micros for one unit of `metric` on `provider`.

    An explicit per-org ``provider_rates.price_micros`` override always wins. Otherwise a
    metric in the flat ``PLATFORM_PRICE_MICROS`` table is priced straight from that table -
    the same price whichever carrier carries it, and a priced metric on an unrated/unknown
    carrier is no longer free. Only metrics outside that table fall back to rate-card cost x
    markup, and only those return 0 for a carrier with no rate card at all.
    """
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
    if metric in PLATFORM_PRICE_MICROS:
        return await platform_price(session, metric)
    cost, _is_override, is_known = await spend.resolve_rate(session, provider, metric)
    if not is_known:
        return 0
    return _price_from_cost(cost)


async def record_refusal(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    kind: str,
    price_micros: int = 0,
    balance_micros: int = 0,
    detail: str | None = None,
    reason: str = "no_credit",
) -> None:
    """Count one attempt the credit gate stopped. Never raises.

    The caller is about to raise and roll back, so on Postgres the row is written through
    its OWN short session and survives. On SQLite (tests: one pinned connection) a second
    session would commit the caller's half-done work, so the row joins the caller's session
    instead - best effort there.
    """
    from app.models import BillingRefusal

    row = dict(
        id=uuid.uuid4(),
        org_id=org_id,
        kind=kind,
        reason=reason,
        price_micros=int(price_micros),
        balance_micros=int(balance_micros),
        detail=str(detail)[:255] if detail else None,
        created_at=_now(),
    )
    try:
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            from app.db.session import get_sessionmaker

            async with get_sessionmaker()() as own:
                # Never wait long on a pool or a lock for a counter row.
                await own.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
                await own.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
                set_org_context(own, org_id)
                own.add(BillingRefusal(**row))
                await own.commit()
        else:
            set_org_context(session, org_id)
            session.add(BillingRefusal(**row))
    except Exception:
        log.exception("telephony_billing.record_refusal_failed", org_id=str(org_id), kind=kind)


async def _require_balance(
    session: AsyncSession,
    org_id: uuid.UUID,
    price: int,
    *,
    kind: str = "sms",
    detail: str | None = None,
) -> None:
    # At least one micro: an empty (or negative) balance refuses even a zero-priced action.
    current = await credits.balance(session, org_id)
    if current < max(int(price), 1):
        await record_refusal(
            session, org_id, kind=kind, price_micros=price, balance_micros=current, detail=detail
        )
        raise TelephonyCreditsError()


# ------------------------------------------------------------------------------------
# SMS / MMS
# ------------------------------------------------------------------------------------
#: Every text and picture message, in or out, counts against the same package allowance -
#: that is the unit customers were sold ("1,000 texts"), not one bucket per direction.
SMS_ALLOWANCE_METRIC = "sms_segments"
VOICE_ALLOWANCE_METRIC = "voice_minutes"


async def _plan_covers(session: AsyncSession, org: Org, metric: str, units: int) -> int:
    """Take `units` off the org's package allowance and report what it covered.

    Returns 0 for an org on no package, which is every org today - the allowance path is
    inert until platform ops seeds plans and puts an org on one.
    """
    if org.plan_code is None or units <= 0:
        return 0
    return await plans.take(session, org.id, metric, units)


async def _plan_headroom(session: AsyncSession, org: Org, metric: str) -> int:
    """What the package would still cover, without taking it - for the pre-send gate, which
    must never write."""
    if org.plan_code is None:
        return 0
    return await plans.remaining(session, org.id, metric)


def _sms_metric(*, is_mms: bool, outbound: bool) -> str:
    return f"{'mms' if is_mms else 'sms'}_{'out' if outbound else 'in'}"


def _sms_units(segments: int | None, *, is_mms: bool) -> int:
    return 1 if is_mms else max(int(segments or 1), 1)


def _is_mms(message: Message) -> bool:
    return bool(message.media)


def _bundle_kind(is_mms: bool) -> str:
    return "mms" if is_mms else "sms"


async def _bundle_headroom(session: AsyncSession, org_id: uuid.UUID, is_mms: bool) -> int:
    from app.services import bundles

    return await bundles.units(session, org_id, _bundle_kind(is_mms))


async def _bundle_covers(
    session: AsyncSession, org_id: uuid.UUID, is_mms: bool, units: int, *, reference: str
) -> int:
    """Spend bundle units (after the plan allowance, before the $ balance)."""
    if units <= 0:
        return 0
    from app.services import bundles

    return await bundles.take(session, org_id, _bundle_kind(is_mms), units, reference=reference)


#: Bundle kind holding prepaid call minutes (both directions).
VOICE_BUNDLE_KIND = "voice"


async def _voice_bundle_minutes(session: AsyncSession, org_id: uuid.UUID) -> int:
    from app.services import bundles

    return await bundles.units(session, org_id, VOICE_BUNDLE_KIND)


async def _minutes_in_flight(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    exclude_call_id: uuid.UUID | None,
    moment: datetime,
) -> int:
    """Whole minutes the org's other not-yet-billed calls have already used. Bundle minutes
    are only taken when a call is billed, so without this every concurrent call would count
    the WHOLE bundle as its own cover."""
    set_org_context(session, org_id)
    calls = (
        await session.execute(
            sa.select(Call).where(
                Call.org_id == org_id,
                Call.billed_at.is_(None),
                Call.created_at >= moment - timedelta(hours=12),
            )
        )
    ).scalars().all()
    total = 0
    for other in calls:
        if other.id == exclude_call_id or (other.extra or {}).get("refused"):
            continue
        if other.ended_at is not None:
            seconds = billable_seconds(other)
        else:
            start = _call_clock_start(other)
            if start is None:
                continue
            seconds = max(int((moment - _as_utc(start)).total_seconds()), 0)
            if other.direction != "outbound":
                seconds = max(seconds, INBOUND_MIN_SECONDS)
        total += spend._ceil_minutes(seconds)
    return total


async def _voice_bundle_cover(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    exclude_call_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> int:
    """Bundle minutes still free for ONE call: the bundle minus what the org's other unbilled
    calls have used (the pool is shared)."""
    bundle = await _voice_bundle_minutes(session, org_id)
    if bundle <= 0:
        return 0
    used = await _minutes_in_flight(
        session, org_id, exclude_call_id=exclude_call_id, moment=moment or _now()
    )
    return max(bundle - used, 0)


async def sms_price(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    carrier: str,
    segments: int | None,
    is_mms: bool,
    outbound: bool = True,
    #: Bill only this many units instead of the message's own count - what the package
    #: allowance did NOT cover.
    units: int | None = None,
) -> int:
    metric = _sms_metric(is_mms=is_mms, outbound=outbound)
    per_unit = await unit_price(session, org_id, carrier, metric)
    billable = _sms_units(segments, is_mms=is_mms) if units is None else max(int(units), 0)
    return per_unit * billable


async def require_sms_credit(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    carrier: str,
    segments: int | None,
    is_mms: bool,
) -> None:
    """Refuse an outbound SMS/MMS the balance cannot cover. No-op when not prepaid.

    The package allowance is checked FIRST: an org with texts left on its plan must go out
    even on an empty balance, because those texts are already paid for. Only the units the
    plan cannot cover have to be covered by credits.
    """
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return
    units = _sms_units(segments, is_mms=is_mms)
    uncovered = max(units - await _plan_headroom(session, org, SMS_ALLOWANCE_METRIC), 0)
    uncovered = max(uncovered - await _bundle_headroom(session, org_id, is_mms), 0)
    if uncovered <= 0:
        return
    price = await sms_price(
        session, org_id, carrier=carrier, segments=segments, is_mms=is_mms, units=uncovered
    )
    await _require_balance(
        session, org_id, price, kind="mms" if is_mms else "sms", detail=f"{units} unit(s)"
    )


async def can_send_sms(session: AsyncSession, org_id: uuid.UUID, message: Message) -> bool:
    """Dispatch-time re-check (held, scheduled and campaign sends reach the carrier without
    passing through send_message's early check). True when not prepaid."""
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return True
    units = _sms_units(message.segment_count_est, is_mms=_is_mms(message))
    uncovered = max(units - await _plan_headroom(session, org, SMS_ALLOWANCE_METRIC), 0)
    uncovered = max(uncovered - await _bundle_headroom(session, org_id, _is_mms(message)), 0)
    if uncovered <= 0:
        return True
    price = await sms_price(
        session,
        org_id,
        carrier=message.carrier,
        segments=message.segment_count_est,
        is_mms=_is_mms(message),
        units=uncovered,
    )
    return await credits.balance(session, org_id) >= max(price, 1)


async def charge_sms(session: AsyncSession, org_id: uuid.UUID, message: Message) -> None:
    """Charge one message once: outbound when the carrier accepts it, inbound on receipt.
    Does not commit - it rides the caller's transaction."""
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return
    set_org_context(session, org_id)
    if await credits._existing(session, org_id, "usage", f"sms:{message.id}") is not None:
        # Already charged in $ - a replay must not now spend bundle units as well.
        return
    outbound = message.direction == "outbound"
    segments = message.segment_count_carrier or message.segment_count_est
    units = _sms_units(segments, is_mms=_is_mms(message))
    # The package allowance is spent first and only the remainder is charged. The take rides
    # this same transaction, so a send that rolls back does not silently eat the customer's
    # included texts.
    billable = units - await _plan_covers(session, org, SMS_ALLOWANCE_METRIC, units)
    billable -= await _bundle_covers(
        session, org_id, _is_mms(message), billable, reference=f"sms:{message.id}"
    )
    if billable <= 0:
        return
    price = await sms_price(
        session,
        org_id,
        carrier=message.carrier,
        segments=segments,
        is_mms=_is_mms(message),
        outbound=outbound,
        units=billable,
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

    # "Was the original send in scope?" asked directly, as the module's own rule states it:
    # only traffic after telephony_prepaid_since is billed. This used to be inferred from the
    # presence of a usage ledger row, which stopped being a sound proxy the moment a package
    # could cover a text outright and leave no row behind.
    # Skipped only on POSITIVE evidence the send predates the gate. An unsaved message has no
    # created_at yet, and defaulting that to "out of scope" would silently drop corrections
    # for the ordinary in-flight case this is called from.
    since = org.telephony_prepaid_since
    created = message.created_at
    if since is not None and created is not None and _as_utc(created) < _as_utc(since):
        return

    # Only now does the package meter the extra segments. A text the plan covered in full has
    # no ledger entry at all, so this deliberately does not ask "was it charged?" - that
    # question would silently drop the correction and let every under-estimated segment on a
    # covered text go unmetered.
    extra = carrier_count - estimated
    billable = extra - await _plan_covers(session, org, SMS_ALLOWANCE_METRIC, extra)
    billable -= await _bundle_covers(
        session, org_id, False, billable, reference=f"sms:{message.id}:segments"
    )
    if billable <= 0:
        return
    per_segment = await unit_price(session, org_id, message.carrier, "sms_out")
    delta = billable * per_segment
    if delta > 0:
        await credits.charge_usage(
            session,
            org_id,
            delta,
            reference=f"sms:{message.id}:segments",
            note=f"{billable} extra segment(s) reported by the carrier",
        )


# ------------------------------------------------------------------------------------
# Calls
# ------------------------------------------------------------------------------------
def voice_price_micros(duration_seconds: int | None, price_per_minute_micros: int) -> int:
    """Voice charge in WHOLE minutes, rounded up (billing v2): 61s = 2 minutes.
    A 0-second (or None) call charges 0. Integer arithmetic only."""
    seconds = int(duration_seconds or 0)
    if seconds <= 0 or price_per_minute_micros <= 0:
        return 0
    return ((seconds + 59) // 60) * int(price_per_minute_micros)


def billable_seconds(call: Call) -> int:
    """Seconds a finished call is billed for.

    Outbound: from answer to hang-up (``duration_seconds``); never answered = 0.
    Inbound: from ARRIVAL (``created_at`` - the room exists from the first ring) to the
    end, at least INBOUND_MIN_SECONDS even when nobody answered.
    """
    if call.direction == "outbound":
        return max(int(call.duration_seconds or 0), 0)
    start = call.created_at
    end = call.ended_at
    if start is None or end is None:
        return max(int(call.duration_seconds or 0), INBOUND_MIN_SECONDS)
    elapsed = int((_as_utc(end) - _as_utc(start)).total_seconds())
    return max(elapsed, INBOUND_MIN_SECONDS)


def call_hold_reference(call_id: uuid.UUID, n: int = 0) -> str:
    """The ONE place call-hold references are built (the ledger's reference uniqueness is
    what makes a repeated hold a no-op)."""
    return f"callres:{call_id}" if n == 0 else f"callres:{call_id}:{n}"


def _call_metric(direction: str) -> str:
    return "voice_min_out" if direction == "outbound" else "voice_min_in"


async def require_call_credit(session: AsyncSession, org_id: uuid.UUID, call: Call) -> None:
    """At an outbound dial: refuse unless at least ONE MINUTE of talk time is covered, then
    hold up to CALL_RESERVE_MINUTES. No-op when not prepaid. Does not commit.

    The floor is one minute: billing is in whole minutes (billing v2), so a dial that
    cannot pay for its first minute is refused cleanly rather than cut a moment later.
    """
    if not await is_prepaid(session, org_id):
        return
    per_minute = await unit_price(session, org_id, call.carrier, "voice_min_out")
    current = await credits.balance(session, org_id)
    minimum = max(voice_price_micros(60, per_minute), 1)
    # Bundle minutes admit the call only when more than the cut-off headroom is free, so
    # enforce_active_calls does not end it moments after it connects.
    if current < minimum and (
        await _voice_bundle_cover(session, org_id, exclude_call_id=call.id)
        <= CUTOFF_HEADROOM_MINUTES
    ):
        await record_refusal(
            session,
            org_id,
            kind="call",
            price_micros=minimum,
            balance_micros=current,
            detail=getattr(call, "to_e164", None),
        )
        raise TelephonyCreditsError()
    hold = min(per_minute * CALL_RESERVE_MINUTES, current)
    if hold > 0:
        await credits.reserve(session, org_id, hold, reference=call_hold_reference(call.id))


async def inbound_call_allowed(session: AsyncSession, org_id: uuid.UUID, carrier: str) -> bool:
    """Billing v2 hard stop: an inbound call is only taken when the balance covers at least
    one inbound minute, or a call-minute bundle has a minute left. True when not prepaid."""
    org = await _org(session, org_id)
    if org is None or not org.telephony_prepaid:
        return True
    per_minute = await unit_price(session, org_id, carrier, "voice_min_in")
    if await credits.balance(session, org_id) >= max(per_minute, 1):
        return True
    return await _voice_bundle_cover(session, org_id) > CUTOFF_HEADROOM_MINUTES


async def refuse_inbound_call(session: AsyncSession, call: Call) -> None:
    """Record the refusal and mark the call so it is never billed. Does not commit."""
    current = await credits.balance(session, call.org_id)
    await record_refusal(
        session,
        call.org_id,
        kind="inbound_call",
        balance_micros=current,
        detail=call.contact_e164,
    )
    set_org_context(session, call.org_id)
    # Not stamped billed here: bill_finished_calls closes it at no charge once it ends, and
    # until then enforce_active_calls keeps retrying the hangup if the teardown failed.
    call.extra = {**(call.extra or {}), "refused": "no_credit"}


def _call_clock_start(call: Call) -> datetime | None:
    """Outbound calls cost from answer; inbound from arrival."""
    if call.direction == "outbound":
        return call.answered_at
    return call.created_at


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
    """Charge the real seconds of every finished call of a prepaid org, release its holds
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
            if (call.extra or {}).get("refused"):
                # Refused for credit: never billed.
                call.billed_at = _now()
                await session.commit()
                continue
            seconds = billable_seconds(call)
            minutes = spend._ceil_minutes(seconds)
            # The package allowance is denominated in whole MINUTES (plans.take takes integer
            # minute units), so it absorbs that many whole minutes of the call; the rest is
            # billed in whole minutes at the per-minute price.
            org_row = await _org(session, org_id)
            covered_minutes = (
                await _plan_covers(session, org_row, VOICE_ALLOWANCE_METRIC, minutes)
                if org_row is not None
                else 0
            )
            # Then prepaid call-minute bundles, then the $ balance.
            remaining_minutes = minutes - covered_minutes
            if remaining_minutes > 0:
                from app.services import bundles

                covered_minutes += await bundles.take(
                    session,
                    org_id,
                    VOICE_BUNDLE_KIND,
                    remaining_minutes,
                    reference=f"call:{call.id}:voice",
                )
            uncovered_seconds = max(seconds - covered_minutes * 60, 0)
            per_minute = await unit_price(
                session, org_id, call.carrier, _call_metric(call.direction)
            )
            price = voice_price_micros(uncovered_seconds, per_minute)
            if price > 0:
                await credits.charge_usage(
                    session,
                    org_id,
                    price,
                    reference=f"call:{call.id}:voice",
                    note=f"{seconds}s {call.direction} call",
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
    """Keep each running call's hold ahead of the minutes it has used - outbound from
    answer, inbound (billing v2) from arrival. When the balance can no longer cover the
    next minute, hang the call up via ``hangup(session, call)``. Returns calls cut off."""
    moment = now or _now()
    rows = (
        await session.execute(
            sa.select(Call.id, Call.org_id)
            .join(Org, Org.id == Call.org_id)
            .where(
                sa.or_(
                    sa.and_(Call.direction == "outbound", Call.answered_at.is_not(None)),
                    Call.direction == "inbound",
                ),
                Call.ended_at.is_(None),
                Call.billed_at.is_(None),
                Call.created_at >= moment - timedelta(hours=12),
                _billable_org_filter(),
            )
            .order_by(Call.created_at)
            .limit(BATCH)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()

    cut = 0
    for call_id, org_id in rows:
        try:
            set_org_context(session, org_id)
            call = await session.get(Call, call_id)
            start = _call_clock_start(call) if call is not None else None
            if call is None or call.ended_at is not None or start is None:
                continue
            if (call.extra or {}).get("refused"):
                # A refused inbound call whose room teardown failed: retry the hangup.
                try:
                    await hangup(session, call)
                except Exception:
                    await session.rollback()
                    log.warning("telephony_billing.refused_hangup_retry_failed", call_id=str(call_id))
                continue
            per_minute = await unit_price(session, org_id, call.carrier, _call_metric(call.direction))
            if per_minute <= 0:
                continue
            elapsed_seconds = max(int((moment - _as_utc(start)).total_seconds()), 0)
            used_micros = voice_price_micros(elapsed_seconds, per_minute)
            headroom_micros = CUTOFF_HEADROOM_MINUTES * per_minute
            held, holds = await _held_for_call(session, org_id, call.id)
            # Unspent call-minute bundle minutes cover the call too (taken when it is
            # billed). The pool is shared: minutes the org's other unbilled calls have
            # already used are not this call's cover.
            bundle_cover = (
                await _voice_bundle_cover(
                    session, org_id, exclude_call_id=call.id, moment=moment
                )
                * per_minute
            )
            if used_micros + headroom_micros <= held + bundle_cover:
                continue
            try:
                # Hold up to CALL_RESERVE_MINUTES more, or whatever whole minutes the
                # balance still covers; less than one minute left = the call ends.
                available = await credits.balance(session, org_id)
                extend = min(per_minute * CALL_RESERVE_MINUTES, (available // per_minute) * per_minute)
                if extend < per_minute:
                    raise InsufficientCreditsError()
                await credits.reserve(
                    session,
                    org_id,
                    extend,
                    reference=call_hold_reference(call.id, holds),
                )
                await session.commit()
            except InsufficientCreditsError:
                await session.rollback()
                set_org_context(session, org_id)
                call = await session.get(Call, call_id)
                if call is None or call.ended_at is not None:
                    continue
                await hangup(session, call)
                set_org_context(session, org_id)
                audit_svc.record(
                    session,
                    org_id,
                    action="call.ended_out_of_credits",
                    target_type="call",
                    target_id=str(call_id),
                    detail={"used_seconds": elapsed_seconds},
                )
                await session.commit()
                cut += 1
        except Exception:
            await session.rollback()
            log.exception("telephony_billing.enforce_call_failed", call_id=str(call_id))
    return cut


# ------------------------------------------------------------------------------------
# Number rental
# ------------------------------------------------------------------------------------
async def require_number_credit(session: AsyncSession, org_id: uuid.UUID, carrier: str) -> None:
    """Refuse a number order the balance cannot cover (first month + setup)."""
    if not await is_prepaid(session, org_id):
        return
    price = await unit_price(session, org_id, carrier, "number_mrc") + await unit_price(
        session, org_id, carrier, "number_setup"
    )
    await _require_balance(session, org_id, price, kind="number", detail=carrier)


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
    if (number.provisioning or {}).get("billing") == "stripe_subscription":
        return
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


def _rental_period_in_scope(number: OrgNumber, period_start: date, since: date) -> bool:
    """Is this rental period inside the stretch we have been billing this org for?

    The number-side mirror of ``_billable_org_filter``'s ``Call.created_at >=
    Org.telephony_prepaid_since``: nothing is charged for a period that predates the
    instant the prepaid gate went on for this org, however the row got there and whichever
    route flipped the flag.

    A number that has never been stamped (``rental_paid_through`` NULL) is judged by when
    its row was created, because the NULL itself carries no date:
    - created BEFORE the gate went on -> the org held it through a stretch we were not
      billing, so charging now would bill for that stretch. Out of scope; the caller stamps
      it forward and the first charge falls one cycle later.
    - created AFTER the gate went on -> in scope and charged from today, unchanged. This is
      what keeps an imported number chargeable rather than silently free forever.
    """
    if number.rental_paid_through is None:
        created = getattr(number, "created_at", None)
        # An unsaved row has no created_at yet; treat that as in scope rather than silently
        # granting a free month.
        return created is None or _as_utc(created).date() >= since
    return period_start >= since


async def renew_number_rentals(session: AsyncSession, *, today: date | None = None) -> int:
    """Charge the next month for every active number of a prepaid org whose rental has run
    out (or was never charged since the gate was switched on). Charged even when it takes
    the balance negative - the number keeps working, and outbound traffic stops until the
    org tops up. One commit per number.

    A period that predates ``orgs.telephony_prepaid_since`` is never charged - the row's
    rental clock is stamped one cycle forward instead, so switching the gate on never bills
    for a stretch we were not charging for. Orgs with no ``telephony_prepaid_since`` are
    skipped entirely (the mirror of ``_billable_org_filter``): we cannot prove any period is
    in scope, and refusing to bill what we cannot prove is the safe direction for money.
    """
    day = today or _now().date()
    rows = (
        await session.execute(
            sa.select(OrgNumber.id, OrgNumber.org_id)
            .join(Org, Org.id == OrgNumber.org_id)
            .where(
                Org.telephony_prepaid.is_(True),
                Org.telephony_prepaid_since.is_not(None),
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
    skipped = 0
    for number_id, org_id in rows:
        try:
            set_org_context(session, org_id)
            number = await session.get(OrgNumber, number_id)
            if (
                number is None
                or (number.provisioning or {}).get("billing") == "stripe_subscription"
            ):
                continue
            org_row = await _org(session, org_id)
            if org_row is None or org_row.telephony_prepaid_since is None:
                continue
            since = _as_utc(org_row.telephony_prepaid_since).date()
            period_start = number.rental_paid_through or day
            if _rental_period_in_scope(number, period_start, since):
                await _charge_rental_period(session, org_id, number, period_start)
                await session.commit()
                renewed += 1
            else:
                # Out of scope: the period predates the instant we started billing this
                # org. Do not charge it - start the clock at the next cycle instead.
                number.rental_paid_through = _next_month(day)
                await session.commit()
                skipped += 1
        except Exception:
            await session.rollback()
            log.exception("telephony_billing.renew_number_failed", number_id=str(number_id))
    if skipped:
        log.info("telephony_billing.rentals_out_of_scope_stamped", count=skipped)
    return renewed


async def stamp_rentals_forward(
    session: AsyncSession, org_id: uuid.UUID, *, today: date | None = None
) -> int:
    """On switching an org's prepaid gate ON: stamp the rental clock forward for every
    active number it already holds and has never billed, so the switch itself never
    triggers a retroactive month's charge.

    The number-side counterpart of the migration backfill, applied to ONE org at the moment
    platform ops flips the gate. Without it, flipping the gate on would let the very next
    ``renew_number_rentals`` pass bill a full month for every number the org already held -
    charging for a stretch during which it was not being billed. Each stamped number is
    charged normally from that date on.

    Returns how many numbers were stamped. Does not commit - it rides the caller's
    transaction, so the switch-on and the stamps land together or not at all.
    """
    day = today or _now().date()
    set_org_context(session, org_id)
    result = await session.execute(
        sa.update(OrgNumber)
        .where(
            OrgNumber.org_id == org_id,
            OrgNumber.rental_paid_through.is_(None),
            OrgNumber.released_at.is_(None),
            OrgNumber.status == "active",
        )
        .values(rental_paid_through=_next_month(day))
    )
    return int(result.rowcount or 0)


# ------------------------------------------------------------------------------------
# Sweeper entry point
# ------------------------------------------------------------------------------------
async def telephony_tick(
    session: AsyncSession, *, hangup: Any, now: datetime | None = None
) -> dict[str, int]:
    return {
        "calls_billed": await bill_finished_calls(session, now=now),
        "calls_cut_off_no_credit": await enforce_active_calls(session, hangup=hangup, now=now),
        "number_rentals_charged": await renew_number_rentals(session, today=(now or _now()).date()),
    }
