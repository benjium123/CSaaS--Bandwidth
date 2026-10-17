"""The 10DLC monthly fee rides on the workspace's monthly plan subscription once the three
prepaid months run out: item first, own subscription cancelled after, never both billing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_sessionmaker
from app.errors import ValidationFailedError
from app.models import SecurityAlert, Subscription, TenDlcRegistration
from app.services import plan_billing, stripe_client, tendlc
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


def _ts(**delta):
    return int((datetime.now(timezone.utc) + timedelta(**delta)).timestamp())


@pytest.fixture
def stripe(stripe_stub):
    calls: list[str] = []
    state = {
        "own": {"status": "trialing", "latest_invoice": "in_1", "trial_end": _ts(hours=12)},
        "plan": {"id": PLAN_SUB, "status": "active", "items": {"data": []}},
    }

    def retrieve(sub_id):
        return state["plan"] if sub_id == PLAN_SUB else state["own"]

    stripe_stub.Subscription.retrieve = Mock(side_effect=retrieve)
    stripe_stub.SubscriptionItem = SimpleNamespace(
        create=Mock(side_effect=lambda **kw: calls.append("item_create") or {"id": "si_10dlc"}),
        delete=Mock(),
    )
    stripe_stub.Product = SimpleNamespace(
        search=Mock(return_value={"data": []}), create=Mock(return_value={"id": "prod_10dlc"})
    )
    stripe_stub.Customer = SimpleNamespace(
        retrieve=Mock(return_value={"invoice_settings": {"default_payment_method": "pm_cust"}})
    )
    stripe_stub.Subscription.create = Mock(return_value={"id": "sub_new", "status": "active"})
    def cancel(*a, **kw):
        calls.append("cancel")
        state["own"] = {**state["own"], "status": "canceled"}

    stripe_stub.Subscription.cancel = Mock(side_effect=cancel)
    stripe_stub.calls = calls
    stripe_stub.state = state
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
    assert kwargs["idempotency_key"] == f"tendlc-monthly-{reg.id}-{PLAN_SUB}"
    assert stripe.calls == ["cancel", "item_create"]
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
        {"status": "trialing"},  # a trialing plan would carry the fee for free
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
    stripe.state["own"] = {"status": "active", "latest_invoice": "in_2", "trial_end": _ts(days=-1)}
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.SubscriptionItem.create.assert_not_called()
    stripe.Subscription.cancel.assert_not_called()


async def test_window_opens_one_day_before_the_trial_ends(session, stripe):
    stripe.state["own"]["trial_end"] = _ts(days=2)
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.Subscription.cancel.assert_not_called()
    stripe.SubscriptionItem.create.assert_not_called()


async def test_discounted_plan_gets_no_move(session, stripe):
    stripe.state["plan"]["discounts"] = [{"coupon": "co_1"}]
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.Subscription.cancel.assert_not_called()
    stripe.SubscriptionItem.create.assert_not_called()


async def test_item_level_plan_discount_still_takes_the_fee_undiscounted(session, stripe):
    stripe.state["plan"]["items"] = {"data": [{"id": "si_p", "discounts": [{"coupon": "c"}]}]}
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True
    kwargs = stripe.SubscriptionItem.create.call_args.kwargs
    assert "discounts" not in kwargs and "discount" not in kwargs
    assert kwargs["price_data"]["unit_amount"] == 1250


async def test_item_create_failure_after_cancel_retries_without_a_second_cancel(session, stripe):
    stripe.SubscriptionItem.create.side_effect = RuntimeError("stripe down")
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    assert reg.filing["own_sub_cancelled"] is True
    assert "monthly_item_id" not in reg.filing

    stripe.SubscriptionItem.create.side_effect = None
    stripe.SubscriptionItem.create.return_value = {"id": "si_10dlc"}
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True
    assert stripe.Subscription.cancel.call_count == 1
    assert reg.filing["monthly_item_id"] == "si_10dlc"
    assert (await session.execute(sa.select(SecurityAlert))).scalars().all() == []


async def test_item_create_still_failing_a_day_after_the_window_alerts_ops_once(session, stripe):
    stripe.SubscriptionItem.create.side_effect = RuntimeError("stripe down")
    _, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    assert (await session.execute(sa.select(SecurityAlert))).scalars().all() == []

    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    reg.filing = {**reg.filing, "monthly_window": past}
    await session.commit()
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    alerts = (await session.execute(sa.select(SecurityAlert))).scalars().all()
    assert [a.kind for a in alerts] == ["tendlc_monthly_orphan"]


async def test_product_is_looked_up_before_one_is_created(session, stripe):
    stripe.Product.search.return_value = {"data": [{"id": "prod_existing"}]}
    _, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    stripe.Product.create.assert_not_called()
    kwargs = stripe.SubscriptionItem.create.call_args.kwargs
    assert kwargs["price_data"]["product"] == "prod_existing"
    assert "tendlc_monthly" in stripe.Product.search.call_args.kwargs["query"]


async def test_new_product_carries_the_kind_metadata(session, stripe):
    _, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    assert stripe.Product.create.call_args.kwargs["metadata"] == {"kind": "tendlc_monthly"}


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


def _deleted_plan(**over):
    return {
        "customer": "cus_1",
        "default_payment_method": "pm_plan",
        "current_period_end": _ts(days=20),
    } | over


async def _moved(session, stripe):
    org, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    assert reg.filing["monthly_item_id"] == "si_10dlc"
    return org, reg


async def test_plan_deleted_recreates_a_standalone_subscription(session, stripe):
    org, reg = await _moved(session, stripe)
    plan = _deleted_plan()

    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, plan)
    await session.commit()

    kwargs = stripe.Subscription.create.call_args.kwargs
    assert kwargs["customer"] == "cus_1"
    assert kwargs["default_payment_method"] == "pm_plan"
    assert kwargs["trial_end"] == plan["current_period_end"]
    assert kwargs["metadata"] == {"kind": "tendlc_fee", "registration_id": str(reg.id)}
    price = kwargs["items"][0]["price_data"]
    assert price["unit_amount"] == 1250 and price["product"] == "prod_10dlc"
    assert reg.subscription_id == "sub_new"
    assert "monthly_item_id" not in reg.filing and "monthly_on" not in reg.filing
    assert (await session.execute(sa.select(SecurityAlert))).scalars().all() == []


async def test_restore_falls_back_to_the_customers_default_card(session, stripe):
    org, reg = await _moved(session, stripe)
    plan = _deleted_plan(default_payment_method=None)
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, plan)
    assert stripe.Subscription.create.call_args.kwargs["default_payment_method"] == "pm_cust"


async def test_restore_without_any_card_alerts_ops(session, stripe):
    org, reg = await _moved(session, stripe)
    stripe.Customer.retrieve.return_value = {"invoice_settings": {}}
    plan = _deleted_plan(default_payment_method=None)
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, plan)
    await session.commit()
    stripe.Subscription.create.assert_not_called()
    alert = (await session.execute(sa.select(SecurityAlert))).scalar_one()
    assert alert.kind == "tendlc_monthly_orphan"
    assert reg.filing["monthly_item_id"] == "si_10dlc"


async def test_restore_alerts_when_the_new_subscription_is_not_active(session, stripe):
    org, reg = await _moved(session, stripe)
    stripe.Subscription.create.return_value = {"id": "sub_new", "status": "incomplete"}
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, _deleted_plan())
    await session.commit()
    alert = (await session.execute(sa.select(SecurityAlert))).scalar_one()
    assert alert.detail["status"] == "incomplete"


async def test_plan_deleted_failure_opens_an_ops_alert(session, stripe):
    org, reg = await _moved(session, stripe)
    stripe.Subscription.create.side_effect = RuntimeError("card declined")
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, _deleted_plan())
    await session.commit()
    alert = (await session.execute(sa.select(SecurityAlert))).scalar_one()
    assert alert.kind == "tendlc_monthly_orphan"
    assert alert.detail["registration_id"] == str(reg.id)


async def test_other_plan_deleted_leaves_the_registration_alone(session, stripe):
    org, reg = await _moved(session, stripe)
    other = _deleted_plan()
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, "sub_other", other)
    stripe.Subscription.create.assert_not_called()


async def test_registration_card_shows_the_reused_brand_price(session, stripe):
    _, reg = await _active(session)
    reg.filing = {**reg.filing, "brand_reused": True}
    assert tendlc.public(reg, None, None)["due_today_cents"] == 6050


async def test_plan_discount_sync_leaves_the_tendlc_item_undiscounted(
    session, stripe, monkeypatch
):
    from app.services import discounts

    org, _ = await _active(session)
    await discounts.set_discount(
        session,
        org.id,
        "subscription",
        percent_bps=3000,
        ends_at=None,
        note=None,
        actor_user_id=None,
    )
    await session.commit()
    modify = Mock(return_value={})
    remote = {
        "items": {
            "data": [
                {"id": "si_plan", "price": {"id": "price_plan"}},
                {
                    "id": "si_10dlc",
                    "price": {"id": "price_10dlc"},
                    "metadata": {"kind": "tendlc_monthly"},
                },
            ]
        }
    }
    fake = SimpleNamespace(
        Subscription=SimpleNamespace(retrieve=Mock(return_value=remote), modify=modify),
        Coupon=SimpleNamespace(create=Mock(return_value={"id": "co_1"})),
    )
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)
    assert await discounts.sync_subscription(session, _settings(), org.id) is True
    ids = {i["id"]: i for i in modify.call_args.kwargs["items"]}
    assert ids["si_plan"]["discounts"] == [{"coupon": "co_1"}]
    assert "si_10dlc" not in ids  # untouched, so never discounted


async def test_no_discount_category_for_10dlc_and_a_stray_row_changes_nothing(
    session, stripe
):
    from app.models import OrgDiscount
    from app.services import discounts

    org, brand, campaign, _ = await _workspace(session)
    with pytest.raises(ValidationFailedError):
        await discounts.set_discount(
            session, org.id, "tendlc", percent_bps=2000, ends_at=None, note=None,
            actor_user_id=None,
        )
    set_org_context(session, org.id)
    session.add(OrgDiscount(org_id=org.id, category="tendlc", percent_bps=5000))
    await session.commit()

    await _checkout(session, org, brand, campaign)
    total = stripe.checkout.Session.create.call_args.kwargs
    assert tendlc.quote("standard")["due_today_cents"] == 6650
    assert tendlc.quote("sole_proprietor")["due_today_cents"] == 3950
    assert "service_discount_bps" not in total["subscription_data"]["metadata"]
    assert tendlc.quote("standard")["service_discount_cents"] == 0


async def test_cancel_applied_at_stripe_but_never_saved_still_gets_the_item(session, stripe):
    def cancel(*a, **kw):
        stripe.state["own"] = {**stripe.state["own"], "status": "canceled"}
        raise TimeoutError("response lost")

    stripe.Subscription.cancel.side_effect = cancel
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    assert reg.filing["monthly_cancelling"] == PLAN_SUB
    assert "own_sub_cancelled" not in reg.filing

    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is True
    assert reg.filing["monthly_item_id"] == "si_10dlc"
    assert "monthly_standalone" not in reg.filing
    assert (await session.execute(sa.select(SecurityAlert))).scalars().all() == []


async def test_canceled_own_subscription_without_a_marker_alerts_ops(session, stripe):
    stripe.state["own"] = {"status": "canceled", "latest_invoice": "i", "trial_end": _ts(days=-1)}
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    alert = (await session.execute(sa.select(SecurityAlert))).scalar_one()
    assert alert.kind == "tendlc_monthly_orphan"
    assert reg.filing["monthly_standalone"] is True


async def test_item_idempotency_key_names_the_plan_subscription(session, stripe):
    _, reg = await _active(session)
    await tendlc.move_monthly_to_plan(session, _settings(), reg)
    key = stripe.SubscriptionItem.create.call_args.kwargs["idempotency_key"]
    assert key == f"tendlc-monthly-{reg.id}-{PLAN_SUB}"


async def test_restore_clears_the_move_bookkeeping(session, stripe):
    org, reg = await _moved(session, stripe)
    reg.filing = {
        **reg.filing,
        "monthly_window": "2026-01-01T00:00:00+00:00",
        "orphan_alerted": True,
        "monthly_cancelling": PLAN_SUB,
    }
    await session.commit()
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, _deleted_plan())
    for key in ("monthly_window", "orphan_alerted", "monthly_cancelling", "monthly_item_id"):
        assert key not in reg.filing


@pytest.mark.parametrize("field", ["discount", "discounts"])
async def test_customer_level_discount_gets_no_move(session, stripe, field):
    stripe.state["plan"]["customer"] = "cus_1"
    stripe.Customer.retrieve.return_value = {field: {"coupon": "c"} if field == "discount" else [1]}
    _, reg = await _active(session)
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    stripe.Subscription.cancel.assert_not_called()
    stripe.SubscriptionItem.create.assert_not_called()


async def test_cancel_failing_for_real_then_plan_deleted_creates_nothing(session, stripe):
    org, reg = await _active(session)
    stripe.Subscription.cancel.side_effect = RuntimeError("stripe down")
    assert await tendlc.move_monthly_to_plan(session, _settings(), reg) is False
    assert "monthly_on" not in reg.filing  # only the marker names the target
    assert reg.filing["monthly_cancelling"] == PLAN_SUB

    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, _deleted_plan())
    stripe.Subscription.create.assert_not_called()
    assert reg.subscription_id == "sub_tendlc"


async def test_restore_skips_when_the_own_subscription_still_bills(session, stripe):
    org, reg = await _moved(session, stripe)
    stripe.state["own"] = {**stripe.state["own"], "status": "trialing"}
    await tendlc.restore_standalone_monthly(session, _settings(), org.id, PLAN_SUB, _deleted_plan())
    stripe.Subscription.create.assert_not_called()
    assert reg.subscription_id == "sub_tendlc"
    assert "monthly_item_id" not in reg.filing and "monthly_on" not in reg.filing
