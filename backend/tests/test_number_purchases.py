import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
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
