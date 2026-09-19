from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.api.routes import billing as billing_routes
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import AccountNotVerifiedError, PermissionDeniedError
from app.models import SUBSCRIPTION_STATUSES, Org, Plan, Subscription, is_entitled
from app.services import plans as plans_svc
from app.services import stripe_client, telephony_access
from tests.conftest import auth_headers, create_org, make_settings, register_and_login


def _as_utc(value):
    """SQLite gives back naive datetimes for DateTime(timezone=True); Postgres gives aware
    ones. Normalise so the assertion tests the INSTANT, not the backend."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _sub_event(event_id, type, obj):
    return {"id": event_id, "type": type, "data": {"object": obj}}


async def _post_event(client, monkeypatch, event):
    monkeypatch.setattr(
        stripe_client,
        "verify_webhook",
        lambda settings, payload, sig: event,
    )
    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=test,sig=test"},
    )
    assert r.status_code == 204, r.text
    return r


async def _api_org(client, email, name="Subscription Org") -> uuid.UUID:
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return uuid.UUID(org["id"])


async def _seed_plans(session):
    await plans_svc.seed_sample_plans(session)
    await session.commit()


async def _read_sub(session, stripe_subscription_id):
    session.expire_all()
    stmt = (
        sa.select(Subscription)
        .where(Subscription.stripe_subscription_id == stripe_subscription_id)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _give_subscription(session, org_id, status, plan_code="starter"):
    await plans_svc.seed_sample_plans(session)
    await session.commit()

    set_org_context(session, org_id)
    sub = Subscription(
        id=uuid.uuid4(),
        org_id=org_id,
        plan_code=plan_code,
        stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
        stripe_customer_id=f"cus_{uuid.uuid4().hex}",
        status=status,
        current_period_end=datetime.now(timezone.utc),
        cancel_at_period_end=False,
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    return sub


async def test_seeding_twice_creates_exactly_three_plans(session):
    await plans_svc.seed_sample_plans(session)
    await session.commit()

    second = await plans_svc.seed_sample_plans(session)
    await session.commit()

    assert second == []

    count = (
        await session.execute(
            sa.select(sa.func.count(Plan.code)).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalar_one()
    assert count == 3

    codes = set(
        (
            await session.execute(
                sa.select(Plan.code).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars()
    )
    assert codes == {"starter", "standard", "professional"}


async def test_seeded_plans_carry_placeholder_price_and_no_stripe_price_id(session):
    await _seed_plans(session)

    rows = (
        await session.execute(
            sa.select(Plan).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()

    assert len(rows) == 3
    assert all(row.monthly_price_micros == 0 for row in rows)
    assert all(row.stripe_price_id is None for row in rows)


async def test_reseeding_does_not_overwrite_an_operator_edit(session):
    await _seed_plans(session)

    starter = (
        await session.execute(
            sa.select(Plan)
            .where(Plan.code == "starter")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    starter.name = "Operator Renamed"
    starter.stripe_price_id = "price_operator"
    await session.commit()

    await plans_svc.seed_sample_plans(session)
    await session.commit()

    session.expire_all()
    starter = (
        await session.execute(
            sa.select(Plan)
            .where(Plan.code == "starter")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert starter.name == "Operator Renamed"
    assert starter.stripe_price_id == "price_operator"


async def test_a_plan_without_a_stripe_price_id_cannot_be_checked_out(client, session):
    token = await register_and_login(client, "checkout-no-price@example.com")
    org = await create_org(client, token, "Checkout No Price")
    org_id = uuid.UUID(org["id"])
    await _seed_plans(session)

    r = await client.post(
        "/api/v1/billing/subscription/checkout",
        json={"plan_code": "starter"},
        headers=auth_headers(token, org_id),
    )

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["error"]["code"] == "feature_unavailable"
    assert "starter" in body["error"]["message"]
    assert "Starter" in body["error"]["message"]


async def test_checkout_with_a_configured_price_id_reaches_stripe_in_subscription_mode(
    client, session, monkeypatch
):
    token = await register_and_login(client, "checkout-configured@example.com")
    org = await create_org(client, token, "Checkout Configured")
    org_id = uuid.UUID(org["id"])
    await _seed_plans(session)

    starter = (
        await session.execute(
            sa.select(Plan)
            .where(Plan.code == "starter")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    starter.stripe_price_id = "price_test_123"
    await session.commit()

    captured = {}

    async def fake_create_subscription_checkout_session(
        settings,
        *,
        org,
        price_id,
        plan_code,
        success_url,
        cancel_url,
        customer_id=None,
        customer_email=None,
    ):
        captured.update(
            {
                "org": org,
                "price_id": price_id,
                "plan_code": plan_code,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "customer_id": customer_id,
                "customer_email": customer_email,
            }
        )
        return {
            "id": "cs_1",
            "url": "https://stripe.test/cs_1",
            "subscription_id": "sub_1",
        }

    monkeypatch.setattr(
        billing_routes.stripe_client,
        "create_subscription_checkout_session",
        fake_create_subscription_checkout_session,
    )

    r = await client.post(
        "/api/v1/billing/subscription/checkout",
        json={"plan_code": "starter"},
        headers=auth_headers(token, org_id),
    )

    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"] == "https://stripe.test/cs_1"
    assert captured["price_id"] == "price_test_123"
    assert captured["plan_code"] == "starter"


async def test_the_stripe_client_asks_for_subscription_mode_and_the_right_price(
    monkeypatch,
):
    class _FakeSessions:
        def __init__(self):
            self.params = None

        def create(self, **params):
            self.params = params
            return {
                "id": "cs_1",
                "url": "https://stripe.test/cs_1",
                "subscription": "sub_1",
            }

    class _FakeCheckout:
        def __init__(self):
            self.Session = _FakeSessions()

    class _FakeStripe:
        def __init__(self):
            self.checkout = _FakeCheckout()

    class _Org:
        def __init__(self):
            self.id = uuid.uuid4()
            self.name = "Checkout Org"

    fake = _FakeStripe()
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)

    org = _Org()
    result = await stripe_client.create_subscription_checkout_session(
        make_settings(),
        org=org,
        price_id="price_test_123",
        plan_code="starter",
        success_url="https://success.test",
        cancel_url="https://cancel.test",
    )

    params = fake.checkout.Session.params
    assert params is not None
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": "price_test_123", "quantity": 1}]
    assert params["subscription_data"]["metadata"]["org_id"] == str(org.id)
    assert params["subscription_data"]["metadata"]["plan_code"] == "starter"
    assert result["url"] == "https://stripe.test/cs_1"


async def test_checkout_requires_authentication(client):
    r = await client.post(
        "/api/v1/billing/subscription/checkout",
        json={"plan_code": "starter"},
    )
    assert r.status_code in (401, 403), r.text


async def test_checkout_session_completed_creates_active_subscription_and_links_org_plan(
    client, session, monkeypatch
):
    org_id = await _api_org(client, "webhook-active@example.com", "Webhook Active Org")
    await _seed_plans(session)

    event = _sub_event(
        "evt_checkout_active",
        "checkout.session.completed",
        {
            "mode": "subscription",
            "status": "complete",
            "payment_status": "paid",
            "subscription": "sub_A",
            "customer": "cus_A",
            "metadata": {
                "org_id": str(org_id),
                "plan_code": "starter",
                "kind": "subscription",
            },
        },
    )
    await _post_event(client, monkeypatch, event)

    sub = await _read_sub(session, "sub_A")
    assert sub is not None
    assert sub.status == "active"
    assert sub.plan_code == "starter"
    assert sub.org_id == org_id
    assert sub.stripe_customer_id == "cus_A"

    session.expire_all()
    org = (
        await session.execute(
            sa.select(Org)
            .where(Org.id == org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert org.plan_code == "starter"


async def test_checkout_session_completed_unpaid_is_incomplete(
    client, session, monkeypatch
):
    org_id = await _api_org(client, "webhook-unpaid@example.com", "Webhook Unpaid Org")
    await _seed_plans(session)

    event = _sub_event(
        "evt_checkout_unpaid",
        "checkout.session.completed",
        {
            "mode": "subscription",
            "status": "open",
            "payment_status": "unpaid",
            "subscription": "sub_unpaid",
            "customer": "cus_unpaid",
            "metadata": {
                "org_id": str(org_id),
                "plan_code": "starter",
                "kind": "subscription",
            },
        },
    )
    await _post_event(client, monkeypatch, event)

    sub = await _read_sub(session, "sub_unpaid")
    assert sub is not None
    assert sub.status == "incomplete"


async def test_customer_subscription_created_trialing_stores_period_and_plan(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client, "webhook-created@example.com", "Webhook Created Org"
    )
    await _seed_plans(session)

    event = _sub_event(
        "evt_sub_created_trialing",
        "customer.subscription.created",
        {
            "id": "sub_B",
            "customer": "cus_B",
            "status": "trialing",
            "current_period_end": 1790000000,
            "cancel_at_period_end": False,
            "metadata": {"org_id": str(org_id), "plan_code": "standard"},
        },
    )
    await _post_event(client, monkeypatch, event)

    sub = await _read_sub(session, "sub_B")
    assert sub is not None
    assert sub.status == "trialing"
    assert _as_utc(sub.current_period_end) == datetime.fromtimestamp(
        1790000000, tz=timezone.utc
    )


async def test_customer_subscription_updated_moves_trialing_to_past_due(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client, "webhook-updated@example.com", "Webhook Updated Org"
    )
    await _seed_plans(session)

    created = _sub_event(
        "evt_created_for_update",
        "customer.subscription.created",
        {
            "id": "sub_B_update",
            "customer": "cus_B_update",
            "status": "trialing",
            "current_period_end": 1790000000,
            "cancel_at_period_end": False,
            "metadata": {"org_id": str(org_id), "plan_code": "standard"},
        },
    )
    await _post_event(client, monkeypatch, created)

    updated = _sub_event(
        "evt_updated_for_update",
        "customer.subscription.updated",
        {
            "id": "sub_B_update",
            "customer": "cus_B_update",
            "status": "past_due",
            "current_period_end": 1790001000,
            "cancel_at_period_end": True,
            "metadata": {"org_id": str(org_id), "plan_code": "standard"},
        },
    )
    await _post_event(client, monkeypatch, updated)

    sub = await _read_sub(session, "sub_B_update")
    assert sub is not None
    assert sub.status == "past_due"
    assert sub.cancel_at_period_end is True
    assert _as_utc(sub.current_period_end) == datetime.fromtimestamp(
        1790001000, tz=timezone.utc
    )


async def test_customer_subscription_deleted_marks_canceled(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client, "webhook-deleted@example.com", "Webhook Deleted Org"
    )
    sub = await _give_subscription(session, org_id, "active")

    event = _sub_event(
        "evt_sub_deleted",
        "customer.subscription.deleted",
        {"id": sub.stripe_subscription_id},
    )
    await _post_event(client, monkeypatch, event)

    row = await _read_sub(session, sub.stripe_subscription_id)
    assert row is not None
    assert row.status == "canceled"


async def test_invoice_payment_failed_moves_active_to_past_due(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client, "webhook-invoice-failed@example.com", "Webhook Invoice Failed Org"
    )
    sub = await _give_subscription(session, org_id, "active")

    event = _sub_event(
        "evt_invoice_failed",
        "invoice.payment_failed",
        {"subscription": sub.stripe_subscription_id, "id": "in_failed_1"},
    )
    await _post_event(client, monkeypatch, event)

    row = await _read_sub(session, sub.stripe_subscription_id)
    assert row is not None
    assert row.status == "past_due"


async def test_invoice_payment_failed_does_not_resurrect_a_canceled_subscription(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client,
        "webhook-invoice-failed-canceled@example.com",
        "Webhook Invoice Failed Canceled Org",
    )
    sub = await _give_subscription(session, org_id, "canceled")

    event = _sub_event(
        "evt_invoice_failed_canceled",
        "invoice.payment_failed",
        {"subscription": sub.stripe_subscription_id, "id": "in_failed_2"},
    )
    await _post_event(client, monkeypatch, event)

    row = await _read_sub(session, sub.stripe_subscription_id)
    assert row is not None
    assert row.status == "canceled"


async def test_the_same_event_id_is_applied_only_once(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client, "webhook-dupe@example.com", "Webhook Duplicate Org"
    )
    sub = await _give_subscription(session, org_id, "active")

    first = _sub_event(
        "evt_dupe",
        "invoice.payment_failed",
        {"subscription": sub.stripe_subscription_id, "id": "in_dupe"},
    )
    await _post_event(client, monkeypatch, first)

    row = await _read_sub(session, sub.stripe_subscription_id)
    assert row is not None
    assert row.status == "past_due"

    second = _sub_event(
        "evt_dupe",
        "customer.subscription.updated",
        {
            "id": sub.stripe_subscription_id,
            "status": "canceled",
            "current_period_end": 1790002000,
            "cancel_at_period_end": True,
        },
    )
    await _post_event(client, monkeypatch, second)

    row = await _read_sub(session, sub.stripe_subscription_id)
    assert row is not None
    assert row.status == "past_due"


async def test_a_different_event_id_does_apply_the_new_status(
    client, session, monkeypatch
):
    org_id = await _api_org(
        client, "webhook-distinct@example.com", "Webhook Distinct Org"
    )
    sub = await _give_subscription(session, org_id, "active")

    event = _sub_event(
        "evt_other",
        "customer.subscription.updated",
        {
            "id": sub.stripe_subscription_id,
            "status": "canceled",
            "current_period_end": 1790002000,
            "cancel_at_period_end": True,
        },
    )
    await _post_event(client, monkeypatch, event)

    row = await _read_sub(session, sub.stripe_subscription_id)
    assert row is not None
    assert row.status == "canceled"


async def test_an_unknown_event_type_changes_nothing_and_does_not_raise(
    client, session, monkeypatch
):
    event = _sub_event(
        "evt_unknown",
        "customer.created",
        {"id": "cus_x"},
    )
    r = await _post_event(client, monkeypatch, event)
    assert r.status_code == 204, r.text

    count = (
        await session.execute(
            sa.select(sa.func.count(Subscription.id)).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("incomplete", False),
        ("incomplete_expired", False),
        ("trialing", True),
        ("active", True),
        ("past_due", True),
        ("canceled", False),
        ("unpaid", False),
        ("paused", False),
        (None, False),
        ("nonsense", False),
    ],
)
async def test_is_entitled_knows_exactly_which_statuses_grant_access(status, expected):
    assert is_entitled(status) is expected


async def test_the_status_tuple_is_exactly_stripes_eight():
    assert set(SUBSCRIPTION_STATUSES) == {
        "incomplete",
        "incomplete_expired",
        "trialing",
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
    }
    assert len(SUBSCRIPTION_STATUSES) == 8


async def test_with_the_flag_off_an_org_with_no_subscription_is_unaffected(
    client, session
):
    """THE REGRESSION PIN: telephony access must remain unchanged when the flag is off."""
    settings = make_settings()
    org_id = await _api_org(client, "gate-off@example.com", "Gate Off Org")

    assert settings.require_subscription_for_telephony is False

    for kind in ("sms", "call", "number"):
        assert await telephony_access.refusal(session, settings, org_id, kind) is None


async def test_with_the_flag_on_no_subscription_is_refused(client, session):
    settings = make_settings(require_subscription_for_telephony=True)
    org_id = await _api_org(client, "gate-no-sub@example.com", "Gate No Sub Org")

    assert (
        await telephony_access.refusal(session, settings, org_id, "sms")
        == "subscription_required"
    )


async def test_with_the_flag_on_a_canceled_subscription_is_refused(
    client, session
):
    settings = make_settings(require_subscription_for_telephony=True)
    org_id = await _api_org(
        client, "gate-canceled-sub@example.com", "Gate Canceled Sub Org"
    )
    await _give_subscription(session, org_id, "canceled")

    assert (
        await telephony_access.refusal(session, settings, org_id, "sms")
        == "subscription_required"
    )


async def test_with_the_flag_on_an_active_subscription_passes(client, session):
    settings = make_settings(require_subscription_for_telephony=True)
    org_id = await _api_org(
        client, "gate-active-sub@example.com", "Gate Active Sub Org"
    )
    await _give_subscription(session, org_id, "active")

    assert await telephony_access.refusal(session, settings, org_id, "sms") is None


async def test_the_refusal_is_its_own_code_not_the_kyc_one(client, session):
    settings = make_settings(require_subscription_for_telephony=True)
    org_id = await _api_org(
        client, "gate-own-code@example.com", "Gate Own Code Org"
    )

    with pytest.raises(PermissionDeniedError) as exc_info:
        await telephony_access.require_telephony_allowed(
            session, org_id, "sms", settings=settings
        )

    assert exc_info.value.code == "subscription_required"
    assert not isinstance(exc_info.value, AccountNotVerifiedError)

    await _give_subscription(session, org_id, "active")
    await telephony_access.require_telephony_allowed(
        session, org_id, "sms", settings=settings
    )
    assert await telephony_access.refusal(session, settings, org_id, "sms") is None


async def test_past_due_still_has_access(client, session):
    settings = make_settings(require_subscription_for_telephony=True)
    org_id = await _api_org(
        client, "gate-past-due@example.com", "Gate Past Due Org"
    )
    await _give_subscription(session, org_id, "past_due")

    assert await telephony_access.refusal(session, settings, org_id, "sms") is None
