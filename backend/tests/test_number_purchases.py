import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import FeatureUnavailableError
from app.models import Inbox, KycProfile, Org, OrgNumber
from app.providers.numbers import OrderResult
from app.services import number_purchases, stripe_client
from tests.conftest import make_settings


@pytest.fixture
def stripe_mock(monkeypatch):
    stripe = SimpleNamespace(
        Price=SimpleNamespace(
            retrieve=Mock(
                return_value={
                    "active": True,
                    "unit_amount": 1500,
                    "currency": "usd",
                    "recurring": {"interval": "month", "interval_count": 1},
                }
            )
        ),
        checkout=SimpleNamespace(
            Session=SimpleNamespace(
                create=Mock(
                    return_value={"id": "cs_test", "url": "https://checkout.stripe.com/test"}
                ),
                retrieve=Mock(),
            )
        ),
        Subscription=SimpleNamespace(retrieve=Mock(), modify=Mock(), cancel=Mock()),
    )
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: stripe)
    return stripe


async def approved_org(session):
    org = Org(id=uuid.uuid4(), name="Paid workspace", slug=uuid.uuid4().hex)
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    session.add(KycProfile(org_id=org.id, status="approved"))
    await session.commit()
    return org


async def test_quantity_checkout_and_retry_reuses_session(session, stripe_mock):
    org = await approved_org(session)
    settings = make_settings(stripe_webhook_secret="whsec_test")
    numbers = ["+12125550101", "+12125550102", "+12125550103"]
    purchase = await number_purchases.create(session, settings, org.id, numbers)
    assert stripe_mock.checkout.Session.create.call_args.kwargs["line_items"] == [
        {"price": settings.stripe_number_price_id, "quantity": 3}
    ]
    assert number_purchases.public(purchase)["monthly_total_cents"] == 4500
    stripe_mock.checkout.Session.retrieve.return_value = {"status": "open"}
    again = await number_purchases.create(session, settings, org.id, numbers)
    assert again.id == purchase.id
    assert stripe_mock.checkout.Session.create.call_count == 1


async def test_payment_required_then_fulfillment_is_idempotent(session, stripe_mock, monkeypatch):
    org = await approved_org(session)
    settings = make_settings(stripe_webhook_secret="whsec_test")
    purchase = await number_purchases.create(session, settings, org.id, ["+12125550101"])
    carrier = SimpleNamespace(
        name="telnyx",
        order_number=AsyncMock(
            return_value=OrderResult(e164="+12125550101", provider_ref="order1", status="active")
        ),
    )
    monkeypatch.setattr("app.providers.numbers.as_provider", lambda obj: obj)
    registry = SimpleNamespace(get=lambda name: carrier)
    monkeypatch.setattr(
        "app.providers.registry_org.prime_org_registry", AsyncMock(return_value=registry)
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, carriers=registry))
    )
    stripe_mock.checkout.Session.retrieve.return_value = {
        "status": "open",
        "payment_status": "unpaid",
    }
    await number_purchases.fulfill(session, request, purchase)
    carrier.order_number.assert_not_called()
    stripe_mock.checkout.Session.retrieve.return_value = {
        "status": "complete",
        "payment_status": "paid",
        "subscription": "sub_test",
        "metadata": {"purchase_id": str(purchase.id)},
    }
    stripe_mock.Subscription.retrieve.return_value = {
        "id": "sub_test",
        "status": "active",
        "items": {"data": [{"price": {"id": settings.stripe_number_price_id}, "quantity": 1}]},
    }
    result = await number_purchases.fulfill(session, request, purchase)
    assert result.state == "complete", result.detail
    await number_purchases.fulfill(session, request, purchase)
    assert carrier.order_number.call_count == 1
    assert len((await session.execute(sa.select(OrgNumber))).scalars().all()) == 1
    assert len((await session.execute(sa.select(Inbox))).scalars().all()) == 1


async def test_wrong_price_is_refused(session, stripe_mock):
    from app.errors import FeatureUnavailableError

    org = await approved_org(session)
    stripe_mock.Price.retrieve.return_value["unit_amount"] = 2000
    with pytest.raises(FeatureUnavailableError):
        await number_purchases.create(
            session, make_settings(stripe_webhook_secret="test"), org.id, ["+12125550101"]
        )
    stripe_mock.checkout.Session.create.assert_not_called()


