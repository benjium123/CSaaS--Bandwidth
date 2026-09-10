from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import UnauthenticatedError
from app.models import CreditLedgerEntry, Org, PaymentMethod, PlatformEvent
from app.services import ai_usage, credits, stripe_client
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, create_org, make_settings, register_and_login


def _payment_intent_event(*, metadata, amount_received=500, intent_id="pi_test_123"):
    return {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": intent_id,
                "amount_received": amount_received,
                "metadata": metadata,
            }
        },
    }


async def _make_org(session, name="Stripe Org", auto=None):
    org = Org(id=uuid.uuid4(), name=name, slug=f"stripe-{uuid.uuid4().hex[:16]}")
    org.credit_auto_recharge = auto if auto is not None else {}
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


async def _make_payment_method(session, org_id):
    set_org_context(session, org_id)
    pm = PaymentMethod(
        id=uuid.uuid4(),
        org_id=org_id,
        stripe_customer_id="cus_test",
        stripe_payment_method_id="pm_test",
        brand="Visa",
        last4="4242",
        is_default=True,
    )
    session.add(pm)
    await session.commit()
    await session.refresh(pm)
    return pm


async def _count_ledger_all(session):
    return (
        await session.execute(
            sa.select(sa.func.count(CreditLedgerEntry.id)).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalar_one()


async def test_a_bad_signature_is_rejected(client, monkeypatch):
    def bad_verify(settings, payload, signature):
        raise UnauthenticatedError("We could not verify that this came from our payment provider.")

    monkeypatch.setattr(stripe_client, "verify_webhook", bad_verify)

    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=bad,sig=bad"},
    )
    assert r.status_code == 401, r.text


async def test_a_missing_signature_is_rejected(client, monkeypatch):
    called = 0

    def bad_verify(settings, payload, signature):
        nonlocal called
        called += 1
        return {}

    monkeypatch.setattr(stripe_client, "verify_webhook", bad_verify)

    r = await client.post("/api/v1/webhooks/stripe", content=b"{}")
    assert r.status_code == 401, r.text
    assert called == 0


async def test_an_unknown_event_type_is_acknowledged_and_ignored(client, session, monkeypatch):
    event = {"type": "customer.created", "data": {"object": {"id": "cus_1"}}}
    monkeypatch.setattr(stripe_client, "verify_webhook", lambda settings, payload, sig: event)

    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=test,sig=test"},
    )
    assert r.status_code == 204, r.text
    assert await _count_ledger_all(session) == 0


async def test_a_payment_intent_without_our_metadata_is_ignored(client, session, monkeypatch):
    event = _payment_intent_event(metadata={"unrelated": "true"})
    monkeypatch.setattr(stripe_client, "verify_webhook", lambda settings, payload, sig: event)

    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=test,sig=test"},
    )
    assert r.status_code == 204, r.text
    assert await _count_ledger_all(session) == 0


async def test_a_successful_payment_adds_credits_once(client, session, monkeypatch):
    token = await register_and_login(client, "pay@example.com")
    org = await create_org(client, token, "Pay Org")
    org_id = uuid.UUID(org["id"])
    amount_cents = 500

    event = _payment_intent_event(
        metadata={"org_id": org["id"], "kind": "credit_topup"},
        amount_received=amount_cents,
        intent_id="pi_pay_1",
    )
    monkeypatch.setattr(stripe_client, "verify_webhook", lambda settings, payload, sig: event)

    url = "/api/v1/webhooks/stripe"
    headers = {"Stripe-Signature": "t=test,sig=test"}
    r1 = await client.post(url, content=b"{}", headers=headers)
    assert r1.status_code == 200, r1.text

    r2 = await client.post(url, content=b"{}", headers=headers)
    assert r2.status_code == 200, r2.text

    set_org_context(session, org_id)
    assert await credits.balance(session, org_id) == amount_cents * 10_000

    topup_count = (
        await session.execute(
            sa.select(sa.func.count(CreditLedgerEntry.id)).where(
                CreditLedgerEntry.org_id == org_id,
                CreditLedgerEntry.entry_type == "topup",
            )
        )
    ).scalar_one()
    assert topup_count == 1

    topup_row = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(
                CreditLedgerEntry.org_id == org_id,
                CreditLedgerEntry.entry_type == "topup",
            )
        )
    ).scalar_one()
    assert topup_row.reference == "pi_pay_1"


async def test_a_payment_for_an_unknown_workspace_is_acknowledged_not_500ed(client, monkeypatch):
    event = _payment_intent_event(
        metadata={"org_id": str(uuid.uuid4()), "kind": "credit_topup"},
        amount_received=500,
        intent_id="pi_unknown_org",
    )
    monkeypatch.setattr(stripe_client, "verify_webhook", lambda settings, payload, sig: event)

    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=test,sig=test"},
    )
    assert r.status_code in (200, 204), r.text


