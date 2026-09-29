"""In-app purchases with the saved card (services/inapp_pay.py, POST /billing/pay): the charge
carries Checkout's metadata, success is credited inline exactly once (the webhook afterwards is
a no-op), a retried request never charges twice, and declines / bank authentication / fraud
refusals credit nothing."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import PaymentMethod
from app.models.billing_v2 import BillingPayment
from app.services import bundles, credits, inapp_pay, payments, stripe_client
from tests.conftest import auth_headers, make_org_with_number

PAY_URL = "/api/v1/billing/pay"
QUOTE_URL = "/api/v1/billing/pay/quote"


async def _org_with_card(client, session, tag: str):  # noqa: ANN001, ANN202
    token, org, _n = await make_org_with_number(
        client,
        f"inapp-{tag}@example.com",
        f"Inapp {tag}",
        f"+1214555{uuid.uuid4().int % 10000:04d}",
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    session.add(
        PaymentMethod(
            id=uuid.uuid4(),
            org_id=org_id,
            stripe_customer_id="cus_inapp",
            stripe_payment_method_id="pm_inapp",
            brand="visa",
            last4="4242",
            is_default=True,
        )
    )
    await session.commit()
    return token, org, org_id


def _fake_charge(monkeypatch, *, status="succeeded", calls=None):  # noqa: ANN001, ANN202
    calls = calls if calls is not None else []

    async def charge(settings, **kwargs):  # noqa: ANN001, ANN202
        calls.append(kwargs)
        if status == "requires_action":
            return {"status": "requires_action", "code": "authentication_required"}
        if status == "failed":
            return {
                "status": "failed", "reason": "Your card was declined.", "code": "card_declined",
            }
        intent = {
            "id": f"pi_{kwargs['idempotency_key']}",
            "status": status,
            "amount_received": kwargs["amount_micros"] // 10_000,
            "metadata": dict(kwargs["metadata"]),
            "payment_method": kwargs["payment_method_id"],
            "customer": kwargs["customer_id"],
        }
        return {"status": status, "id": intent["id"], "intent": intent}

    async def not_risky(session, settings, intent):  # noqa: ANN001, ANN202
        return False

    monkeypatch.setattr(stripe_client, "charge_saved_card", charge)
    monkeypatch.setattr(payments, "refuse_risky_payment", not_risky)
    return calls


async def test_quote_shows_price_and_card(client, session):
    token, org, _ = await _org_with_card(client, session, "quote")
    r = await client.get(
        QUOTE_URL, params={"kind": "sms", "qty": 5}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paid_micros"] == 52_000_000
    assert body["list_micros"] == 65_000_000
    assert body["units"] == 5000
    assert body["card"] == {"brand": "visa", "last4": "4242"}


async def test_bundle_paid_inline_once_and_retry_never_charges_twice(client, session, monkeypatch):
    token, org, org_id = await _org_with_card(client, session, "bundle")
    calls = _fake_charge(monkeypatch)
    body = {"kind": "sms", "qty": 5, "request_id": "req-bundle-1"}
    r = await client.post(PAY_URL, json=body, headers=auth_headers(token, org["id"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    assert len(calls) == 1
    pid = inapp_pay.payment_id_for(org_id, "req-bundle-1")
    assert calls[0]["metadata"] == {
        "org_id": str(org_id), "kind": "sms_bundle", "qty": "5", "payment_id": str(pid),
    }
    assert calls[0]["amount_micros"] == 52_000_000
    assert calls[0]["payment_method_id"] == "pm_inapp"

    r = await client.post(PAY_URL, json=body, headers=auth_headers(token, org["id"]))
    assert r.json()["status"] == "succeeded"
    assert len(calls) == 1  # the paid row short-circuits: no second charge

    session.expunge_all()
    set_org_context(session, org_id)
    assert await bundles.units(session, org_id, "sms") == 5000
    row = await session.get(BillingPayment, pid)
    assert row.state == "paid" and row.units_credited == 5000


async def test_credit_topup_inline_then_webhook_is_noop(client, session, monkeypatch):
    token, org, org_id = await _org_with_card(client, session, "credit")
    calls = _fake_charge(monkeypatch)
    before = await credits.balance(session, org_id)
    r = await client.post(
        PAY_URL,
        json={"kind": "credit", "amount_micros": 25_000_000, "request_id": "req-credit-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.json()["status"] == "succeeded", r.text
    assert calls[0]["metadata"] == {
        "org_id": str(org_id), "kind": "credit_topup", "source": "in_app",
    }
    session.expunge_all()
    assert await credits.balance(session, org_id) == before + 25_000_000

    intent_id = f"pi_{calls[0]['idempotency_key']}"
    event = {
        "id": "evt_inapp_credit",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": intent_id, "amount_received": 2500,
                            "metadata": calls[0]["metadata"]}},
    }
    monkeypatch.setattr(stripe_client, "verify_webhook", lambda s, p, sig: event)
    r = await client.post(
        "/api/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,sig=1"}
    )
    assert r.status_code in (200, 204), r.text
    session.expunge_all()
    assert await credits.balance(session, org_id) == before + 25_000_000
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(sa.func.count(BillingPayment.id)).where(
                BillingPayment.stripe_payment_intent_id == intent_id
            )
        )
    ).scalar_one()
    assert rows == 1


async def test_declined_card_credits_nothing(client, session, monkeypatch):
    token, org, org_id = await _org_with_card(client, session, "declined")
    _fake_charge(monkeypatch, status="failed")
    r = await client.post(
        PAY_URL,
        json={"kind": "mms", "qty": 1, "request_id": "req-declined-1"},
        headers=auth_headers(token, org["id"]),
    )
    body = r.json()
    assert body["status"] == "declined" and body["message"] == "Your card was declined."
    session.expunge_all()
    set_org_context(session, org_id)
    assert await bundles.units(session, org_id, "mms") == 0
    row = await session.get(BillingPayment, inapp_pay.payment_id_for(org_id, "req-declined-1"))
    assert row.state == "failed"


async def test_bank_authentication_falls_back(client, session, monkeypatch):
    token, org, _ = await _org_with_card(client, session, "sca")
    _fake_charge(monkeypatch, status="requires_action")
    r = await client.post(
        PAY_URL,
        json={"kind": "credit", "amount_micros": 25_000_000, "request_id": "req-sca-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.json()["status"] == "requires_action"


async def test_fraud_refusal_credits_nothing(client, session, monkeypatch):
    token, org, org_id = await _org_with_card(client, session, "risky")
    _fake_charge(monkeypatch)

    async def risky(session, settings, intent):  # noqa: ANN001, ANN202
        return True

    monkeypatch.setattr(payments, "refuse_risky_payment", risky)
    r = await client.post(
        PAY_URL,
        json={"kind": "voice", "qty": 1, "request_id": "req-risky-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.json()["status"] == "refused"
    session.expunge_all()
    set_org_context(session, org_id)
    assert await bundles.units(session, org_id, "voice") == 0


async def test_fulfillment_error_reports_processing(client, session, monkeypatch):
    token, org, _ = await _org_with_card(client, session, "boom")
    _fake_charge(monkeypatch)

    async def boom(session, settings, intent):  # noqa: ANN001, ANN202
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(payments, "handle_bundle_intent", boom)
    r = await client.post(
        PAY_URL,
        json={"kind": "sms", "qty": 1, "request_id": "req-boom-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "processing"


async def test_no_card_on_file(client, session, monkeypatch):
    token, org, _n = await make_org_with_number(
        client, "inapp-nocard@example.com", "Inapp nocard", "+12145559871"
    )
    _fake_charge(monkeypatch)
    r = await client.post(
        PAY_URL,
        json={"kind": "sms", "qty": 1, "request_id": "req-nocard-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_card_on_file"


async def test_invalid_topup_amount_rejected(client, session, monkeypatch):
    token, org, _ = await _org_with_card(client, session, "amount")
    _fake_charge(monkeypatch)
    r = await client.post(
        PAY_URL,
        json={"kind": "credit", "amount_micros": 4_990_000, "request_id": "req-amount-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422