async def test_release_reduces_quantity_and_last_number_cancels(session, stripe_mock):
    from app.models import NumberPurchase

    org = await approved_org(session)
    purchase = NumberPurchase(
        id=uuid.uuid4(),
        org_id=org.id,
        numbers=[],
        state="complete",
        subscription_id="sub_test",
        subscription_status="active",
    )
    session.add(purchase)
    numbers = [
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164=f"+1212555010{i}",
            carrier="telnyx",
            status="active",
            is_active=True,
            provisioning={"number_purchase_id": str(purchase.id), "billing": "stripe_subscription"},
        )
        for i in range(1, 3)
    ]
    session.add_all(numbers)
    await session.commit()
    stripe_mock.Subscription.retrieve.return_value = {
        "status": "active",
        "items": {"data": [{"id": "si_test"}]},
    }
    numbers[0].status = "released"
    await session.commit()
    await number_purchases.sync_released_number(session, make_settings(), numbers[0])
    assert stripe_mock.Subscription.modify.call_args.kwargs["items"] == [
        {"id": "si_test", "quantity": 1}
    ]
    stripe_mock.Subscription.cancel.assert_not_called()
    numbers[1].status = "released"
    await session.commit()
    await number_purchases.sync_released_number(session, make_settings(), numbers[1])
    stripe_mock.Subscription.cancel.assert_called_once()
    assert purchase.subscription_status == "canceled"


@pytest.mark.parametrize(("available", "refused"), [(None, False), (1199, True), (1200, False)])
async def test_checkout_is_refused_when_telnyx_cannot_fund_the_numbers(
    session, stripe_mock, monkeypatch, available, refused
):
    """Two numbers need 2 x $3.50 (number + first E911 month) plus the $5 floor = $12. Below that the customer is
    never sent to pay; None (Telnyx not configured) does not block."""
    org = await approved_org(session)
    settings = make_settings(stripe_webhook_secret="whsec_test")

    async def fake_available(_settings):
        return available

    monkeypatch.setattr(number_purchases, "telnyx_available_cents", fake_available)
    numbers = ["+12125550111", "+12125550112"]
    if refused:
        with pytest.raises(FeatureUnavailableError) as exc:
            await number_purchases.create(session, settings, org.id, numbers)
        assert exc.value.code == "numbers_temporarily_unavailable"
        stripe_mock.checkout.Session.create.assert_not_called()
    else:
        await number_purchases.create(session, settings, org.id, numbers)
        stripe_mock.checkout.Session.create.assert_called_once()


# ----------------------------------------------------------------------------------
# Operator recovery for a paid purchase that stalled
# ----------------------------------------------------------------------------------
async def _stalled(session, org, e164s, *, provisioned=(), state="needs_attention"):
    from app.models import NumberPurchase

    purchase = NumberPurchase(
        id=uuid.uuid4(),
        org_id=org.id,
        numbers=[{"e164": n, "state": "selected"} for n in e164s],
        state=state,
        subscription_id=f"sub_{uuid.uuid4().hex[:8]}",
        subscription_status="active",
    )
    session.add(purchase)
    await session.commit()
    entries = list(purchase.numbers)
    for index, e164 in enumerate(e164s):
        if e164 in provisioned:
            number = OrgNumber(id=uuid.uuid4(), org_id=org.id, e164=e164, carrier="telnyx")
            session.add(number)
            entries[index] = {"e164": e164, "state": "active", "number_id": str(number.id)}
    purchase.numbers = entries
    await session.commit()
    return purchase


def _carrier_request(monkeypatch, settings, *, owned):
    carrier = SimpleNamespace(
        name="telnyx",
        lookup_owned_number=AsyncMock(side_effect=lambda e164: owned[e164]),
        order_number=AsyncMock(
            side_effect=lambda e164: OrderResult(e164=e164, provider_ref="o", status="active")
        ),
    )
    monkeypatch.setattr("app.providers.numbers.as_provider", lambda obj: obj)
    registry = SimpleNamespace(get=lambda name: carrier)
    monkeypatch.setattr(
        "app.providers.registry_org.prime_org_registry", AsyncMock(return_value=registry)
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, carriers=registry))
    )
    return carrier, request