async def test_auto_recharge_charges_once_per_crossing(session, monkeypatch):
    org = await _make_org(session)
    pm = await _make_payment_method(session, org.id)
    await session.refresh(org)

    org.credit_auto_recharge = {
        "enabled": True,
        "threshold_micros": 10_000_000,
        "amount_micros": 5_000_000,
        "payment_method_id": str(pm.id),
    }
    await session.commit()
    await session.refresh(org)

    calls = 0

    async def fake_charge(settings, *, org, amount_micros, payment_method_id, customer_id, idempotency_key):
        nonlocal calls
        calls += 1
        return {"id": "pi_auto_1", "status": "succeeded"}

    monkeypatch.setattr(stripe_client, "charge_off_session", fake_charge)
    monkeypatch.setattr(stripe_client, "is_configured", lambda settings: True)

    settings = make_settings()
    first = await ai_usage.maybe_auto_recharge(session, org, settings=settings)
    second = await ai_usage.maybe_auto_recharge(session, org, settings=settings)

    assert first["charged"] is True
    assert second is None
    assert calls == 1

    set_org_context(session, org.id)
    markers = (
        await session.execute(
            sa.select(PlatformEvent).where(
                PlatformEvent.org_id == org.id,
                PlatformEvent.event_type == "billing.low_balance",
            )
        )
    ).scalars().all()
    auto_markers = [e for e in markers if (e.payload or {}).get("kind") == "auto_recharge"]
    assert len(auto_markers) == 1


async def test_auto_recharge_does_nothing_when_it_is_switched_off(session, monkeypatch):
    org = await _make_org(session, auto={"threshold_micros": 10_000_000, "amount_micros": 5_000_000})

    calls = 0

    async def fake_charge(settings, *, org, amount_micros, payment_method_id, customer_id, idempotency_key):
        nonlocal calls
        calls += 1
        return {"id": "", "status": "failed", "reason": "should not be called"}

    monkeypatch.setattr(stripe_client, "charge_off_session", fake_charge)

    result = await ai_usage.maybe_auto_recharge(session, org, settings=make_settings())
    assert result is None
    assert calls == 0


async def test_a_declined_card_does_not_add_credits(session, monkeypatch):
    org = await _make_org(session)
    pm = await _make_payment_method(session, org.id)
    await session.refresh(org)

    org.credit_auto_recharge = {
        "enabled": True,
        "threshold_micros": 10_000_000,
        "amount_micros": 5_000_000,
        "payment_method_id": str(pm.id),
    }
    await session.commit()
    await session.refresh(org)

    async def fake_charge(settings, *, org, amount_micros, payment_method_id, customer_id, idempotency_key):
        return {"id": "", "status": "failed", "reason": "Your card was declined."}

    monkeypatch.setattr(stripe_client, "charge_off_session", fake_charge)
    monkeypatch.setattr(stripe_client, "is_configured", lambda settings: True)

    result = await ai_usage.maybe_auto_recharge(session, org, settings=make_settings())
    assert result["charged"] is False

    set_org_context(session, org.id)
    assert await credits.balance(session, org.id) == 0

    topup_count = (
        await session.execute(
            sa.select(sa.func.count(CreditLedgerEntry.id)).where(
                CreditLedgerEntry.org_id == org.id,
                CreditLedgerEntry.entry_type == "topup",
            )
        )
    ).scalar_one()
    assert topup_count == 0


async def test_a_platform_operator_adjustment_requires_a_note(client, session):
    token = await register_and_login(client, "ops-adjust@example.com")
    org = await create_org(client, token, "Adjust Org")
    org_id = uuid.UUID(org["id"])

    url = f"/api/v1/platform/billing/orgs/{org['id']}/adjustments"
    ops_headers = {"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN}

    r_empty = await client.post(
        url,
        json={"amount_micros": 1000, "note": "", "entry_type": "adjustment"},
        headers=ops_headers,
    )
    assert r_empty.status_code in (400, 422), r_empty.text
    assert r_empty.json()["error"]["code"] == "validation_failed"

    r_ok = await client.post(
        url,
        json={"amount_micros": 1000, "note": "Credit for testing", "entry_type": "adjustment"},
        headers=ops_headers,
    )
    assert r_ok.status_code == 201, r_ok.text
    assert r_ok.json()["balance_after_micros"] == 1000

    assert await credits.balance(session, org_id) == 1000


async def test_platform_billing_routes_reject_a_bad_ops_token(client):
    token = await register_and_login(client, "bad-ops@example.com")
    org = await create_org(client, token, "Bad Ops Org")

    url = f"/api/v1/platform/billing/orgs/{org['id']}/adjustments"
    r = await client.post(
        url,
        json={"amount_micros": 1000, "note": "hello", "entry_type": "adjustment"},
        headers={"X-Platform-Ops-Token": "wrong-token"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "permission_denied"


async def test_platform_billing_routes_reject_a_missing_ops_token(client):
    """A MISSING ops token must be 403, exactly like a wrong one.

    Binding: the operator frontend treats any 401 as a full session expiry and logs the
    operator out app-wide, so a forgotten or mistyped ops token must never return 401.
    """
    token = await register_and_login(client, "no-ops@example.com")
    org = await create_org(client, token, "No Ops Org")

    url = f"/api/v1/platform/billing/orgs/{org['id']}/adjustments"
    r = await client.post(
        url,
        json={"amount_micros": 1000, "note": "hello", "entry_type": "adjustment"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "permission_denied"


async def test_platform_billing_reads_also_reject_a_bad_ops_token(client):
    """The GET surface is gated too, and with the same 403."""
    token = await register_and_login(client, "ops-read@example.com")
    org = await create_org(client, token, "Ops Read Org")

    r = await client.get(
        f"/api/v1/platform/billing/orgs/{org['id']}",
        headers={"X-Platform-Ops-Token": "wrong-token"},
    )
    assert r.status_code == 403, r.text
