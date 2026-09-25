"""Billing v2 tests: platform pricing, bundles, and Stripe payment webhooks.

Money is integer micros. Tenant-scoped reads require set_org_context.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import Call, CreditLedgerEntry, Message, Org
from app.models.billing_v2 import BillingPayment, BillingRefusal, PlatformPrice
from app.services import bundles, credits, payments
from app.services import stripe_client
from app.services import telephony_billing
from app.services.telephony_billing import TelephonyCreditsError
from tests.conftest import auth_headers, make_org_with_number

SMS_OUT = 15_000
SMS_IN = 15_000
MMS_OUT = 35_000
MMS_IN = 35_000
VOICE_MIN_OUT = 12_000
VOICE_MIN_IN = 12_000
FAX_PAGE_OUT = 100_000
NUMBER_MRC = 15_000_000
VOICE_MIN = 12_000

WEBHOOK_URL = "/api/v1/webhooks/stripe"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _new_org(session, name: str = "Billing Org") -> Org:
    org = Org(id=uuid.uuid4(), name=name, slug=f"billing-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    return org


async def _enable(session, org_id, *, balance: int = 0, since: datetime | None = None) -> Org:
    org = await session.get(Org, uuid.UUID(str(org_id)))
    org.telephony_prepaid = True
    org.telephony_prepaid_since = since or (_now() - timedelta(hours=1))
    await session.commit()
    if balance:
        await credits.topup(session, org.id, balance, reference=f"topup-{uuid.uuid4()}")
        await session.commit()
    return org


async def _usage(session, org_id) -> list[CreditLedgerEntry]:
    set_org_context(session, uuid.UUID(str(org_id)))
    return list(
        (
            await session.execute(
                sa.select(CreditLedgerEntry)
                .where(CreditLedgerEntry.entry_type == "usage")
                .order_by(CreditLedgerEntry.seq)
            )
        ).scalars()
    )


async def _balance(session, org_id) -> int:
    return await credits.balance(session, uuid.UUID(str(org_id)))


def _outbound_call(org_id, **extra) -> Call:
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


def _payment_intent_event(intent_id, amount_received, metadata, *, event_id=None):
    return {
        "id": event_id or f"evt_{uuid.uuid4()}",
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": intent_id,
                "object": "payment_intent",
                "amount_received": amount_received,
                "currency": "usd",
                "metadata": metadata,
            }
        },
    }


# ==================================================================================
# Pricing
# ==================================================================================
async def test_unit_price_defaults(session):
    org = await _new_org(session)
    await _enable(session, org.id)

    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "sms_out") == 15_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "sms_in") == 15_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "mms_out") == 35_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "mms_in") == 35_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "voice_min_out") == 12_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "voice_min_in") == 12_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "fax_page_out") == 100_000
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "number_mrc") == 15_000_000


async def test_platform_price_overrides_constant(session):
    org = await _new_org(session)
    await _enable(session, org.id)

    session.add(
        PlatformPrice(
            metric="sms_out",
            price_micros=20_000,
        )
    )
    await session.commit()

    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "sms_out") == 20_000


def test_voice_price_micros_whole_minutes():
    assert telephony_billing.voice_price_micros(61, 12_000) == 24_000
    assert telephony_billing.voice_price_micros(60, 12_000) == 12_000
    assert telephony_billing.voice_price_micros(0, 12_000) == 0


def test_billable_seconds():
    org_id = uuid.uuid4()
    T = datetime(2026, 1, 1, tzinfo=timezone.utc)

    outbound = _outbound_call(org_id, direction="outbound", duration_seconds=61)
    assert telephony_billing.billable_seconds(outbound) == 61

    outbound_no_answer = _outbound_call(org_id, direction="outbound", duration_seconds=None)
    assert telephony_billing.billable_seconds(outbound_no_answer) == 0

    inbound_minimum = _outbound_call(
        org_id, direction="inbound", created_at=T, ended_at=T + timedelta(seconds=20)
    )
    assert telephony_billing.billable_seconds(inbound_minimum) == 60

    inbound_from_arrival = _outbound_call(
        org_id,
        direction="inbound",
        created_at=T,
        answered_at=T + timedelta(seconds=30),
        ended_at=T + timedelta(seconds=90),
        duration_seconds=60,
    )
    assert telephony_billing.billable_seconds(inbound_from_arrival) == 90


# ==================================================================================
# Call billing
# ==================================================================================
async def test_bill_finished_calls_inbound_uses_arrival_time(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    now = _now()

    set_org_context(session, org.id)
    session.add(
        _outbound_call(
            org.id,
            direction="inbound",
            status="completed",
            created_at=now - timedelta(minutes=3),
            answered_at=now - timedelta(seconds=150),
            ended_at=now - timedelta(seconds=90),
            duration_seconds=60,
        )
    )
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    assert await _balance(session, org.id) == 1_000_000 - 24_000

    rows = await _usage(session, org.id)
    assert len(rows) == 1
    assert -rows[0].amount_micros == 24_000


async def test_sms_refusal_records_billing_refusal(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=5_000)

    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_sms_credit(
            session, org.id, carrier="bandwidth", segments=1, is_mms=False
        )

    await session.flush()
    set_org_context(session, org.id)
    refusal = (await session.execute(sa.select(BillingRefusal))).scalar_one()

    assert refusal.kind == "sms"
    assert refusal.price_micros == 15_000
    assert refusal.balance_micros == 5_000


# ==================================================================================
# Bundle quotes and ledger
# ==================================================================================
def test_quote_from_list_sms_four():
    q = bundles.quote_from_list("sms", 4, 12_000_000)
    assert q["list"] == 48_000_000
    assert q["discount"] == 0
    assert q["paid"] == 48_000_000
    assert q["units"] == 4_000


def test_quote_from_list_sms_five_gets_discount():
    q = bundles.quote_from_list("sms", 5, 12_000_000)
    assert q["list"] == 60_000_000
    assert q["discount"] == 12_000_000
    assert q["paid"] == 48_000_000
    assert q["unit_paid"] == 9_600_000
    assert q["units"] == 5_000


def test_quote_from_list_mms_four_no_discount():
    q = bundles.quote_from_list("mms", 4, 3_000_000)
    assert q["discount"] == 0
    assert q["paid"] == 12_000_000


def test_quote_from_list_mms_five_gets_ten_percent():
    q = bundles.quote_from_list("mms", 5, 3_000_000)
    assert q["unit_paid"] == 2_700_000
    assert q["discount"] == 1_500_000
    assert q["paid"] == 13_500_000
    assert q["units"] == 500


def test_quote_from_list_rejects_invalid_qty():
    with pytest.raises(ValidationFailedError):
        bundles.quote_from_list("sms", 0, 12_000_000)

    with pytest.raises(ValidationFailedError):
        bundles.quote_from_list("sms", 501, 12_000_000)


async def test_bundle_credit_and_take_are_idempotent(session):
    org = await _new_org(session)

    await bundles.credit(session, org.id, "sms", 1000, reference="p1")
    await session.commit()

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 1000

    taken = await bundles.take(session, org.id, "sms", 3, reference="m1")
    await session.commit()
    assert taken == 3

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 997

    taken = await bundles.take(session, org.id, "sms", 3, reference="m1")
    await session.commit()
    assert taken == 3

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 997

    taken = await bundles.take(session, org.id, "sms", 2000, reference="m2")
    await session.commit()
    assert taken == 997

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 0

    await bundles.credit(session, org.id, "sms", 1000, reference="p1")
    await session.commit()

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 0


# ==================================================================================
# Bundle-aware SMS/MMS charging
# ==================================================================================
async def test_sms_bundle_covers_outbound_charge(session):
    org = await _new_org(session)
    await _enable(session, org.id)  # zero balance
    await bundles.credit(session, org.id, "sms", 1000, reference="bundle-1000")
    await session.commit()

    await telephony_billing.require_sms_credit(
        session, org.id, carrier="bandwidth", segments=2, is_mms=False
    )

    message = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="outbound",
        status="accepted",
        from_e164="+12145550108",
        to_e164="+19725550108",
        body="x",
        media=[],
        carrier="bandwidth",
        segment_count_est=2,
    )
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 998
    assert await _usage(session, org.id) == []
    assert await _balance(session, org.id) == 0


async def test_bundle_partially_covers_sms_and_balance_pays_rest(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    await bundles.credit(session, org.id, "sms", 1, reference="one-sms")
    await session.commit()

    message = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="outbound",
        status="accepted",
        from_e164="+12145550108",
        to_e164="+19725550108",
        body="x",
        media=[],
        carrier="bandwidth",
        segment_count_est=3,
    )
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "sms") == 0
    rows = await _usage(session, org.id)
    assert len(rows) == 1
    assert -rows[0].amount_micros == 30_000


async def test_mms_uses_mms_bundle_not_sms_bundle(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    await bundles.credit(session, org.id, "mms", 100, reference="mms-bundle")
    await bundles.credit(session, org.id, "sms", 7, reference="sms-bundle")
    await session.commit()

    message = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="outbound",
        status="accepted",
        from_e164="+12145550108",
        to_e164="+19725550108",
        body="pic",
        media=["https://x/y.jpg"],
        carrier="bandwidth",
        segment_count_est=1,
    )
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()

    set_org_context(session, org.id)
    assert await bundles.units(session, org.id, "mms") == 99
    assert await bundles.units(session, org.id, "sms") == 7


async def test_inbound_sms_charges_at_zero_balance_without_bundle(session):
    org = await _new_org(session)
    await _enable(session, org.id)

    message = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="inbound",
        status="received",
        from_e164="+19725550106",
        to_e164="+12145550106",
        body="hello",
        media=[],
        carrier="bandwidth",
        segment_count_est=1,
    )
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()

    assert await _balance(session, org.id) == -SMS_IN


# ==================================================================================
# Stripe webhook payments
# ==================================================================================
async def test_webhook_bundle_purchase(client, session, monkeypatch):
    org = await _new_org(session)
    row, _quote = await payments.start_bundle_payment(session, org.id, kind="sms", qty=5)
    await session.commit()

    metadata = {
        "org_id": str(org.id),
        "kind": "sms_bundle",
        "qty": "5",
        "payment_id": str(row.id),
    }
    event = _payment_intent_event(
        intent_id="pi_bundle_1",
        amount_received=5200,
        metadata=metadata,
        event_id="evt_bundle_1",
    )

    monkeypatch.setattr(
        stripe_client, "verify_webhook", lambda settings, payload, sig: event
    )
    r = await client.post(
        WEBHOOK_URL,
        json=event,
        headers={"Stripe-Signature": "sig_bundle_1"},
    )
    assert r.status_code in (200, 204), r.text
    await session.commit()

    session.expunge_all()
    set_org_context(session, org.id)
    payment = (
        await session.execute(sa.select(BillingPayment).where(BillingPayment.id == row.id))
    ).scalar_one()

    assert await bundles.units(session, org.id, "sms") == 5000
    assert payment.state == "paid"
    assert payment.paid_micros == 52_000_000
    assert payment.list_micros == 65_000_000
    assert payment.discount_micros == 13_000_000
    assert payment.units_credited == 5000
    assert payment.stripe_payment_intent_id == "pi_bundle_1"
    assert await credits.balance(session, org.id) == 0

    # Same payment intent, different event id: still idempotent.
    event2 = _payment_intent_event(
        intent_id="pi_bundle_1",
        amount_received=5200,
        metadata=metadata,
        event_id="evt_bundle_2",
    )
    monkeypatch.setattr(
        stripe_client, "verify_webhook", lambda settings, payload, sig: event2
    )
    r = await client.post(
        WEBHOOK_URL,
        json=event2,
        headers={"Stripe-Signature": "sig_bundle_2"},
    )
    assert r.status_code in (200, 204), r.text
    await session.commit()

    set_org_context(session, org.id)
    payment_again = (
        await session.execute(sa.select(BillingPayment).where(BillingPayment.id == row.id))
    ).scalar_one()
    assert payment_again.units_credited == 5000
    assert await bundles.units(session, org.id, "sms") == 5000

    count = (
        await session.execute(
            sa.select(sa.func.count(BillingPayment.id)).where(BillingPayment.org_id == org.id)
        )
    ).scalar_one()
    assert count == 1


async def test_webhook_topup_and_auto_recharge_kinds(client, session, monkeypatch):
    org = await _new_org(session)

    event = _payment_intent_event(
        intent_id="pi_topup_1",
        amount_received=2500,
        metadata={"org_id": str(org.id), "kind": "credit_topup"},
        event_id="evt_topup_1",
    )
    monkeypatch.setattr(
        stripe_client, "verify_webhook", lambda settings, payload, sig: event
    )
    r = await client.post(
        WEBHOOK_URL,
        json=event,
        headers={"Stripe-Signature": "sig_topup_1"},
    )
    assert r.status_code == 200, r.text
    await session.commit()

    set_org_context(session, org.id)
    payment = (
        await session.execute(
            sa.select(BillingPayment).where(
                BillingPayment.stripe_payment_intent_id == "pi_topup_1"
            )
        )
    ).scalar_one()
    assert payment.kind == "topup"
    assert payment.state == "paid"
    assert payment.paid_micros == 25_000_000
    assert await credits.balance(session, org.id) == 25_000_000

    event2 = _payment_intent_event(
        intent_id="pi_topup_2",
        amount_received=2500,
        metadata={
            "org_id": str(org.id),
            "kind": "credit_topup",
            "source": "auto_recharge",
        },
        event_id="evt_topup_2",
    )
    monkeypatch.setattr(
        stripe_client, "verify_webhook", lambda settings, payload, sig: event2
    )
    r = await client.post(
        WEBHOOK_URL,
        json=event2,
        headers={"Stripe-Signature": "sig_topup_2"},
    )
    assert r.status_code == 200, r.text
    await session.commit()

    set_org_context(session, org.id)
    payment2 = (
        await session.execute(
            sa.select(BillingPayment).where(
                BillingPayment.stripe_payment_intent_id == "pi_topup_2"
            )
        )
    ).scalar_one()
    assert payment2.kind == "auto_recharge"


# ==================================================================================
# Bundle catalog route
# ==================================================================================
async def test_get_bundle_catalog(client, session):
    token, org, _n = await make_org_with_number(
        client, "bundle-route@example.com", "Bundle Route", "+12145550910"
    )

    r = await client.get(
        "/api/v1/billing/bundles", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["kinds"]["sms"]["units_per_bundle"] == 1000
    assert body["kinds"]["mms"]["units_per_bundle"] == 100
    assert body["kinds"]["sms"]["list_micros"] == 13_000_000
    assert body["kinds"]["mms"]["list_micros"] == 3_000_000
