"""PNL metrics, AI usage cost, fixed cost CRUD, and telnyx cost tests."""

from __future__ import annotations

import types
import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import BillingPayment
from app.models.billing import AiUsageEvent
from app.models.billing_v2 import TelnyxCostDaily
from app.services.console import _fixed_cost_micros
from tests.conftest import auth_headers
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401


def _fixed_costs_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("fixed_costs"), list):
        return payload["fixed_costs"]
    return []


def _fixed_cost_id(payload):
    if isinstance(payload, dict):
        if "id" in payload:
            return payload["id"]
        nested = payload.get("fixed_cost")
        if isinstance(nested, dict) and "id" in nested:
            return nested["id"]
    raise AssertionError(f"could not find fixed cost id in {payload!r}")


async def test_ai_cost_reduces_cash_profit(ops, session, ops_settings):
    token = await _operator(ops, session)
    A = await _new_org(session, "AI Customer")
    now = datetime.now(timezone.utc)

    set_org_context(session, A)
    session.add(
        BillingPayment(
            id=uuid.uuid4(),
            org_id=A,
            kind="sms_bundle",
            state="paid",
            quantity=1,
            list_micros=10_000_000,
            paid_micros=10_000_000,
            discount_micros=0,
            stripe_fee_micros=320_000,
            units_credited=1,
            paid_at=now,
            stripe_payment_intent_id=f"pi_ai_{uuid.uuid4().hex[:12]}",
        )
    )
    session.add(
        AiUsageEvent(
            id=uuid.uuid4(),
            org_id=A,
            occurred_at=now,
            provider="deepgram",
            kind="stt",
            metric="audio_seconds",
            quantity=1,
            cost_micros=250_000,
            price_micros=300_000,
            source="api",
            idempotency_key=f"ai_{uuid.uuid4()}",
        )
    )
    await session.commit()

    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    org = next(o for o in body["orgs"] if o["org_id"] == str(A))
    metrics = org["metrics"]

    assert metrics["ai_cost"] == 250_000
    assert metrics["provider_cost"] == metrics["carrier_cost"] + 250_000
    assert metrics["cash_profit"] == 10_000_000 - 320_000 - metrics["provider_cost"]
    assert body["totals"]["ai_cost"] >= 250_000


def test_fixed_cost_pro_rating_unit():
    start = date(2024, 1, 1)
    end = start + timedelta(days=29)  # inclusive: 30 days
    monthly = 18_000_000

    full = types.SimpleNamespace(
        monthly_micros=monthly,
        starts_on=start,
        ends_on=end,
    )
    assert _fixed_cost_micros([full], start, end) == round(monthly * 12 / 365 * 30)

    mid = types.SimpleNamespace(
        monthly_micros=monthly,
        starts_on=start + timedelta(days=20),
        ends_on=end + timedelta(days=5),
    )
    assert _fixed_cost_micros([mid], start, end) == round(monthly * 12 / 365 * 10)

    before = types.SimpleNamespace(
        monthly_micros=monthly,
        starts_on=start - timedelta(days=10),
        ends_on=start - timedelta(days=1),
    )
    assert _fixed_cost_micros([before], start, end) == 0

    open_ended = types.SimpleNamespace(
        monthly_micros=monthly,
        starts_on=start + timedelta(days=20),
        ends_on=None,
    )
    assert _fixed_cost_micros([open_ended], start, end) == round(monthly * 12 / 365 * 10)


