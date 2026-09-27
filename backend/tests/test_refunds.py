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
