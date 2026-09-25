"""P44c: stolen-card checks, early fraud warnings, chargebacks."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import PermissionDeniedError
from app.models import (
    CreditLedgerEntry,
    KycProfile,
    Org,
    OrgMonitoring,
    PaymentMethod,
    SecurityAlert,
)
from app.services import ban_list, card_risk, credits, phone_region, stripe_client


async def _org(session, country: str | None = None) -> Org:
    org = Org(id=uuid.uuid4(), name="Card Org", slug=f"cd-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    if country:
        session.add(KycProfile(id=uuid.uuid4(), org_id=org.id, country=country))
        await session.commit()
    phone_region.forget()
    return org


def _pm(org_id, fingerprint: str, *, age=timedelta(days=1)) -> PaymentMethod:
    return PaymentMethod(
        id=uuid.uuid4(),
        org_id=org_id,
        stripe_customer_id="cus_1",
        stripe_payment_method_id=f"pm_{uuid.uuid4().hex[:8]}",
        card_fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc) - age,
    )


async def _alerts(session, org_id, kind):
    return (
        await session.execute(
            sa.select(SecurityAlert).where(
                SecurityAlert.org_id == org_id, SecurityAlert.kind == kind
            )
        )
    ).scalars().all()


async def test_foreign_card_is_refused(session):
    org = await _org(session, "US")
    with pytest.raises(PermissionDeniedError) as err:
        await card_risk.check_new_card(session, org.id, fingerprint="fp1", card_country="NG")
    assert err.value.code == "card_country_mismatch"


async def test_home_card_is_accepted(session):
    org = await _org(session, "GB")
    await card_risk.check_new_card(session, org.id, fingerprint="fp1", card_country="GB")


async def test_fourth_card_in_30_days_is_refused(session):
    org = await _org(session)
    for i in range(3):
        session.add(_pm(org.id, f"fp{i}"))
    session.add(_pm(org.id, "old", age=timedelta(days=40)))
    await session.commit()
    with pytest.raises(PermissionDeniedError) as err:
        await card_risk.check_new_card(session, org.id, fingerprint="fp9", card_country="US")
    assert err.value.code == "too_many_cards"


async def test_card_saved_on_another_org_opens_an_alert(session):
    first = await _org(session)
    session.add(_pm(first.id, "shared-fp"))
    await session.commit()
    second = await _org(session)
    await card_risk.check_new_card(session, second.id, fingerprint="shared-fp", card_country="US")
    await session.commit()
    assert len(await _alerts(session, second.id, "shared_card")) == 1


def _intent(org_id, amount_cents=5000, kind="credit_topup"):
    return {
        "id": "pi_1",
        "amount_received": amount_cents,
        "metadata": {"org_id": str(org_id), "kind": kind},
        "latest_charge": {"payment_method_details": {"card": {"fingerprint": "stolen-fp"}}},
    }


@pytest.fixture
def fake_stripe(monkeypatch):
    calls = {"refunds": []}
    state = {"intent": None}

    async def retrieve(settings, pi):
        return state["intent"]

    async def refund(settings, pi, *, idempotency_key):
        calls["refunds"].append((pi, idempotency_key))
        return {"id": "re_1"}

    monkeypatch.setattr(stripe_client, "retrieve_intent_with_charge", retrieve)
    monkeypatch.setattr(stripe_client, "refund_intent", refund)
    return calls, state


async def _setup_paid_org(session):
    org = await _org(session)
    await credits.topup(session, org.id, 50_000_000, reference=f"t-{uuid.uuid4()}")
    await session.commit()
    return org


async def test_early_fraud_warning_refunds_holds_bans_and_pauses(session, settings, fake_stripe):
    calls, state = fake_stripe
    org = await _setup_paid_org(session)
    state["intent"] = _intent(org.id)
    event = {
        "type": "radar.early_fraud_warning.created",
        "data": {"object": {"id": "issfr_1", "payment_intent": "pi_1", "actionable": True}},
    }
    await card_risk.handle_stripe_event(session, settings, event)
    await session.commit()

    assert calls["refunds"] == [("pi_1", "efw-issfr_1")]
    assert await credits.balance(session, org.id) == 0
    assert await ban_list.matches(session, [ban_list.identifier("card_fingerprint", "stolen-fp")])
    set_org_context(session, org.id)
    state_row = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org.id))
    ).scalar_one()
    assert state_row.level == "paused"
    assert len(await _alerts(session, org.id, "early_fraud_warning")) == 1

    # A replay (Stripe retry with a new event id) never takes the money twice.
    await card_risk.handle_stripe_event(session, settings, event)
    await session.commit()
    assert await credits.balance(session, org.id) == 0


async def test_chargeback_holds_then_a_won_dispute_returns_the_credit(
    session, settings, fake_stripe
):
    calls, state = fake_stripe
    org = await _setup_paid_org(session)
    state["intent"] = _intent(org.id)
    created = {
        "type": "charge.dispute.created",
        "data": {"object": {"id": "dp_1", "payment_intent": "pi_1", "status": "needs_response"}},
    }
    await card_risk.handle_stripe_event(session, settings, created)
    await session.commit()
    assert calls["refunds"] == []  # the bank already took the money back
    assert await credits.balance(session, org.id) == 0

    won = {
        "type": "charge.dispute.closed",
        "data": {"object": {"id": "dp_1", "payment_intent": "pi_1", "status": "won"}},
    }
    await card_risk.handle_stripe_event(session, settings, won)
    await card_risk.handle_stripe_event(session, settings, won)
    await session.commit()
    assert await credits.balance(session, org.id) == 50_000_000


async def test_warning_then_chargeback_on_one_payment_holds_once(session, settings, fake_stripe):
    _calls, state = fake_stripe
    org = await _setup_paid_org(session)
    await credits.topup(session, org.id, 50_000_000, reference=f"t-{uuid.uuid4()}")
    await session.commit()
    state["intent"] = _intent(org.id)
    await card_risk.handle_stripe_event(session, settings, {
        "type": "radar.early_fraud_warning.created",
        "data": {"object": {"id": "issfr_2", "payment_intent": "pi_1", "actionable": False}},
    })
    await card_risk.handle_stripe_event(session, settings, {
        "type": "charge.dispute.created",
        "data": {"object": {"id": "dp_2", "payment_intent": "pi_1", "status": "needs_response"}},
    })
    await session.commit()
    assert await credits.balance(session, org.id) == 50_000_000  # held once, not twice

    # Refunded after the warning: winning the dispute must not hand the credit back too.
    state["intent"] = {**_intent(org.id), "latest_charge": {"amount_refunded": 5000}}
    await card_risk.handle_stripe_event(session, settings, {
        "type": "charge.dispute.closed",
        "data": {"object": {"id": "dp_2", "payment_intent": "pi_1", "status": "won"}},
    })
    await session.commit()
    assert await credits.balance(session, org.id) == 50_000_000


async def test_fraud_on_a_bundle_payment_pauses_without_touching_the_balance(
    session, settings, fake_stripe
):
    _calls, state = fake_stripe
    org = await _setup_paid_org(session)
    state["intent"] = _intent(org.id, kind="sms_bundle")
    await card_risk.handle_stripe_event(
        session,
        settings,
        {"type": "charge.dispute.created", "data": {"object": {"id": "dp_2", "payment_intent": "pi_1"}}},
    )
    await session.commit()
    assert await credits.balance(session, org.id) == 50_000_000
    set_org_context(session, org.id)
    level = (
        await session.execute(sa.select(OrgMonitoring.level).where(OrgMonitoring.org_id == org.id))
    ).scalar_one()
    assert level == "paused"


def test_every_checkout_asks_for_3d_secure():
    assert stripe_client.THREE_DS_OPTIONS == {"card": {"request_three_d_secure": "any"}}
