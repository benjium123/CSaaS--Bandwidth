"""Plan allowances: the texts, voice minutes and numbers an org's package includes.

This module owns the allowance counters ONLY. Usage is taken from the plan's allowance first
and everything past it becomes overage; the prepaid credit ledger - top-ups, reserves and
usage charges - is written exclusively by services/credits.py. `take()` is the seam between
the two: it reports how many units the plan covered and the caller charges the remainder.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import Org
from app.models.plans import Plan, PlanAllowance

log = structlog.get_logger("plans")

#: Metrics a plan can grant an allowance for. Anything else is overage from the first unit.
ALLOWANCE_METRICS: tuple[str, ...] = ("sms_segments", "voice_minutes", "numbers")


# ------------------------------------------------------------------------------------
# Billing periods
# ------------------------------------------------------------------------------------
def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def period_for(started_at: datetime | None, today: date) -> tuple[date, date]:
    """The org's current cycle as (period_start, period_end_exclusive).

    Cycles run anniversary-to-anniversary from the day the org started its plan, not on the
    calendar month. The anniversary day is re-applied to each month from the ORIGINAL
    started_at day: clamping 31 -> 28/29 in February must not drag March's cycle back to the
    28th, so a clamped period_start is never reused as the next anchor.
    """
    anchor_day = started_at.day if started_at is not None else today.day
    period_start = _clamped(today.year, today.month, anchor_day)
    if period_start > today:
        year, month = _shift_month(today.year, today.month, -1)
        period_start = _clamped(year, month, anchor_day)
    year, month = _shift_month(period_start.year, period_start.month, 1)
    return period_start, _clamped(year, month, anchor_day)


def _today() -> date:
    """UTC. The cycle boundary belongs to the org's anniversary, not to the server's local
    midnight, so it must not move twice a year."""
    return datetime.now(timezone.utc).date()


# ------------------------------------------------------------------------------------
# Plan / allowance lookups
# ------------------------------------------------------------------------------------
async def _load_org_plan(
    session: AsyncSession, org_id: uuid.UUID
) -> tuple[Org | None, Plan | None]:
    """The org plus its plan; the plan is None whenever plan_code is NULL or deactivated.
    Callers must already have set the tenant context."""
    org = await session.get(Org, org_id)
    if org is None or not org.plan_code:
        return org, None
    plan = await session.get(Plan, org.plan_code)
    if plan is None or not plan.is_active:
        return org, None
    return org, plan


async def plan_for(session: AsyncSession, org_id: uuid.UUID) -> Plan | None:
    set_org_context(session, org_id)
    _org, plan = await _load_org_plan(session, org_id)
    return plan


def _included(plan: Plan) -> dict[str, int]:
    """Allowance per metric, keyed by ALLOWANCE_METRICS. A metric the plan omits, or a value
    nobody can parse, seeds 0: an allowance is a promise the platform can keep, not a debt."""
    raw = plan.included if isinstance(plan.included, dict) else {}
    seeded: dict[str, int] = {}
    for metric in ALLOWANCE_METRICS:
        try:
            seeded[metric] = max(int(raw.get(metric, 0)), 0)
        except (TypeError, ValueError):
            seeded[metric] = 0
    return seeded


async def _allowances(
    session: AsyncSession, org_id: uuid.UUID, period_start: date
) -> dict[str, PlanAllowance]:
    rows = await session.scalars(
        sa.select(PlanAllowance).where(
            PlanAllowance.org_id == org_id,
            PlanAllowance.period_start == period_start,
        )
    )
    return {row.metric: row for row in rows}


async def ensure_period(
    session: AsyncSession, org_id: uuid.UUID, *, today: date | None = None
) -> dict[str, PlanAllowance]:
    """This org's allowance rows for the current period, created on first use.

    Two callers can race (a first message and a first call, say), compute the same period and
    both insert. The unique key (org_id, period_start, metric) lets exactly one win: the loser
    rolls back and re-reads the winner's rows instead of duplicating them. Counters already on
    disk are never reset or back-dated, and periods that have rolled over are left as history.
    """
    set_org_context(session, org_id)
    org, plan = await _load_org_plan(session, org_id)
    if org is None or plan is None:
        return {}
    period_start, _period_end = period_for(org.plan_started_at, today or _today())
    seeded = _included(plan)

    rows = await _allowances(session, org_id, period_start)
    for _attempt in range(2):
        missing = [metric for metric in ALLOWANCE_METRICS if metric not in rows]
        if not missing:
            return rows
        fresh = {
            metric: PlanAllowance(
                org_id=org_id,
                period_start=period_start,
                metric=metric,
                included_units=seeded[metric],
                used_units=0,
            )
            for metric in missing
        }
        try:
            # The insert goes inside a SAVEPOINT, not the caller's transaction: take() runs
            # mid-send, so a plain rollback here would throw away the Message or Call row the
            # caller is in the middle of writing. Losing a race for an allowance row must cost
            # nothing but a re-read. Same pattern as services/ai_usage.py.
            async with session.begin_nested():
                session.add_all(list(fresh.values()))
                await session.flush()
        except IntegrityError:
            log.debug(
                "plans.allowance_race",
                org_id=str(org_id),
                period=period_start.isoformat(),
            )
            set_org_context(session, org_id)
            rows = await _allowances(session, org_id, period_start)
            continue
        rows.update(fresh)
        return rows
    return rows


# ------------------------------------------------------------------------------------
# Taking allowance
# ------------------------------------------------------------------------------------
async def _take_up_to(session: AsyncSession, allowance_id: uuid.UUID, units: int) -> int:
    """One bounded UPDATE: add `units` only if they still fit, and report what was taken.

    Returns the UNITS taken, never the row count. The WHERE either matches the single row and
    takes the whole batch or matches nothing and takes none, so rowcount is only ever 1 or 0 -
    returning it would report a fully covered 3-segment text as 1 covered segment and bill the
    customer for the other two.
    """
    if units <= 0:
        return 0
    result = await session.execute(
        sa.update(PlanAllowance)
        .where(
            PlanAllowance.id == allowance_id,
            PlanAllowance.used_units + units <= PlanAllowance.included_units,
        )
        .values(used_units=PlanAllowance.used_units + units)
        # A counter, not an object we keep using: skip ORM sync so this stays one statement.
        .execution_options(synchronize_session=False)
    )
    return units if (result.rowcount or 0) else 0


async def _headroom(session: AsyncSession, allowance_id: uuid.UUID) -> int:
    remaining = await session.scalar(
        sa.select(PlanAllowance.included_units - PlanAllowance.used_units).where(
            PlanAllowance.id == allowance_id
        )
    )
    return max(int(remaining or 0), 0)


async def remaining(
    session: AsyncSession, org_id: uuid.UUID, metric: str, *, today: date | None = None
) -> int:
    """How many units of `metric` the plan would still cover, WITHOUT taking any.

    Deliberately read-only and row-creating-free: this is what the pre-send gate asks, and a
    gate must never write. It is a peek, not a reservation - the answer can be stale by the
    time the unit is actually taken, which is exactly why `take()` re-checks with a bounded
    UPDATE rather than trusting this.
    """
    set_org_context(session, org_id)
    org, plan = await _load_org_plan(session, org_id)
    if org is None or plan is None or metric not in ALLOWANCE_METRICS:
        return 0
    period_start, _period_end = period_for(org.plan_started_at, today or _today())
    row = (await _allowances(session, org_id, period_start)).get(metric)
    if row is None:
        # Nothing taken yet this period, so the whole allowance is still there.
        return _included(plan)[metric]
    return max(int(row.included_units) - int(row.used_units), 0)


async def take(
    session: AsyncSession,
    org_id: uuid.UUID,
    metric: str,
    units: int,
    *,
    today: date | None = None,
) -> int:
    """Move up to `units` of `metric` off the plan's allowance.

    Returns how many units the plan covered (0..units); the caller prices the rest as overage
    against prepaid credits. Does not commit - the caller's transaction owns it.

    The counter is bumped with a bounded UPDATE (used + n <= included) rather than a
    read-then-write, so two calls arriving at once cannot jointly push a metric past its
    allowance and hand the customer a unit nobody was billed for.
    """
    set_org_context(session, org_id)
    units = int(units)
    if units <= 0 or metric not in ALLOWANCE_METRICS:
        return 0

    rows = await ensure_period(session, org_id, today=today)
    row = rows.get(metric)
    if row is None:
        return 0

    covered = await _take_up_to(session, row.id, units)
    if not covered:
        # The whole amount did not fit. Take only the headroom that is left, bounded again so
        # a concurrent caller that just spent it cannot drive used_units past included_units.
        covered = await _take_up_to(session, row.id, await _headroom(session, row.id))
    if covered:
        log.debug("plans.allowance_spend", org_id=str(org_id), metric=metric, units=covered)
    return covered


# ------------------------------------------------------------------------------------
# Reporting and pricing
# ------------------------------------------------------------------------------------
async def usage(
    session: AsyncSession, org_id: uuid.UUID, *, today: date | None = None
) -> dict:
    """What the customer sees: allowance, usage and what is left. No plan -> no metrics at
    all, because an org without a plan has no allowance to report. Prices never appear here
    or anywhere else in this module."""
    set_org_context(session, org_id)
    org, plan = await _load_org_plan(session, org_id)
    if org is None or plan is None:
        return {
            "plan_code": None,
            "plan_name": None,
            "period_start": None,
            "period_end": None,
            "metrics": {},
        }

    period_start, period_end = period_for(org.plan_started_at, today or _today())
    rows = await _allowances(session, org_id, period_start)
    seeded = _included(plan)
    metrics: dict[str, dict[str, int]] = {}
    for metric in ALLOWANCE_METRICS:
        row = rows.get(metric)
        # Before the first unit is taken there is no row yet; the plan's number is still the
        # truth, and reporting 0 there would look like a bug to the customer.
        included = int(row.included_units) if row is not None else seeded[metric]
        used = int(row.used_units) if row is not None else 0
        metrics[metric] = {
            "included": included,
            "used": used,
            "remaining": max(included - used, 0),
        }
    return {
        "plan_code": org.plan_code,
        "plan_name": plan.name,
        "period_start": period_start,
        "period_end": period_end,
        "metrics": metrics,
    }


def overage_rate(plan: Plan | None, metric: str) -> int | None:
    """The plan's customer price in micros for one overage unit, or None when the plan does
    not price that metric - the caller then falls back to the rate card. A negative or
    unparsable value is bad data, and "no price here" beats a nonsense price."""
    if plan is None:
        return None
    rates = plan.overage_rates if isinstance(plan.overage_rates, dict) else {}
    raw = rates.get(metric)
    if raw is None:
        return None
    try:
        micros = int(raw)
    except (TypeError, ValueError):
        return None
    return micros if micros >= 0 else None


# ------------------------------------------------------------------------------------
# Sample plan catalogue
# ------------------------------------------------------------------------------------
SAMPLE_PLAN_CODES: tuple[str, ...] = ("starter", "standard", "professional")

#: SAMPLE plans. PRICING IS NOT DECIDED, and two fields say so explicitly:
#:
#:   monthly_price_micros = 0   a PLACEHOLDER, not a price. Zero must never be presented to
#:                              a customer as "free" - it means "nobody has set this yet".
#:   stripe_price_id = None     a PLACEHOLDER. It is also the safety catch: a plan with no
#:                              Stripe price id CANNOT be checked out (the route refuses and
#:                              names the plan), so a $0 plan physically cannot be sold while
#:                              this is unset. The operator supplies real `price_...` ids.
#:
#: The allowance and overage numbers below are illustrative shapes - escalating inclusions,
#: gently decreasing per-unit overage - so the plumbing has something to exercise. They are
#: not a commercial proposal.
_SAMPLE_PLANS: dict[str, dict] = {
    "starter": {
        "name": "Starter",
        "included": {"sms_segments": 500, "voice_minutes": 300, "numbers": 1, "seats": 3},
        "overage_rates": {
            "sms_segments": 12_000,
            "voice_minutes": 12_000,
            "numbers": 1_500_000,
        },
    },
    "standard": {
        "name": "Standard",
        "included": {"sms_segments": 2_000, "voice_minutes": 1_000, "numbers": 3, "seats": 10},
        "overage_rates": {
            "sms_segments": 11_000,
            "voice_minutes": 11_000,
            "numbers": 1_250_000,
        },
    },
    "professional": {
        "name": "Professional",
        "included": {
            "sms_segments": 10_000,
            "voice_minutes": 5_000,
            "numbers": 10,
            "seats": 50,
        },
        "overage_rates": {
            "sms_segments": 10_000,
            "voice_minutes": 10_000,
            "numbers": 1_000_000,
        },
    },
}


async def seed_sample_plans(session: AsyncSession) -> list[str]:
    """Create any of the three sample plans that do not exist yet. Returns the codes created.

    Idempotency is per-row existence, like services/defaults.seed_org_defaults - there is no
    marker row and no version. An existing plan is left COMPLETELY alone: the operator edits
    these rows by hand (that is how real prices and Stripe price ids arrive), and a re-seed
    that "refreshed" them would silently revert pricing to the placeholders and take working
    checkouts offline. Adding a NEW code to the catalogue is therefore the only thing a
    re-run can ever do.

    Does not commit - the caller owns the transaction. `plans` is platform-wide, not
    tenant-scoped, so no org context is involved.
    """
    created: list[str] = []
    for code in SAMPLE_PLAN_CODES:
        if await session.get(Plan, code) is not None:
            continue
        spec = _SAMPLE_PLANS[code]
        session.add(
            Plan(
                code=code,
                name=spec["name"],
                # Both placeholders; see _SAMPLE_PLANS above.
                monthly_price_micros=0,
                stripe_price_id=None,
                included=dict(spec["included"]),
                overage_rates=dict(spec["overage_rates"]),
                is_active=True,
            )
        )
        created.append(code)
    if created:
        log.info("plans.sample_plans_seeded", codes=created)
    return created


async def bootstrap_sample_plans() -> list[str]:
    """Startup hook: seed the sample catalogue, on its own session, NEVER raising.

    Called once from the app lifespan (app/main.py). Safe on every boot because
    `seed_sample_plans` only inserts codes that are absent - an operator's edited prices and
    Stripe price ids are never touched, so a redeploy cannot quietly revert pricing.

    Failure is logged and swallowed, deliberately. The catalogue feeds a plan picker; missing
    it degrades one screen. Making it fatal would take auth, messaging, calls and webhooks
    offline - and crash-loop the API - because a placeholder row could not be written, or
    because the deploy reached this before its migration. The error is logged at error level
    with the traceback so it is alertable rather than silent.

    Returns the codes created (empty when nothing was needed, and also when seeding failed).
    """
    from app.db.session import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            created = await seed_sample_plans(session)
            if created:
                await session.commit()
            return created
    except Exception:
        log.exception("plans.sample_plans_seed_failed")
        return []
