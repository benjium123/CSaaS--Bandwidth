"""10DLC on the Profit page: its own service, paid minus the carrier fees derived from the
registrations; the 10DLC line of a plan invoice moves out of "Plans"."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import BillingPayment, TenDlcRegistration
from app.services import payments, profit_daily, refunds
from tests.test_tendlc_self_serve import _workspace

PAID_AT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
DAY = PAID_AT.date()
CENT = 10_000


def _invoice(org_id, *, kind="tendlc_fee", invoice_id="in_t1", paid=6050, lines=None):
    return {
        "id": invoice_id,
        "billing_reason": "subscription_create",
        "amount_paid": paid,
        "status_transitions": {"paid_at": int(PAID_AT.timestamp())},
        "payment_intent": f"pi_{invoice_id}",
        "parent": {
            "subscription_details": {
                "subscription": "sub_1",
                "metadata": {"kind": kind, "org_id": str(org_id)},
            }
        },
        "lines": {"data": lines or []},
    }


async def _reg(session, *, stage="active", reused=False, rejection=None, tier="standard"):
    org, brand, campaign, _ = await _workspace(session)
    set_org_context(session, org.id)
    brand.carrier_refs = {"telnyx": "B1"}
    session.add(
        TenDlcRegistration(
            org_id=org.id, brand_id=brand.id, campaign_id=campaign.id, fee_tier=tier,
            stage=stage, filing={"brand_reused": reused}, paid_at=PAID_AT, rejection=rejection,
        )
    )
    await session.commit()
    return org.id


async def _payment(session, org_id, *, kind="tendlc", paid_cents=6050, fee=0, detail=None):
    set_org_context(session, org_id)
    row = BillingPayment(
        id=uuid.uuid4(), org_id=org_id, kind=kind, state="paid", quantity=1,
        stripe_payment_intent_id=f"pi_{uuid.uuid4().hex[:8]}",
        list_micros=paid_cents * CENT, paid_micros=paid_cents * CENT, discount_micros=0,
        stripe_fee_micros=fee, paid_at=PAID_AT, detail=detail or {},
    )
    session.add(row)
    await session.commit()
    return row


async def _day(session, org_id, start=DAY, end=DAY):
    body = await profit_daily.daily(session, start, end, org_id)
    return body["service_totals"], body


async def test_tendlc_checkout_invoice_is_recorded_as_tendlc(session):
    org, *_ = await _workspace(session)
    assert await payments.record_subscription_invoice(session, _invoice(org.id))
    await session.commit()
    set_org_context(session, org.id)
    row = (await session.execute(sa.select(BillingPayment))).scalar_one()
    assert (row.kind, row.paid_micros) == ("tendlc", 6050 * CENT)
    assert "tendlc_micros" not in (row.detail or {})


async def test_plan_invoice_stores_its_tendlc_monthly_line(session):
    org, *_ = await _workspace(session)
    lines = [
        {"amount": 3600, "metadata": {"kind": "workspace_plan"}},
        {"amount": 1250, "metadata": {"kind": "tendlc_monthly"}},
    ]
    inv = _invoice(org.id, kind="workspace_plan", invoice_id="in_p1", paid=4850, lines=lines)
    assert await payments.record_subscription_invoice(session, inv)
    await session.commit()
    set_org_context(session, org.id)
    row = (await session.execute(sa.select(BillingPayment))).scalar_one()
    assert row.kind == "plan" and row.detail["tendlc_micros"] == 1250 * CENT


async def test_first_filing_that_went_active(session):
    org = await _reg(session, stage="active")
    await _payment(session, org, fee=200 * CENT)
    t, _ = await _day(session, org)
    row = t["tendlc"]
    assert row["charged"] == 6050 * CENT
    assert row["carrier_cost"] == (450 + 1500 + 3000) * CENT
    assert row["stripe_fees"] == 200 * CENT
    assert row["units"] == 1
    assert row["profit"] == row["charged"] - row["carrier_cost"] - row["stripe_fees"]


async def test_brand_reused_has_no_brand_cost(session):
    org = await _reg(session, stage="active", reused=True)
    await _payment(session, org)
    t, _ = await _day(session, org)
    assert t["tendlc"]["carrier_cost"] == (1500 + 3000) * CENT


async def test_rejected_campaign_still_costs_the_review(session):
    org = await _reg(session, stage="campaign_rejected")
    await _payment(session, org)
    t, _ = await _day(session, org)
    assert t["tendlc"]["carrier_cost"] == (450 + 1500 + 3000) * CENT


async def test_plan_row_10dlc_part_moves_out_of_plans(session):
    org = await _reg(session, stage="brand_approved")
    await _payment(
        session, org, kind="plan", paid_cents=4850, fee=97 * CENT,
        detail={"tendlc_micros": 1250 * CENT},
    )
    t, _ = await _day(session, org)
    assert t["plans"]["charged"] == 3600 * CENT
    assert t["tendlc"]["charged"] == 1250 * CENT
    assert t["plans"]["stripe_fees"] + t["tendlc"]["stripe_fees"] == 97 * CENT
    assert t["tendlc"]["stripe_fees"] == 97 * CENT * 1250 // 4850


async def test_plan_row_refund_is_shared_pro_rata(session):
    org = await _reg(session, stage="brand_approved")
    await _payment(
        session, org, kind="plan", paid_cents=4000,
        detail={"tendlc_micros": 1000 * CENT, "refund": {"refunded_micros": 2000 * CENT}},
    )
    t, _ = await _day(session, org)
    assert t["tendlc"]["charged"] == 500 * CENT
    assert t["plans"]["charged"] == 1500 * CENT


async def test_charge_refunded_on_a_tendlc_row(session):
    org = await _reg(session, stage="brand_approved")
    row = await _payment(session, org)
    event = {
        "type": "charge.refunded",
        "data": {"object": {
            "payment_intent": row.stripe_payment_intent_id, "amount": 6050,
            "amount_refunded": 6050,
        }},
    }
    assert await refunds.handle_charge_refunded(session, event)
    await session.commit()
    t, _ = await _day(session, org)
    assert t.get("tendlc", {}).get("charged", 0) == 0
    from app.services import credits

    assert await credits.balance(session, org) == 0


async def test_monthly_cost_on_paid_at_plus_90_days(session):
    org = await _reg(session, stage="active")
    d90 = DAY + timedelta(days=90)
    t, body = await _day(session, org, DAY, d90 + timedelta(days=35))
    by_day = {d["date"]: d["services"].get("tendlc") for d in body["days"]}
    assert by_day[d90.isoformat()]["carrier_cost"] == 1000 * CENT
    assert by_day[(d90 + timedelta(days=30)).isoformat()]["carrier_cost"] == 1000 * CENT
    assert by_day[(d90 - timedelta(days=30)).isoformat()] is None
    # a window that starts after the paid day still finds the monthly day
    t2, _ = await _day(session, org, d90, d90)
    assert t2["tendlc"]["carrier_cost"] == 1000 * CENT
    assert date(2026, 9, 29) == d90