async def test_fixed_costs_crud_and_net_profit(ops, session, ops_settings):
    token = await _operator(ops, session)
    await _new_org(session, "Fixed Cost Org")

    today = datetime.now(timezone.utc).date()
    starts_on = today - timedelta(days=30)
    create_payload = {
        "name": "VPS",
        "monthly_micros": 18_000_000,
        "starts_on": starts_on.isoformat(),
    }

    r = await ops.post(
        "/api/v1/ops/console/fixed-costs",
        json=create_payload,
        headers=auth_headers(token),
    )
    assert r.status_code == 201, r.text
    cost_id = _fixed_cost_id(r.json())

    r = await ops.get("/api/v1/ops/console/fixed-costs", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    costs = _fixed_costs_list(r.json())
    assert any(c.get("name") == "VPS" for c in costs)

    end = today
    start = end - timedelta(days=9)
    r = await ops.get(
        "/api/v1/ops/console/orgs",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    totals = r.json()["totals"]
    assert totals["fixed_costs"] == round(18_000_000 * 12 / 365 * 10)
    assert totals["net_profit"] == (
        totals["cash_profit"]
        - totals["fixed_costs"]
        - totals["unattributed_carrier_cost"]
    )

    bad_ends = starts_on - timedelta(days=1)
    r = await ops.put(
        f"/api/v1/ops/console/fixed-costs/{cost_id}",
        json={
            "name": "VPS",
            "monthly_micros": 18_000_000,
            "starts_on": starts_on.isoformat(),
            "ends_on": bad_ends.isoformat(),
        },
        headers=auth_headers(token),
    )
    assert r.status_code == 422, r.text

    r = await ops.put(
        f"/api/v1/ops/console/fixed-costs/{cost_id}",
        json={"name": "VPS", "monthly_micros": 20_000_000, "starts_on": starts_on.isoformat()},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text

    r = await ops.get("/api/v1/ops/console/fixed-costs", headers=auth_headers(token))
    costs = _fixed_costs_list(r.json())
    updated = next(c for c in costs if c["id"] == cost_id)
    assert updated["monthly_micros"] == 20_000_000

    r = await ops.delete(
        f"/api/v1/ops/console/fixed-costs/{cost_id}",
        headers=auth_headers(token),
    )
    assert r.status_code == 204, r.text

    r = await ops.get("/api/v1/ops/console/fixed-costs", headers=auth_headers(token))
    assert _fixed_costs_list(r.json()) == []


async def test_unattributed_telnyx_cost_is_platform_only(ops, session, ops_settings):
    token = await _operator(ops, session)
    A = await _new_org(session, "Telnyx Org")
    today = datetime.now(timezone.utc).date()

    set_org_context(session, A)
    session.add(
        TelnyxCostDaily(
            id=uuid.uuid4(),
            org_id=A,
            period_date=today,
            cost_micros=400_000,
            record_type="call",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()

    await session.execute(
        sa.insert(TelnyxCostDaily)
        .values(
            id=uuid.uuid4(),
            org_id=None,
            period_date=today,
            cost_micros=1_000_000,
            record_type="call",
            created_at=datetime.now(timezone.utc),
        )
        .execution_options(allow_unscoped=True)
    )
    await session.commit()

    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()

    org = next(o for o in body["orgs"] if o["org_id"] == str(A))
    assert org["metrics"]["telnyx_actual_cost"] == 400_000
    assert body["totals"]["unattributed_carrier_cost"] == 1_000_000
    assert body["totals"]["net_profit"] == (
        body["totals"]["cash_profit"]
        - body["totals"]["fixed_costs"]
        - body["totals"]["unattributed_carrier_cost"]
    )


async def test_reviewer_cannot_write_fixed_costs(ops, session, ops_settings):
    reviewer = await _operator(ops, session, email="reviewer-fc@example.com", role="reviewer")

    payload = {
        "name": "VPS",
        "monthly_micros": 18_000_000,
        "starts_on": datetime.now(timezone.utc).date().isoformat(),
    }

    r = await ops.post(
        "/api/v1/ops/console/fixed-costs",
        json=payload,
        headers=auth_headers(reviewer),
    )
    assert r.status_code == 403, r.text

    r = await ops.get("/api/v1/ops/console/fixed-costs", headers=auth_headers(reviewer))
    assert r.status_code == 200, r.text


async def test_accrual_revenue_counts_consumption_not_prepayment(ops, session, ops_settings):
    """P46 P&L part 2: accrual = usage + bundle units consumed (at the paid unit price) +
    direct payments (plan/fees); top-ups and bundle purchases are prepayments."""
    from app.models import BundleLedgerEntry
    from app.services import credits

    token = await _operator(ops, session)
    A = await _new_org(session, "Accrual Customer")
    now = datetime.now(timezone.utc)
    set_org_context(session, A)

    def pay(kind, paid, units=0):
        session.add(
            BillingPayment(
                id=uuid.uuid4(), org_id=A, kind=kind, state="paid", quantity=1,
                list_micros=paid, paid_micros=paid, discount_micros=0, stripe_fee_micros=0,
                units_credited=units, paid_at=now,
                stripe_payment_intent_id=f"pi_acc_{uuid.uuid4().hex[:12]}",
            )
        )

    pay("sms_bundle", 13_000_000, units=1000)  # $0.013 per SMS
    pay("topup", 5_000_000)  # prepayment: not revenue until used
    pay("plan", 29_000_000)  # subscription invoice: revenue when paid
    session.add_all(
        [
            BundleLedgerEntry(org_id=A, kind="sms", entry_type="purchase", seq=1,
                              delta_units=1000, balance_after_units=1000, reference="p1"),
            BundleLedgerEntry(org_id=A, kind="sms", entry_type="usage", seq=2,
                              delta_units=-100, balance_after_units=900, reference="u1"),
        ]
    )
    await session.commit()
    set_org_context(session, A)
    await credits.charge_usage(session, A, 200_000, reference="acc-usage-1", note="t")
    await session.commit()

    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    m = next(o for o in body["orgs"] if o["org_id"] == str(A))["metrics"]

    assert m["bundle_revenue_consumed"] == 1_300_000  # 100 units x $0.013
    assert m["direct_revenue"] == 29_000_000
    assert m["usage_revenue"] == 200_000
    assert m["accrual_revenue"] == 200_000 + 1_300_000 + 29_000_000
    assert m["paid"] == 47_000_000  # cash beside it: everything received
    assert m["accrual_profit"] == m["accrual_revenue"] - m["stripe_fees"] - m["provider_cost"]
    totals = body["totals"]
    assert totals["accrual_revenue"] >= m["accrual_revenue"]
    assert totals["net_accrual_profit"] == (
        totals["accrual_profit"] - totals["fixed_costs"] - totals["unattributed_carrier_cost"]
    )
