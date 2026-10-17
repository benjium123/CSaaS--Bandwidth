# ruff: noqa: F811 - pytest fixtures imported from test_ops_console
"""Switchboard > Profit: charged, carrier cost (Telnyx actual over estimate), Stripe fees and
fixed costs, by day and service, for all workspaces or one."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app.db.base import set_org_context
from app.models import BillingPayment, CreditLedgerEntry, FixedCost, OrgNumber
from app.models.billing_v2 import TelnyxCostDaily
from app.models.spend import ProviderSpendDaily
from app.services import profit_daily
from tests.conftest import auth_headers
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401

DAY = date(2026, 10, 15)
AT = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)


async def _seed(session):
    a = await _new_org(session, "Profit A")
    b = await _new_org(session, "Profit B")
    set_org_context(session, a)
    for seq, (ref, amount) in enumerate(
        [
            (f"call:{uuid.uuid4()}:voice", -162_000),
            (f"call:{uuid.uuid4()}:transcript", -20_000),
            (f"rec:{uuid.uuid4()}", -30_000),
            (f"num:{uuid.uuid4()}:2026-10-15", -2_499_000),
        ],
        start=1,
    ):
        session.add(CreditLedgerEntry(
            id=uuid.uuid4(), org_id=a, entry_type="usage", seq=seq, amount_micros=amount,
            balance_after_micros=0, reference=ref, created_at=AT, updated_at=AT,
        ))
    # A $15 number charge refunded to the balance the same day: not revenue.
    num_ref = f"num:{uuid.uuid4()}:2026-10-15"
    session.add(CreditLedgerEntry(
        id=uuid.uuid4(), org_id=a, entry_type="usage", seq=10, amount_micros=-15_000_000,
        balance_after_micros=0, reference=num_ref, created_at=AT, updated_at=AT,
    ))
    session.add(CreditLedgerEntry(
        id=uuid.uuid4(), org_id=a, entry_type="refund", seq=11, amount_micros=15_000_000,
        balance_after_micros=0, reference=f"refund:{num_ref}", created_at=AT, updated_at=AT,
    ))
    # A card refund reference is not a usage charge: left to the payments section.
    session.add(CreditLedgerEntry(
        id=uuid.uuid4(), org_id=a, entry_type="refund", seq=12, amount_micros=1_000_000,
        balance_after_micros=0, reference="refund:pi_123", created_at=AT, updated_at=AT,
    ))
    session.add(ProviderSpendDaily(
        id=uuid.uuid4(), org_id=a, period_date=DAY, provider="signalwire",
        metric="voice_min_out", quantity=18, cost_micros=144_000, created_at=AT, updated_at=AT,
    ))
    session.add(ProviderSpendDaily(
        id=uuid.uuid4(), org_id=a, period_date=DAY, provider="signalwire",
        metric="number_mrc", quantity=3, cost_micros=97_000, created_at=AT, updated_at=AT,
    ))
    # Number rental is booked whole on the renewal day (bought Sep 15 -> renews Oct 15);
    # the spread-out number_mrc rows above are ignored. One bought Sep 20 does not renew today.
    for e164, bought in (("+12145550101", 15), ("+12145550102", 20)):
        session.add(OrgNumber(
            id=uuid.uuid4(), org_id=a, e164=e164, carrier="signalwire", status="active",
            is_active=True, monthly_cost_cents=100,
            purchased_at=datetime(2026, 9, bought, 9, 0, tzinfo=timezone.utc),
        ))
    session.add(BillingPayment(
        id=uuid.uuid4(), org_id=a, kind="topup", state="paid", paid_micros=15_000_000,
        list_micros=15_000_000, stripe_fee_micros=735_000, paid_at=AT, created_at=AT,
        updated_at=AT,
    ))
    await session.commit()

    set_org_context(session, b)
    session.add(CreditLedgerEntry(
        id=uuid.uuid4(), org_id=b, entry_type="usage", seq=1, amount_micros=-120_000,
        balance_after_micros=0, reference=f"call:{uuid.uuid4()}:voice", created_at=AT,
        updated_at=AT,
    ))
    # Telnyx estimate for B's calls is replaced by Telnyx's own bill for the same day.
    session.add(ProviderSpendDaily(
        id=uuid.uuid4(), org_id=b, period_date=DAY, provider="telnyx",
        metric="voice_min_out", quantity=10, cost_micros=50_000, created_at=AT, updated_at=AT,
    ))
    session.add(TelnyxCostDaily(
        id=uuid.uuid4(), org_id=b, period_date=DAY, record_type="voice", direction="outbound",
        quantity=11, cost_micros=55_000, created_at=AT,
    ))
    # The CRM's Telnyx traffic (no workspace) is not Ringlite's.
    session.add(TelnyxCostDaily(
        id=uuid.uuid4(), org_id=None, period_date=DAY, record_type="sms",
        direction="outbound", quantity=100, cost_micros=800_000, created_at=AT,
    ))
    session.add(FixedCost(
        id=uuid.uuid4(), name="VPS", monthly_micros=18_250_000,
        starts_on=date(2026, 1, 1), ends_on=None,
    ))
    await session.commit()
    return a, b


def _day(body, d=DAY):
    return next(x for x in body["days"] if x["date"] == d.isoformat())


async def test_one_workspace_by_service(session):
    a, _ = await _seed(session)
    body = await profit_daily.daily(session, DAY, DAY, a)
    day = _day(body)
    calls = day["services"]["calls"]
    assert calls["charged"] == 162_000 and calls["carrier_cost"] == 144_000
    assert calls["units"] == 18 and calls["profit"] == 18_000
    assert day["services"]["transcription"]["charged"] == 20_000
    assert day["services"]["recording"]["charged"] == 30_000
    numbers = day["services"]["numbers"]
    assert numbers["charged"] == 2_499_000 and numbers["carrier_cost"] == 1_000_000
    assert numbers["units"] == 1
    # A top-up's Stripe fee is a cost of the day but of no one service.
    assert day["unallocated_stripe_fees"] == 735_000
    assert day["total"]["stripe_fees"] == 735_000
    assert day["fixed_costs"] == 0  # fixed costs only in the all-workspaces view
    assert day["total"]["charged"] == 162_000 + 20_000 + 30_000 + 2_499_000
    assert day["total"]["profit"] == (
        day["total"]["charged"] - 144_000 - 1_000_000 - 735_000
    )
    assert body["org_name"] == "Profit A"


async def test_all_workspaces_uses_telnyx_actual_and_fixed_costs(session):
    await _seed(session)
    body = await profit_daily.daily(session, DAY, DAY, None)
    day = _day(body)
    calls = day["services"]["calls"]
    # A's SignalWire estimate + B's Telnyx ACTUAL (55k), not B's estimate (50k).
    assert calls["carrier_cost"] == 144_000 + 55_000
    assert calls["charged"] == 162_000 + 120_000
    assert "sms" not in day["services"]  # the CRM's unattributed texts are excluded
    assert day["fixed_costs"] == round(18_250_000 * 12 / 365)
    assert body["totals"]["fixed_costs"] == day["fixed_costs"]


def test_service_mapping():
    assert profit_daily.service_for_reference("call:x:voice") == "calls"
    assert profit_daily.service_for_reference("call:x:transcript") == "transcription"
    assert profit_daily.service_for_reference("vmstt:x") == "transcription"
    assert profit_daily.service_for_reference("sum:x") == "summaries"
    assert profit_daily.service_for_reference("usage:x") == "ai"
    assert profit_daily.service_for_reference("weird") == "other"
    assert profit_daily.service_for_payment("topup") is None
    assert profit_daily.service_for_payment("voice_bundle") == "calls"
    assert profit_daily.service_for_payment("tendlc") == "tendlc"
    assert profit_daily.service_for_metric("mms_out") == "mms"


async def test_route_is_operator_only(ops, session, ops_settings):
    a, _ = await _seed(session)
    r = await ops.get("/api/v1/ops/console/profit-daily")
    assert r.status_code in (401, 403)
    token = await _operator(ops, session)
    r = await ops.get(
        f"/api/v1/ops/console/profit-daily?start={DAY}&end={DAY}&org_id={a}",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert _day(r.json())["services"]["calls"]["charged"] == 162_000


async def test_number_rental_only_on_renewal_day(session):
    a, _ = await _seed(session)
    body = await profit_daily.daily(session, date(2026, 10, 16), date(2026, 10, 20), a)
    by_day = {d["date"]: d["services"].get("numbers", {}).get("carrier_cost", 0)
              for d in body["days"]}
    assert by_day["2026-10-16"] == 0 and by_day["2026-10-20"] == 1_000_000
