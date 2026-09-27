"""Refunds (services/refunds.py): a Stripe refund takes back only the UNUSED part of what
the payment granted, in proportion; the rest is an ops shortfall alert, never negative."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import BillingPayment, Org, SecurityAlert
from app.services import bundles, credits, refunds

CENT = 10_000


async def _org(session) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Refund Org", slug=f"refund-{org_id.hex[:8]}"))
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def _payment(session, org_id, *, kind: str, intent: str, paid_cents: int, **extra):
    set_org_context(session, org_id)
    row = BillingPayment(
        id=uuid.uuid4(),
        org_id=org_id,
        kind=kind,
        state="paid",
        stripe_payment_intent_id=intent,
        quantity=1,
        list_micros=paid_cents * CENT,
        paid_micros=paid_cents * CENT,
        discount_micros=0,
        paid_at=datetime.now(timezone.utc),
        **extra,
    )
    session.add(row)
    await session.commit()
    return row


def _event(intent: str, amount_cents: int, refunded_cents: int) -> dict:
    return {
        "type": "charge.refunded",
        "data": {
            "object": {
                "payment_intent": intent,
                "amount": amount_cents,
                "amount_refunded": refunded_cents,
            }
        },
    }


async def _refund(session, intent, amount_cents, refunded_cents) -> bool:
    handled = await refunds.handle_charge_refunded(
        session, _event(intent, amount_cents, refunded_cents)
    )
    await session.commit()
    return handled


async def _alerts(session, org_id) -> list[SecurityAlert]:
    return list(
        (
            await session.execute(
                sa.select(SecurityAlert)
                .where(SecurityAlert.kind == "refund_shortfall", SecurityAlert.org_id == org_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars()
    )


async def test_full_refund_takes_back_only_the_unused_credit_and_flags_the_rest(session):
    org_id = await _org(session)
    await credits.topup(session, org_id, 50 * 100 * CENT, reference="pi_a")
    await credits.charge_usage(session, org_id, 20 * 100 * CENT, reference="use-1")
    row = await _payment(session, org_id, kind="topup", intent="pi_a", paid_cents=5000)

    assert await _refund(session, "pi_a", 5000, 5000)

    assert await credits.balance(session, org_id) == 0  # never negative
    await session.refresh(row)
    assert row.state == "refunded"
    assert row.detail["refund"]["taken_credit_micros"] == 30 * 100 * CENT
    assert row.detail["refund"]["shortfall_credit_micros"] == 20 * 100 * CENT
    alerts = await _alerts(session, org_id)
    assert len(alerts) == 1
    assert alerts[0].detail["shortfall"] == {"credit_micros": 20 * 100 * CENT}


async def test_partial_refunds_take_back_each_new_share_once(session):
    org_id = await _org(session)
    await credits.topup(session, org_id, 50 * 100 * CENT, reference="pi_b")
    row = await _payment(session, org_id, kind="topup", intent="pi_b", paid_cents=5000)

    await _refund(session, "pi_b", 5000, 1000)  # $10 back
    assert await credits.balance(session, org_id) == 40 * 100 * CENT
    await _refund(session, "pi_b", 5000, 3000)  # cumulative $30: $20 more
    assert await credits.balance(session, org_id) == 20 * 100 * CENT
    await _refund(session, "pi_b", 5000, 3000)  # replayed: nothing more
    assert await credits.balance(session, org_id) == 20 * 100 * CENT

    await session.refresh(row)
    assert row.state == "paid"  # not fully refunded
    assert await _alerts(session, org_id) == []


async def test_bundle_refund_takes_back_the_proportional_unused_units(session):
    org_id = await _org(session)
    await bundles.credit(session, org_id, "sms", 1000, reference="pi:pi_c")
    await bundles.take(session, org_id, "sms", 400, reference="sms-use")
    await session.commit()
    await _payment(session, org_id, kind="sms_bundle", intent="pi_c", paid_cents=1300)

    await _refund(session, "pi_c", 1300, 650)  # half back: 500 of the 1,000 units

    assert await bundles.units(session, org_id, "sms") == 100
    assert await _alerts(session, org_id) == []


async def test_custom_invoice_refund_reverses_every_line(session):
    org_id = await _org(session)
    await credits.topup(session, org_id, 25 * 100 * CENT, reference="invoice:in_1:0")
    await bundles.credit(session, org_id, "voice", 1000, reference="invoice:in_1:1")
    await session.commit()
    await _payment(
        session,
        org_id,
        kind="invoice",
        intent="pi_d",
        paid_cents=3500,
        stripe_checkout_id="in_1",
        detail={"lines": [{"type": "credit"}, {"type": "package", "package": "voice"}]},
    )

    await _refund(session, "pi_d", 3500, 3500)

    assert await credits.balance(session, org_id) == 0
    assert await bundles.units(session, org_id, "voice") == 0


async def test_a_credit_already_held_for_fraud_is_not_taken_twice(session):
    org_id = await _org(session)
    await credits.topup(session, org_id, 50 * 100 * CENT, reference="pi_e")
    await credits.topup(session, org_id, 10 * 100 * CENT, reference="other-topup")
    await credits.adjust(
        session, org_id, -50 * 100 * CENT, reference="fraud:pi_e", note="Held", created_by=None
    )
    await _payment(session, org_id, kind="topup", intent="pi_e", paid_cents=5000)

    await _refund(session, "pi_e", 5000, 5000)

    assert await credits.balance(session, org_id) == 10 * 100 * CENT
    assert await _alerts(session, org_id) == []


async def test_an_unknown_payment_is_not_ours(session):
    assert not await refunds.handle_charge_refunded(session, _event("pi_unknown", 100, 100))


# --- fees: the customer bears Stripe's fee -------------------------------------------------
FEE = 175 * CENT  # $1.75 on a $50 payment


async def _topup_paid(session, org_id, intent, dollars=50, fee=FEE):
    await credits.topup(session, org_id, dollars * 100 * CENT, reference=intent)
    return await _payment(
        session, org_id, kind="topup", intent=intent, paid_cents=dollars * 100,
        stripe_fee_micros=fee,
    )


async def test_manual_stripe_refund_is_grossed_up_by_the_card_fee(session):
    org_id = await _org(session)
    await _topup_paid(session, org_id, "pi_f")
    await credits.charge_usage(session, org_id, 20 * 100 * CENT, reference="use-f")

    await _refund(session, "pi_f", 5000, 2895)  # $28.95 back = $30 of credit

    assert await credits.balance(session, org_id) == 0
    assert await _alerts(session, org_id) == []


async def test_refundable_is_unused_paid_credit_less_the_fee_share_and_never_bundles(session):
    org_id = await _org(session)
    await _topup_paid(session, org_id, "pi_g")
    await credits.charge_usage(session, org_id, 20 * 100 * CENT, reference="use-g")
    await bundles.credit(session, org_id, "sms", 1000, reference="pi:pi_bundle")
    await session.commit()
    await _payment(session, org_id, kind="sms_bundle", intent="pi_bundle", paid_cents=1300,
                   stripe_fee_micros=68 * CENT)

    plan = await refunds.refundable(session, org_id)

    assert plan["credit_micros"] == 30 * 100 * CENT
    assert plan["fee_micros"] == 105 * CENT  # $1.75 x 30/50
    assert plan["refund_micros"] == 2895 * CENT
    assert [p["kind"] for p in plan["payments"]] == ["topup"]


async def test_refundable_waits_until_stripe_settles_the_fee(session):
    org_id = await _org(session)
    await _topup_paid(session, org_id, "pi_h", fee=None)

    plan = await refunds.refundable(session, org_id)

    assert plan["payments"] == []
    assert plan["waiting_on_fee_micros"] == 50 * 100 * CENT


async def test_refund_unused_credit_newest_first_then_the_webhook_takes_nothing_more(
    session, monkeypatch
):
    org_id = await _org(session)
    older = await _topup_paid(session, org_id, "pi_old")
    older.paid_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    await session.commit()
    await _topup_paid(session, org_id, "pi_new")  # paid now
    await credits.charge_usage(session, org_id, 70 * 100 * CENT, reference="use-lifo")
    calls: list[tuple[str, int]] = []

    async def fake_refund(settings, intent_id, cents, *, key, org_id):  # noqa: ARG001
        calls.append((intent_id, cents))
        return {"id": "re_1"}

    monkeypatch.setattr(refunds, "_stripe_refund", fake_refund)

    result = await refunds.refund_unused_credit(session, None, org_id, actor_user_id=None)

    # $30 left: all of it from the newest payment, less $1.05 of its fee.
    assert calls == [("pi_new", 2895)]
    assert result["failed"] is None
    assert await credits.balance(session, org_id) == 0

    await _refund(session, "pi_new", 5000, 2895)  # Stripe's webhook for that refund
    assert await credits.balance(session, org_id) == 0
    assert await _alerts(session, org_id) == []


async def test_a_failed_card_refund_gives_the_credit_back(session, monkeypatch):
    org_id = await _org(session)
    await _topup_paid(session, org_id, "pi_fail")

    async def boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("card_declined")

    monkeypatch.setattr(refunds, "_stripe_refund", boom)

    result = await refunds.refund_unused_credit(session, None, org_id, actor_user_id=None)

    assert result["refunded"] == []
    assert "card_declined" in result["failed"]["error"]
    assert await credits.balance(session, org_id) == 50 * 100 * CENT
    plan = await refunds.refundable(session, org_id)
    assert plan["credit_micros"] == 50 * 100 * CENT  # refundable again


async def test_pnl_takes_refunds_off_revenue_and_keeps_the_stripe_fee(session):
    from datetime import timedelta

    from app.services import console

    org_id = await _org(session)
    await _topup_paid(session, org_id, "pi_pnl")
    await _refund(session, "pi_pnl", 5000, 5000)  # full manual refund

    now = datetime.now(timezone.utc)
    metrics = await console.org_metrics(
        session, now - timedelta(days=1), now + timedelta(days=1),
        (now - timedelta(days=1)).date(), (now + timedelta(days=1)).date(), org_ids=[org_id],
    )
    m = console.finish(metrics[org_id])
    assert m["paid"] == 0
    assert m["refunded_cash"] == 50 * 100 * CENT
    assert m["stripe_fees"] == FEE
    assert m["cash_profit"] == -FEE
