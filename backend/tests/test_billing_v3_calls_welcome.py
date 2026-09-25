"""Call-minute bundles, the new list prices, and the one-time welcome credit.

Money is integer micros. Tenant-scoped reads require set_org_context.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app import config
from app.db.base import set_org_context
from app.models import Call, CreditLedgerEntry, KycProfile, Org
from app.services import billing_ops, bundles, credits, telephony_billing
from tests.conftest import make_settings

VOICE_MIN = 12_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _new_org(session, name: str = "Calls Org") -> Org:
    org = Org(id=uuid.uuid4(), name=name, slug=f"calls-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    return org


async def _enable(session, org_id, *, balance: int = 0) -> Org:
    org = await session.get(Org, org_id)
    org.telephony_prepaid = True
    org.telephony_prepaid_since = _now() - timedelta(hours=1)
    await session.commit()
    if balance:
        await credits.topup(session, org_id, balance, reference=f"topup-{uuid.uuid4()}")
        await session.commit()
    return org


async def _give_minutes(session, org_id, minutes: int) -> None:
    await bundles.credit(session, org_id, "voice", minutes, reference=f"buy-{uuid.uuid4()}")
    await session.commit()


async def _approve(session, org_id) -> None:
    set_org_context(session, org_id)
    session.add(KycProfile(id=uuid.uuid4(), org_id=org_id, status="approved"))
    await session.commit()


def _call(org_id, **extra) -> Call:
    return Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction=extra.pop("direction", "outbound"),
        contact_e164="+19725550100",
        our_e164="+12145550100",
        carrier="bandwidth",
        status=extra.pop("status", "queued"),
        extra={},
        **extra,
    )


# ----------------------------------------------------------------------------------
# Prices and quotes
# ----------------------------------------------------------------------------------
async def test_new_list_prices(session):
    assert await telephony_billing.platform_price(session, "voice_min_out") == 12_000
    assert await telephony_billing.platform_price(session, "voice_min_in") == 12_000
    assert await telephony_billing.platform_price(session, "sms_bundle") == 13_000_000
    assert await telephony_billing.platform_price(session, "voice_bundle") == 10_000_000


def test_sms_bundle_at_13_gets_20_percent_at_five():
    q = bundles.quote_from_list("sms", 5, 13_000_000)
    assert q["unit_paid"] == 10_400_000
    assert q["paid"] == 52_000_000
    assert q["discount"] == 13_000_000


def test_call_bundle_quotes():
    one = bundles.quote_from_list("voice", 1, 10_000_000)
    assert one["paid"] == 10_000_000
    assert one["units"] == 1_000
    five = bundles.quote_from_list("voice", 5, 10_000_000)
    assert five["unit_paid"] == 9_000_000
    assert five["paid"] == 45_000_000
    assert five["discount"] == 5_000_000
    assert five["units"] == 5_000


# ----------------------------------------------------------------------------------
# Call billing draws on call-minute bundles before the balance
# ----------------------------------------------------------------------------------
async def test_finished_call_uses_bundle_minutes_then_balance(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    await _give_minutes(session, org.id, 2)
    now = _now()
    set_org_context(session, org.id)
    session.add(
        _call(
            org.id,
            status="completed",
            answered_at=now - timedelta(seconds=160),
            ended_at=now - timedelta(seconds=10),
            duration_seconds=150,  # 3 whole minutes
        )
    )
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    assert await bundles.units(session, org.id, "voice") == 0
    assert await credits.balance(session, org.id) == 1_000_000 - VOICE_MIN
    # Re-running bills nothing more.
    assert await telephony_billing.bill_finished_calls(session) == 0
    assert await credits.balance(session, org.id) == 1_000_000 - VOICE_MIN


async def test_call_fully_covered_by_bundle_charges_nothing(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await _give_minutes(session, org.id, 1_000)
    now = _now()
    set_org_context(session, org.id)
    session.add(
        _call(
            org.id,
            direction="inbound",
            status="completed",
            created_at=now - timedelta(minutes=5),
            ended_at=now - timedelta(minutes=1),
        )
    )
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    assert await bundles.units(session, org.id, "voice") == 996
    assert await credits.balance(session, org.id) == 0
    set_org_context(session, org.id)
    usage = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(CreditLedgerEntry.entry_type == "usage")
        )
    ).scalars().all()
    assert usage == []


async def test_bundle_minutes_let_calls_through_at_zero_balance(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    assert await telephony_billing.inbound_call_allowed(session, org.id, "bandwidth") is False

    await _give_minutes(session, org.id, 10)
    assert await telephony_billing.inbound_call_allowed(session, org.id, "bandwidth") is True

    call = _call(org.id)
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()
    # Outbound dial is not refused with $0 when bundle minutes are left.
    await telephony_billing.require_call_credit(session, org.id, call)


async def test_running_call_covered_by_bundle_is_not_cut(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await _give_minutes(session, org.id, 100)
    now = _now()
    set_org_context(session, org.id)
    session.add(
        _call(org.id, status="in_progress", answered_at=now - timedelta(minutes=3))
    )
    await session.commit()

    hung: list = []

    async def hangup(_session, c):
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 0
    assert hung == []


async def test_running_call_without_bundle_or_balance_is_cut(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    now = _now()
    set_org_context(session, org.id)
    session.add(
        _call(org.id, status="in_progress", answered_at=now - timedelta(minutes=3))
    )
    await session.commit()

    hung: list = []

    async def hangup(_session, c):
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 1
    assert len(hung) == 1


# ----------------------------------------------------------------------------------
# Welcome credit
# ----------------------------------------------------------------------------------
async def test_welcome_credit_is_granted_once(session):
    config.set_active_settings(make_settings(welcome_credit_micros=1_000_000))
    org = await _new_org(session)
    await _approve(session, org.id)
    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    assert await credits.balance(session, org.id) == 1_000_000


async def test_sweep_grants_existing_orgs_once(session):
    config.set_active_settings(make_settings(welcome_credit_micros=1_000_000))
    a = await _new_org(session, "Existing A")
    b = await _new_org(session, "Existing B")
    await _approve(session, a.id)
    await _approve(session, b.id)
    await billing_ops.grant_welcome_credit(session, a.id)
    await session.commit()

    granted = await billing_ops.grant_missing_welcome_credits(session)
    assert granted >= 1
    assert await credits.balance(session, a.id) == 1_000_000
    assert await credits.balance(session, b.id) == 1_000_000
    assert await billing_ops.grant_missing_welcome_credits(session) == 0
    assert await credits.balance(session, b.id) == 1_000_000


async def test_welcome_credit_off_when_zero(session):
    # The suite default is 0.
    org = await _new_org(session)
    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    assert await billing_ops.grant_missing_welcome_credits(session) == 0
    assert await credits.balance(session, org.id) == 0


# ----------------------------------------------------------------------------------
# Review fixes: the bundle is ONE shared pool across concurrent calls
# ----------------------------------------------------------------------------------
async def test_concurrent_calls_cannot_each_count_the_whole_bundle(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await _give_minutes(session, org.id, 10)
    now = _now()
    set_org_context(session, org.id)
    for _ in range(20):
        session.add(
            _call(org.id, status="in_progress", answered_at=now - timedelta(minutes=8))
        )
    await session.commit()

    hung: list = []

    async def hangup(_session, c):
        hung.append(c.id)

    # 20 calls x 8 minutes = 160 minutes against a 10-minute pool and $0: all are cut.
    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 20
    assert len(hung) == 20


async def test_two_calls_share_a_big_bundle_without_being_cut(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await _give_minutes(session, org.id, 100)
    now = _now()
    set_org_context(session, org.id)
    for _ in range(2):
        session.add(
            _call(org.id, status="in_progress", answered_at=now - timedelta(minutes=3))
        )
    await session.commit()

    async def hangup(_session, c):  # pragma: no cover - must not be called
        raise AssertionError("cut a covered call")

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 0


async def test_admission_needs_more_than_the_cutoff_headroom(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await _give_minutes(session, org.id, 1)
    # One free minute would be cut at the first tick, so the call is not taken.
    assert await telephony_billing.inbound_call_allowed(session, org.id, "bandwidth") is False
    call = _call(org.id)
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()
    import pytest

    with pytest.raises(telephony_billing.TelephonyCreditsError):
        await telephony_billing.require_call_credit(session, org.id, call)


async def test_minutes_used_by_other_live_calls_reduce_admission(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await _give_minutes(session, org.id, 10)
    now = _now()
    set_org_context(session, org.id)
    session.add(_call(org.id, status="in_progress", answered_at=now - timedelta(minutes=9)))
    await session.commit()
    # 10 minutes minus 9 already used by the live call = 1 free: not enough for a new call.
    assert await telephony_billing.inbound_call_allowed(session, org.id, "bandwidth") is False


# ----------------------------------------------------------------------------------
# Review fixes: alerts and the 10DLC customer quote
# ----------------------------------------------------------------------------------
async def test_zero_balance_with_bundle_units_is_low_not_exhausted(session, settings):
    from app.services import billing_alerts

    org = await _new_org(session)
    await _enable(session, org.id)
    assert await billing_alerts.evaluate(session, settings, org) == "exhausted"
    await _give_minutes(session, org.id, 50)
    assert await billing_alerts.evaluate(session, settings, org) == "low"


def test_tendlc_customer_quote_shows_totals_only():
    from app.services import tendlc

    q = tendlc.customer_quote("standard")
    assert q == {
        "fee_tier": "standard",
        "monthly_cents": 1000,
        "upfront_months": 3,
        "due_today_cents": 450 + 1500 + 500 + 3 * 1000,
    }


async def test_zero_balance_with_only_sms_units_stays_exhausted(session, settings):
    from app.services import billing_alerts

    org = await _new_org(session)
    await _enable(session, org.id)
    await bundles.credit(session, org.id, "sms", 1_000, reference=f"sms-{uuid.uuid4()}")
    await session.commit()
    assert await billing_alerts.evaluate(session, settings, org) == "exhausted"


# ----------------------------------------------------------------------------------
# Welcome credit waits for KYC approval
# ----------------------------------------------------------------------------------
async def test_welcome_credit_waits_for_kyc_approval(session):
    config.set_active_settings(make_settings(welcome_credit_micros=1_000_000))
    org = await _new_org(session, "Unverified")
    set_org_context(session, org.id)
    profile = KycProfile(id=uuid.uuid4(), org_id=org.id, status="draft")
    session.add(profile)
    await session.commit()

    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    await billing_ops.grant_missing_welcome_credits(session)
    assert await credits.balance(session, org.id) == 0

    set_org_context(session, org.id)
    profile.status = "approved"
    await session.commit()
    await billing_ops.grant_missing_welcome_credits(session)
    assert await credits.balance(session, org.id) == 1_000_000


async def test_no_kyc_profile_gets_no_welcome_credit(session):
    config.set_active_settings(make_settings(welcome_credit_micros=1_000_000))
    org = await _new_org(session, "No profile")
    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    assert await credits.balance(session, org.id) == 0


# ----------------------------------------------------------------------------------
# 911 calls, stuck calls and the maximum call length
# ----------------------------------------------------------------------------------
async def test_emergency_call_is_never_cut_at_zero_balance(session):
    org = await _enable(session, (await _new_org(session)).id)
    call = _call(org.id, status="in_progress", answered_at=_now() - timedelta(minutes=10))
    call.extra = {"emergency": True}
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    async def hangup(_session, c):  # pragma: no cover - must not be called
        raise AssertionError("cut a 911 call")

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 0


async def test_emergency_call_does_not_use_bundle_minutes(session):
    org = await _enable(session, (await _new_org(session)).id)
    await _give_minutes(session, org.id, 10)
    call = _call(org.id, status="in_progress", answered_at=_now() - timedelta(minutes=30))
    call.extra = {"emergency": True}
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()
    assert await telephony_billing.inbound_call_allowed(session, org.id, "bandwidth") is True


async def test_call_past_max_length_is_closed_and_billed_at_most_the_cap(session):
    org = await _enable(session, (await _new_org(session)).id, balance=50_000_000)
    call = _call(org.id, status="in_progress", answered_at=_now() - timedelta(hours=9))
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()
    calls_hung: list = []

    async def hangup(_session, c):
        calls_hung.append(c.id)  # the carrier never confirms: force-close path

    await telephony_billing.enforce_active_calls(session, hangup=hangup)
    set_org_context(session, org.id)
    closed = await session.get(Call, call.id)
    await session.refresh(closed)
    assert calls_hung == [call.id]
    assert closed.ended_at is not None
    assert telephony_billing.billable_seconds(closed) == telephony_billing.MAX_CALL_SECONDS


async def test_call_whose_room_is_gone_is_closed(session):
    org = await _enable(session, (await _new_org(session)).id, balance=50_000_000)
    call = _call(org.id, status="in_progress", answered_at=_now() - timedelta(minutes=5))
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    async def hangup(_session, c):
        raise RuntimeError("room already gone")

    async def gone(_session, c):
        return False

    org_id, call_id = org.id, call.id
    assert await telephony_billing.enforce_active_calls(session, hangup=hangup, is_live=gone) == 0
    set_org_context(session, org_id)
    closed = await session.get(Call, call_id, populate_existing=True)
    assert closed.ended_at is not None


async def test_live_or_unknown_room_is_left_alone(session):
    org = await _enable(session, (await _new_org(session)).id, balance=50_000_000)
    set_org_context(session, org.id)
    calls = [
        _call(org.id, status="in_progress", answered_at=_now() - timedelta(minutes=5))
        for _ in range(2)
    ]
    session.add_all(calls)
    await session.commit()
    answers = {calls[0].id: True, calls[1].id: None}

    async def is_live(_session, c):
        return answers[c.id]

    async def hangup(_session, c):  # pragma: no cover - must not be called
        raise AssertionError("closed a live call")

    await telephony_billing.enforce_active_calls(session, hangup=hangup, is_live=is_live)
    for c in calls:
        set_org_context(session, org.id)
        row = await session.get(Call, c.id)
        await session.refresh(row)
        assert row.ended_at is None


def test_billable_seconds_never_exceeds_the_cap():
    call = Call(direction="outbound", duration_seconds=20 * 3600)
    assert telephony_billing.billable_seconds(call) == telephony_billing.MAX_CALL_SECONDS