async def test_retry_links_an_order_that_went_through_and_orders_only_the_missing_one(
    session, monkeypatch
):
    org = await approved_org(session)
    a, b = "+12125550201", "+12125550202"
    purchase = await _stalled(session, org, [a, b])
    carrier, request = _carrier_request(monkeypatch, make_settings(), owned={a: True, b: False})

    result, failures = await number_purchases.retry(session, request, purchase.id)

    assert failures == []
    assert result.state == "complete"
    carrier.order_number.assert_awaited_once_with(b)  # never re-ordered the one Telnyx has
    held = {n.e164 for n in (await session.execute(sa.select(OrgNumber))).scalars()}
    assert held == {a, b}


async def test_retry_never_touches_a_number_another_workspace_holds(session, monkeypatch):
    org = await approved_org(session)
    other = await approved_org(session)
    taken = "+12125550203"
    session.add(OrgNumber(id=uuid.uuid4(), org_id=other.id, e164=taken, carrier="telnyx"))
    await session.commit()
    set_org_context(session, org.id)
    purchase = await _stalled(session, org, [taken])
    carrier, request = _carrier_request(monkeypatch, make_settings(), owned={taken: True})

    result, failures = await number_purchases.retry(session, request, purchase.id)

    assert result.state == "needs_attention"
    assert len(failures) == 1 and taken in failures[0]
    carrier.order_number.assert_not_called()
    carrier.lookup_owned_number.assert_not_called()


async def test_retry_refuses_a_purchase_that_is_still_being_set_up(session, monkeypatch):
    from app.errors import ConflictError

    org = await approved_org(session)
    purchase = await _stalled(session, org, ["+12125550204"], state="provisioning")
    _carrier, request = _carrier_request(monkeypatch, make_settings(), owned={})
    with pytest.raises(ConflictError):
        await number_purchases.retry(session, request, purchase.id)


def _refund_stripe(stripe_mock):
    stripe_mock.Subscription.retrieve.return_value = {
        "id": "sub",
        "status": "active",
        "latest_invoice": "in_1",
        "items": {"data": [{"id": "si_1", "quantity": 2}]},
    }
    stripe_mock.InvoicePayment = SimpleNamespace(
        list=Mock(return_value={"data": [{"payment": {"payment_intent": "pi_1"}}]})
    )
    stripe_mock.Refund = SimpleNamespace(create=Mock(return_value={"id": "re_1"}))


async def test_refund_keeps_provisioned_numbers_and_refunds_the_rest(session, stripe_mock):
    from app.services import seats

    org = await approved_org(session)
    a, b = "+12125550205", "+12125550206"
    purchase = await _stalled(session, org, [a, b], provisioned=[a])
    _refund_stripe(stripe_mock)

    result = await number_purchases.refund_unprovisioned(session, make_settings(), purchase.id)

    assert result.state == "complete"
    modify = stripe_mock.Subscription.modify.call_args
    assert modify.kwargs["items"] == [{"id": "si_1", "quantity": 1}]
    stripe_mock.Subscription.cancel.assert_not_called()
    refund = stripe_mock.Refund.create.call_args.kwargs
    assert (refund["payment_intent"], refund["amount"]) == ("pi_1", 1500)
    assert await seats.paid_numbers(session, org.id) == 1


async def test_refunding_every_number_cancels_billing(session, stripe_mock):
    org = await approved_org(session)
    purchase = await _stalled(session, org, ["+12125550207", "+12125550208"])
    _refund_stripe(stripe_mock)

    result = await number_purchases.refund_unprovisioned(session, make_settings(), purchase.id)

    assert result.state == "refunded"
    assert result.subscription_status == "canceled"
    stripe_mock.Subscription.cancel.assert_called_once()
    assert stripe_mock.Refund.create.call_args.kwargs["amount"] == 3000
