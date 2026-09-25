"""Call-minute bundles, the new list prices, and the one-time welcome credit.

Money is integer micros. Tenant-scoped reads require set_org_context.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app import config
from app.db.base import set_org_context
from app.models import Call, CreditLedgerEntry, Org
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
    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    assert await credits.balance(session, org.id) == 1_000_000


async def test_sweep_grants_existing_orgs_once(session):
    config.set_active_settings(make_settings(welcome_credit_micros=1_000_000))
    a = await _new_org(session, "Existing A")
    b = await _new_org(session, "Existing B")
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
