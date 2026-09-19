"""P37c packages: the allowance counters a plan grants, and what spills into overage.

The thing these tests actually defend is the seam: `take()` reports how many units the plan
COVERED, and whatever it did not cover is what the customer gets charged for. An off-by-one
there is not a cosmetic bug - it is a wrong invoice, every month, for every customer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Message, Org
from app.models.plans import Plan, PlanAllowance
from app.services import credits, telephony_billing
from app.services import plans as plans_svc


async def _plan(
    session,
    code: str = "growth",
    *,
    included: dict | None = None,
    overage: dict | None = None,
    is_active: bool = True,
) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        monthly_price_micros=49_000_000,
        included=included if included is not None else {"sms_segments": 10, "voice_minutes": 5},
        overage_rates=overage if overage is not None else {"sms_segments": 20_000},
        is_active=is_active,
    )
    session.add(plan)
    await session.commit()
    return plan


async def _org(
    session, *, plan_code: str | None = "growth", started: datetime | None = None
) -> Org:
    org = Org(
        id=uuid.uuid4(),
        name="Packaged Org",
        slug=f"pk-{uuid.uuid4().hex[:16]}",
        plan_code=plan_code,
        plan_started_at=started or datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    session.add(org)
    await session.commit()
    return org


async def _used(session, org_id, metric: str) -> int:
    set_org_context(session, org_id)
    return int(
        await session.scalar(
            sa.select(PlanAllowance.used_units).where(
                PlanAllowance.org_id == org_id, PlanAllowance.metric == metric
            )
        )
        or 0
    )


# ======================================================================================
# Billing periods
# ======================================================================================
def test_the_cycle_runs_from_the_plan_anniversary_not_the_calendar_month():
    start, end = plans_svc.period_for(datetime(2026, 1, 15, tzinfo=timezone.utc), date(2026, 3, 20))
    assert start == date(2026, 3, 15)
    assert end == date(2026, 4, 15)


def test_a_day_before_the_anniversary_is_still_last_months_cycle():
    start, end = plans_svc.period_for(datetime(2026, 1, 15, tzinfo=timezone.utc), date(2026, 3, 14))
    assert start == date(2026, 2, 15)
    assert end == date(2026, 3, 15)


def test_a_31st_anniversary_clamps_in_february_without_dragging_march_back():
    """The clamp must never become the next anchor: if February's 28th were reused as the
    anchor, every later cycle would start on the 28th and the customer would quietly lose
    three days of allowance a year."""
    anchor = datetime(2026, 1, 31, tzinfo=timezone.utc)
    feb_start, feb_end = plans_svc.period_for(anchor, date(2026, 2, 10))
    assert feb_start == date(2026, 1, 31)
    assert feb_end == date(2026, 2, 28)

    # The cycle that contains 1 April began on 31 March. Had February's clamped 28th become
    # the new anchor, this would come back as 28 March and the customer would quietly lose
    # three days of allowance every February, for good.
    mar_start, mar_end = plans_svc.period_for(anchor, date(2026, 4, 1))
    assert mar_start == date(2026, 3, 31)
    assert mar_end == date(2026, 4, 30)

    # And the anchor still reaches the 31st in a long month later in the year.
    may_start, _ = plans_svc.period_for(anchor, date(2026, 6, 1))
    assert may_start == date(2026, 5, 31)


# ======================================================================================
# ensure_period
# ======================================================================================
async def test_the_period_is_seeded_from_the_plans_included_allowances(session):
    await _plan(session, included={"sms_segments": 2_000, "voice_minutes": 1_000, "numbers": 3})
    org = await _org(session)

    rows = await plans_svc.ensure_period(session, org.id, today=date(2026, 3, 20))
    await session.commit()

    assert rows["sms_segments"].included_units == 2_000
    assert rows["voice_minutes"].included_units == 1_000
    assert rows["numbers"].included_units == 3
    assert all(row.used_units == 0 for row in rows.values())
    assert rows["sms_segments"].period_start == date(2026, 3, 15)


async def test_a_metric_the_plan_omits_is_included_as_zero(session):
    await _plan(session, included={"sms_segments": 100})
    org = await _org(session)

    rows = await plans_svc.ensure_period(session, org.id, today=date(2026, 3, 20))

    assert rows["numbers"].included_units == 0
    assert rows["voice_minutes"].included_units == 0


async def test_ensure_period_is_idempotent_and_never_resets_a_counter(session):
    await _plan(session)
    org = await _org(session)

    await plans_svc.ensure_period(session, org.id, today=date(2026, 3, 20))
    await plans_svc.take(session, org.id, "sms_segments", 4, today=date(2026, 3, 20))
    await session.commit()

    await plans_svc.ensure_period(session, org.id, today=date(2026, 3, 20))
    await session.commit()

    assert await _used(session, org.id, "sms_segments") == 4
    set_org_context(session, org.id)
    assert (
        await session.scalar(
            sa.select(sa.func.count(PlanAllowance.id)).where(PlanAllowance.org_id == org.id)
        )
    ) == len(plans_svc.ALLOWANCE_METRICS)


async def test_an_org_with_no_plan_has_no_allowance_at_all(session):
    org = await _org(session, plan_code=None)

    assert await plans_svc.ensure_period(session, org.id) == {}
    assert await plans_svc.take(session, org.id, "sms_segments", 3) == 0


async def test_an_inactive_plan_grants_nothing(session):
    await _plan(session, is_active=False)
    org = await _org(session)

    assert await plans_svc.plan_for(session, org.id) is None
    assert await plans_svc.take(session, org.id, "sms_segments", 1) == 0


# ======================================================================================
# take() - the seam that decides what the customer is charged for
# ======================================================================================
async def test_take_reports_the_units_covered_not_the_rows_updated(session):
    """The regression that matters most: a rowcount-shaped return would say "1 covered" for a
    3-segment text and bill the other two despite the allowance having room for all three."""
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)

    assert await plans_svc.take(session, org.id, "sms_segments", 3, today=date(2026, 3, 20)) == 3
    await session.commit()
    assert await _used(session, org.id, "sms_segments") == 3


async def test_a_batch_that_does_not_fit_takes_only_the_headroom(session):
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)
    today = date(2026, 3, 20)

    assert await plans_svc.take(session, org.id, "sms_segments", 8, today=today) == 8
    # 5 more asked for, only 2 left: the plan covers 2 and the caller charges 3 as overage.
    assert await plans_svc.take(session, org.id, "sms_segments", 5, today=today) == 2
    await session.commit()

    assert await _used(session, org.id, "sms_segments") == 10


async def test_an_exhausted_allowance_covers_nothing_further(session):
    await _plan(session, included={"sms_segments": 2})
    org = await _org(session)
    today = date(2026, 3, 20)

    assert await plans_svc.take(session, org.id, "sms_segments", 2, today=today) == 2
    assert await plans_svc.take(session, org.id, "sms_segments", 1, today=today) == 0
    await session.commit()
    assert await _used(session, org.id, "sms_segments") == 2


async def test_used_units_can_never_exceed_the_included_allowance(session):
    """The whole point of the bounded UPDATE. Whatever the interleaving, the counter must
    land exactly on the allowance and never above it - a unit taken past the allowance is a
    unit nobody was billed for."""
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)
    today = date(2026, 3, 20)

    covered = 0
    for _ in range(8):
        covered += await plans_svc.take(session, org.id, "sms_segments", 3, today=today)
    await session.commit()

    assert covered == 10
    assert await _used(session, org.id, "sms_segments") == 10


async def test_a_zero_or_negative_take_is_a_no_op(session):
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)

    assert await plans_svc.take(session, org.id, "sms_segments", 0) == 0
    assert await plans_svc.take(session, org.id, "sms_segments", -5) == 0
    await session.commit()
    assert await _used(session, org.id, "sms_segments") == 0


async def test_an_unknown_metric_is_never_covered(session):
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)

    assert await plans_svc.take(session, org.id, "carrier_pigeons", 1) == 0


async def test_a_new_period_starts_the_allowance_over(session):
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)

    assert await plans_svc.take(session, org.id, "sms_segments", 10, today=date(2026, 3, 20)) == 10
    await session.commit()
    # Next cycle: a fresh row, a fresh allowance, and last month's counter left as history.
    assert await plans_svc.take(session, org.id, "sms_segments", 6, today=date(2026, 4, 20)) == 6
    await session.commit()

    set_org_context(session, org.id)
    rows = (
        await session.execute(
            sa.select(PlanAllowance.period_start, PlanAllowance.used_units).where(
                PlanAllowance.org_id == org.id, PlanAllowance.metric == "sms_segments"
            )
        )
    ).all()
    assert {(row.period_start, row.used_units) for row in rows} == {
        (date(2026, 3, 15), 10),
        (date(2026, 4, 15), 6),
    }


async def test_taking_does_not_commit_the_callers_transaction(session):
    """take() runs in the middle of a send, so it must never commit: a send that fails later
    has to roll back as one piece, allowance and all.

    Proven through a sibling row the CALLER added before the take. If take() had committed,
    that row would survive the rollback. (The allowance rows themselves are written inside a
    SAVEPOINT, and aiosqlite keeps savepoint writes across an outer rollback where PostgreSQL
    discards them - so the sibling, not the counter, is what this can portably assert.)
    """
    await _plan(session, included={"sms_segments": 10})
    org = await _org(session)
    # Held before the rollback: afterwards every ORM attribute is expired, and reading one
    # would go back to the DB from a context that cannot await.
    org_id = org.id

    sibling_slug = f"sibling-{uuid.uuid4().hex[:12]}"
    session.add(Org(id=uuid.uuid4(), name="Mid-send work", slug=sibling_slug))
    await session.flush()

    assert await plans_svc.take(session, org_id, "sms_segments", 4, today=date(2026, 3, 20)) == 4
    await session.rollback()

    assert (
        await session.scalar(sa.select(sa.func.count(Org.id)).where(Org.slug == sibling_slug))
    ) == 0


# ======================================================================================
# usage() and overage_rate()
# ======================================================================================
async def test_usage_reports_included_used_and_remaining_and_no_prices(session):
    await _plan(session, included={"sms_segments": 100, "voice_minutes": 50})
    org = await _org(session)
    today = date(2026, 3, 20)
    await plans_svc.take(session, org.id, "sms_segments", 30, today=today)
    await session.commit()

    out = await plans_svc.usage(session, org.id, today=today)

    assert out["plan_code"] == "growth"
    assert out["period_start"] == date(2026, 3, 15)
    assert out["metrics"]["sms_segments"] == {"included": 100, "used": 30, "remaining": 70}
    assert out["metrics"]["voice_minutes"] == {"included": 50, "used": 0, "remaining": 50}
    # Customers see allowance, never cost, price or margin.
    flat = str(out)
    assert "micros" not in flat and "price" not in flat and "cost" not in flat


async def test_usage_before_any_traffic_still_reports_the_plans_allowance(session):
    await _plan(session, included={"sms_segments": 100})
    org = await _org(session)

    out = await plans_svc.usage(session, org.id, today=date(2026, 3, 20))

    assert out["metrics"]["sms_segments"]["included"] == 100
    assert out["metrics"]["sms_segments"]["remaining"] == 100


async def test_usage_for_an_org_with_no_plan_is_empty(session):
    org = await _org(session, plan_code=None)

    out = await plans_svc.usage(session, org.id)

    assert out["plan_code"] is None
    assert out["metrics"] == {}


@pytest.mark.parametrize(
    ("rates", "metric", "expected"),
    [
        ({"sms_segments": 20_000}, "sms_segments", 20_000),
        ({"sms_segments": 20_000}, "voice_minutes", None),
        ({"sms_segments": -1}, "sms_segments", None),
        ({"sms_segments": "nonsense"}, "sms_segments", None),
        ({}, "sms_segments", None),
    ],
)
def test_overage_rate_falls_back_to_the_rate_card_on_bad_data(rates, metric, expected):
    plan = Plan(code="x", name="X", included={}, overage_rates=rates, is_active=True)
    assert plans_svc.overage_rate(plan, metric) == expected


def test_overage_rate_with_no_plan_is_none():
    assert plans_svc.overage_rate(None, "sms_segments") is None


# ======================================================================================
# Allowance-first, credits-second: the whole point of selling a package
# ======================================================================================
SMS_OUT_MICROS = 10_000  # flat platform price: $0.01 per segment, carrier-independent
#                          (telephony_billing.PLATFORM_PRICE_MICROS["sms_out"])


async def _prepaid(session, org: Org, *, balance: int = 0) -> Org:
    org.telephony_prepaid = True
    org.telephony_prepaid_since = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    if balance:
        await credits.topup(session, org.id, balance, reference=f"tp-{uuid.uuid4()}")
        await session.commit()
    return org


def _message(org_id, *, segments: int = 1, direction: str = "outbound") -> Message:
    return Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=uuid.uuid4(),
        direction=direction,
        status="accepted",
        body="hello",
        carrier="telnyx",
        segment_count_est=segments,
    )


async def test_an_included_text_goes_out_on_an_empty_balance(session):
    """The package was already paid for. Refusing a text the customer has allowance for
    because their overage balance is empty would be charging them twice for one message."""
    await _plan(session, included={"sms_segments": 100})
    org = await _prepaid(session, await _org(session), balance=0)

    # No raise: the plan covers it.
    await telephony_billing.require_sms_credit(
        session, org.id, carrier="telnyx", segments=1, is_mms=False
    )
    assert (
        await telephony_billing.can_send_sms(session, org.id, _message(org.id)) is True
    )


async def test_an_org_with_no_allowance_left_is_still_refused_when_empty(session):
    await _plan(session, included={"sms_segments": 1})
    org = await _prepaid(session, await _org(session), balance=0)
    await plans_svc.take(session, org.id, "sms_segments", 1)
    await session.commit()

    with pytest.raises(telephony_billing.TelephonyCreditsError):
        await telephony_billing.require_sms_credit(
            session, org.id, carrier="telnyx", segments=1, is_mms=False
        )


async def test_a_covered_text_spends_allowance_and_no_credits(session):
    await _plan(session, included={"sms_segments": 100})
    org = await _prepaid(session, await _org(session), balance=1_000_000)

    await telephony_billing.charge_sms(session, org.id, _message(org.id, segments=2))
    await session.commit()

    assert await credits.balance(session, org.id) == 1_000_000, "no credits spent"
    assert await _used(session, org.id, "sms_segments") == 2


async def test_only_the_segments_past_the_allowance_are_charged(session):
    """Two segments included, a three-segment text sent: the plan eats two and the customer
    pays for exactly one."""
    await _plan(session, included={"sms_segments": 2})
    org = await _prepaid(session, await _org(session), balance=1_000_000)

    await telephony_billing.charge_sms(session, org.id, _message(org.id, segments=3))
    await session.commit()

    assert await credits.balance(session, org.id) == 1_000_000 - SMS_OUT_MICROS
    assert await _used(session, org.id, "sms_segments") == 2


async def test_an_org_on_no_package_is_billed_exactly_as_before(session):
    """The allowance path must be completely inert for the orgs that have no plan - which is
    every org until ops seeds packages."""
    org = await _prepaid(session, await _org(session, plan_code=None), balance=1_000_000)

    await telephony_billing.charge_sms(session, org.id, _message(org.id, segments=2))
    await session.commit()

    assert await credits.balance(session, org.id) == 1_000_000 - 2 * SMS_OUT_MICROS


async def test_extra_carrier_segments_meter_against_the_package_too(session):
    """A text the plan covered in full has no ledger entry. If the correction checked the
    ledger before the allowance, every under-estimated segment on a covered text would go
    unmetered - free traffic, every time the carrier counted higher than we did."""
    await _plan(session, included={"sms_segments": 100})
    org = await _prepaid(session, await _org(session), balance=1_000_000)
    message = _message(org.id, segments=1)
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()
    assert await _used(session, org.id, "sms_segments") == 1

    # The carrier says it was really three segments.
    message.segment_count_carrier = 3
    await telephony_billing.charge_segment_correction(session, org.id, message)
    await session.commit()

    assert await _used(session, org.id, "sms_segments") == 3
    assert await credits.balance(session, org.id) == 1_000_000, "the plan covered them"


async def test_extra_segments_past_the_allowance_are_charged(session):
    await _plan(session, included={"sms_segments": 2})
    org = await _prepaid(session, await _org(session), balance=1_000_000)
    message = _message(org.id, segments=1)
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()

    message.segment_count_carrier = 3
    await telephony_billing.charge_segment_correction(session, org.id, message)
    await session.commit()

    # Allowance had 1 left of 2; the third segment is charged.
    assert await _used(session, org.id, "sms_segments") == 2
    assert await credits.balance(session, org.id) == 1_000_000 - SMS_OUT_MICROS


async def test_a_send_from_before_the_gate_is_never_corrected(session):
    """Switching prepaid on must not reach back and charge for traffic that predates it."""
    await _plan(session, included={"sms_segments": 100})
    org = await _org(session)
    org.telephony_prepaid = True
    org.telephony_prepaid_since = datetime.now(timezone.utc)
    await session.commit()

    message = _message(org.id, segments=1)
    message.created_at = org.telephony_prepaid_since - timedelta(days=1)
    message.segment_count_carrier = 3

    await telephony_billing.charge_segment_correction(session, org.id, message)
    await session.commit()

    assert await _used(session, org.id, "sms_segments") == 0
    assert await credits.balance(session, org.id) == 0
