"""The 10DLC monthly fee rides on the workspace's monthly plan subscription once the three
prepaid months run out: item first, own subscription cancelled after, never both billing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_sessionmaker
from app.errors import ValidationFailedError
from app.models import SecurityAlert, Subscription, TenDlcRegistration
from app.services import plan_billing, tendlc
from tests.test_tendlc_self_serve import (  # noqa: F401
    _checkout,
    _pay,
    _settings,
    _workspace,
    stripe_stub,
    telnyx,
)

PLAN_SUB = "sub_plan_1"


@pytest.fixture(autouse=True)
def _fresh_product_cache(monkeypatch):
    monkeypatch.setattr(tendlc, "_monthly_product_id", None)


@pytest.fixture
def stripe(stripe_stub):
    calls: list[str] = []
    stripe_stub.SubscriptionItem = SimpleNamespace(
        create=Mock(side_effect=lambda **kw: calls.append("item_create") or {"id": "si_10dlc"}),
        delete=Mock(),
    )
    stripe_stub.Product = SimpleNamespace(create=Mock(return_value={"id": "prod_10dlc"}))
    stripe_stub.Subscription.create = Mock(return_value={"id": "sub_new"})
    stripe_stub.Subscription.cancel = Mock(side_effect=lambda *a, **kw: calls.append("cancel"))
    stripe_stub.calls = calls
    return stripe_stub


async def _active(session, *, days_ago=89, plan="team", interval="month", status="active"):
    org, brand, campaign, _ = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    await _pay(session, reg)
    set_org_context(session, org.id)
    row = (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(TenDlcRegistration.id == reg.id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    row.stage = "active"
    row.paid_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    if plan:
        await plan_billing.ensure_catalog(session, _settings())
        session.add(
            Subscription(
                org_id=org.id,
                plan_code=plan,
                stripe_subscription_id=PLAN_SUB,
                stripe_customer_id="cus_1",
                status=status,
                billing_interval=interval,
            )
        )
    await session.commit()
    return org, row


def test_parse_items_skips_the_tendlc_item_but_refuses_other_unknowns():
    settings = _settings()
    plan_price = plan_billing.plan_price_id(settings, "team", "month")
    if not plan_price:
        pytest.skip("no plan prices configured in test settings")
    sub = {
        "items": {
            "data": [
                {"id": "si_plan", "price": {"id": plan_price}, "quantity": 1},
                {
                    "id": "si_t",
                    "price": {"id": "price_x"},
                    "quantity": 1,
                    "metadata": {"kind": "tendlc_monthly"},
                },
            ]
        }
    }
    assert plan_billing.parse_items(settings, sub).plan_code == "team"
    sub["items"]["data"][1]["metadata"] = {"kind": "other"}
    with pytest.raises(ValidationFailedError):
        plan_billing.parse_items(settings, sub)


async def test_not_before_the_three_months_run_out(session, stripe):
    _, reg = await _active(session, days_ago=60)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.SubscriptionItem.create.assert_not_called()


async def test_moves_onto_an_active_monthly_plan_item_before_cancel(session, stripe):
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True

    kwargs = stripe.SubscriptionItem.create.call_args.kwargs
    assert kwargs["subscription"] == PLAN_SUB
    assert kwargs["price_data"]["unit_amount"] == 1250
    assert kwargs["price_data"]["product"] == "prod_10dlc"
    assert kwargs["price_data"]["recurring"] == {"interval": "month"}
    assert kwargs["metadata"] == {"kind": "tendlc_monthly", "registration_id": str(reg.id)}
    assert kwargs["proration_behavior"] == "create_prorations"
    assert kwargs["idempotency_key"] == f"tendlc-monthly-{reg.id}"
    assert stripe.calls == ["item_create", "cancel"]
    cancel = stripe.Subscription.cancel.call_args
    assert cancel.args[0] == "sub_tendlc"
    assert cancel.kwargs["prorate"] is False
    assert reg.subscription_id == "sub_tendlc"  # the refund code still reads its first invoice
    assert reg.filing["monthly_item_id"] == "si_10dlc"
    assert reg.filing["monthly_on"] == PLAN_SUB

    # Done: a second pass touches nothing.
    stripe.SubscriptionItem.create.reset_mock()
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True
    stripe.SubscriptionItem.create.assert_not_called()


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"interval": "year"},
        {"plan": None},
        {"status": "past_due"},
    ],
)
async def test_yearly_missing_or_unhealthy_plan_leaves_the_standalone_subscription(
    session, stripe, kwargs
):
    _, reg = await _active(session, **kwargs)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.SubscriptionItem.create.assert_not_called()
    stripe.Subscription.cancel.assert_not_called()
    assert "monthly_item_id" not in reg.filing


async def test_already_billing_standalone_is_not_moved(session, stripe):
    stripe.Subscription.retrieve.return_value = {"status": "active", "latest_invoice": "in_2"}
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.SubscriptionItem.create.assert_not_called()
    stripe.Subscription.cancel.assert_not_called()


async def test_stripe_error_on_item_create_cancels_nothing_and_retries(session, stripe):
    stripe.SubscriptionItem.create.side_effect = RuntimeError("stripe down")
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.Subscription.cancel.assert_not_called()
    assert "monthly_item_id" not in reg.filing

    stripe.SubscriptionItem.create.side_effect = None
    stripe.SubscriptionItem.create.return_value = {"id": "si_10dlc"}
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True


async def test_failed_own_cancel_retries_only_the_cancel(session, stripe):
    stripe.Subscription.cancel.side_effect = RuntimeError("stripe down")
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    assert reg.filing["monthly_item_id"] == "si_10dlc"
    assert stripe.SubscriptionItem.create.call_count == 1

    stripe.Subscription.cancel.side_effect = None
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True
    assert stripe.SubscriptionItem.create.call_count == 1  # no second item
    assert stripe.Subscription.cancel.call_count == 2
    assert reg.filing["own_sub_cancelled"] is True


async def test_tick_moves_through_the_active_stage(session, stripe, telnyx):
    org, reg = await _active(session)
    reg_id = reg.id
    await tendlc.tick(get_sessionmaker(), _settings(), client=telnyx.client)
    stripe.SubscriptionItem.create.assert_called_once()
    async with get_sessionmaker()() as s:
        set_org_context(s, org.id)
        row = await s.get(TenDlcRegistration, reg_id)
        assert row.filing["monthly_item_id"] == "si_10dlc"


async def test_campaign_declined_after_the_move_deletes_the_item_only(session, stripe):
    _, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    stripe.Subscription.cancel.reset_mock()

    await tendlc._cancel_monthly(_settings(), reg)

    stripe.SubscriptionItem.delete.assert_called_once_with(
        "si_10dlc", proration_behavior="none"
    )
    stripe.Subscription.cancel.assert_not_called()  # own subscription is already cancelled
    assert "monthly_item_id" not in reg.filing


async def test_plan_deleted_recreates_a_standalone_subscription(session, stripe):
    org, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)

    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, "cus_1")
    await session.commit()

    kwargs = stripe.Subscription.create.call_args.kwargs
    assert kwargs["customer"] == "cus_1"
    assert kwargs["metadata"] == {"kind": "tendlc_fee", "registration_id": str(reg.id)}
    assert kwargs["items"][0]["price_data"]["unit_amount"] == 1250
    assert reg.subscription_id == "sub_new"
    assert "monthly_item_id" not in reg.filing and "monthly_on" not in reg.filing


async def test_plan_deleted_failure_opens_an_ops_alert(session, stripe):
    org, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    stripe.Subscription.create.side_effect = RuntimeError("card declined")

    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, "cus_1")
    await session.commit()

    alert = (await session.execute(sa.select(SecurityAlert))).scalar_one()
    assert alert.kind == "tendlc_monthly_orphan"
    assert alert.detail["registration_id"] == str(reg.id)
    assert reg.filing["monthly_item_id"] == "si_10dlc"  # untouched, still retryable by ops


async def test_other_plan_deleted_leaves_the_registration_alone(session, stripe):
    org, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, "sub_other", "cus_1")
    stripe.Subscription.create.assert_not_called()
    assert uuid.UUID(str(reg.id))
