from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.api.routes import billing as billing_routes
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import AccountNotVerifiedError, PermissionDeniedError
from app.models import (
    SUBSCRIPTION_STATUSES,
    Org,
    OrgMembership,
    Plan,
    Role,
    Subscription,
    is_entitled,
)
from app.repositories import users as users_repo
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


# ----------------------------------------------------------------------------------
# Startup bootstrap of the sample catalogue (app/main.py lifespan)
# ----------------------------------------------------------------------------------
async def _plan_codes(session) -> set[str]:
    session.expire_all()
    return set(
        (
            await session.execute(
                sa.select(Plan.code).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars()
    )


async def _load_plan(session, code: str) -> Plan:
    session.expire_all()
    return (
        await session.execute(
            sa.select(Plan)
            .where(Plan.code == code)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()


async def _run_startup(monkeypatch) -> None:
    """Boot the REAL app through its lifespan, so these tests exercise the wiring in
    app/main.py rather than calling the seeder themselves.

    init_engine/dispose_engine are stubbed out for the duration: the lifespan would
    otherwise replace the fixture's engine with a second, empty in-memory SQLite database
    (StaticPool gives each engine its own) and then dispose it, so the seeding under test
    would land somewhere the assertions cannot see. Everything else in the lifespan - and
    in particular the bootstrap call itself - runs untouched.
    """
    from app import main as main_module

    application = main_module.create_app(make_settings())
    monkeypatch.setattr(main_module, "init_engine", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "dispose_engine", _anoop)
    async with application.router.lifespan_context(application):
        pass


async def _anoop(*args, **kwargs) -> None:
    return None


async def test_app_startup_seeds_the_three_sample_plans(engine, session, monkeypatch):
    assert await _plan_codes(session) == set()

    await _run_startup(monkeypatch)

    assert await _plan_codes(session) == {"starter", "standard", "professional"}


async def test_app_startup_twice_still_leaves_exactly_three_plans(
    engine, session, monkeypatch
):
    await _run_startup(monkeypatch)
    await _run_startup(monkeypatch)

    count = (
        await session.execute(
            sa.select(sa.func.count(Plan.code)).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalar_one()
    assert count == 3


async def test_app_startup_does_not_revert_an_operator_pricing_edit(
    engine, session, monkeypatch
):
    """The deploy-reverts-pricing regression: an operator sets a real price and a real
    Stripe price id, the service redeploys, and the placeholders must NOT come back."""
    await _run_startup(monkeypatch)

    starter = await _load_plan(session, "starter")
    starter.name = "Starter (Operator)"
    starter.monthly_price_micros = 29_000_000
    starter.stripe_price_id = "price_live_starter"
    starter.included = {"sms_segments": 777, "voice_minutes": 300, "numbers": 1, "seats": 3}
    starter.is_active = False
    await session.commit()

    await _run_startup(monkeypatch)

    starter = await _load_plan(session, "starter")
    assert starter.name == "Starter (Operator)"
    assert starter.monthly_price_micros == 29_000_000
    assert starter.stripe_price_id == "price_live_starter"
    assert starter.included["sms_segments"] == 777
    assert starter.is_active is False
    assert await _plan_codes(session) == {"starter", "standard", "professional"}


async def test_startup_seeding_failure_does_not_stop_the_app_booting(
    engine, session, monkeypatch
):
    """Logged-and-continued, not fatal: a catalogue that cannot be written must not take
    auth, messaging and webhooks down with it."""

    async def boom(_session):
        raise RuntimeError("plans table is not there yet")

    monkeypatch.setattr(plans_svc, "seed_sample_plans", boom)

    await _run_startup(monkeypatch)

    assert await _plan_codes(session) == set()


# ------------------------------------------------------------------------------------
# GET /api/v1/billing/plans - the catalogue the frontend plan picker reads
# ------------------------------------------------------------------------------------
PLAN_CONTRACT_FIELDS = {
    "code",
    "name",
    "included",
    "overage_rates",
    "monthly_price_micros",
    "stripe_price_id",
    "is_active",
}


async def _scoped_member(client, session, org_id, email, permissions):
    """A second user in the org holding EXACTLY `permissions` - never "*"."""
    token = await register_and_login(client, email)
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, email)
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"scoped-{uuid.uuid4().hex[:8]}",
        permissions=list(permissions),
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def _set_plan_fields(session, code, **fields):
    plan = (
        await session.execute(
            sa.select(Plan)
            .where(Plan.code == code)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    for key, value in fields.items():
        setattr(plan, key, value)
    await session.commit()


async def test_plans_endpoint_returns_the_catalogue_in_the_deliberate_order(
    client, session
):
    token = await register_and_login(client, "plans-list@example.com")
    org = await create_org(client, token, "Plans List Org")
    org_id = uuid.UUID(org["id"])
    await _seed_plans(session)

    r = await client.get("/api/v1/billing/plans", headers=auth_headers(token, org_id))

    assert r.status_code == 200, r.text
    body = r.json()
    # starter, standard, professional - the ladder, not alphabetical (which would put
    # professional first) and not insertion order.
    assert [row["code"] for row in body] == ["starter", "standard", "professional"]
    assert [row["name"] for row in body] == ["Starter", "Standard", "Professional"]
    for row in body:
        assert set(row) == PLAN_CONTRACT_FIELDS
        assert isinstance(row["included"], dict)
        assert isinstance(row["overage_rates"], dict)
        assert isinstance(row["monthly_price_micros"], int)
        assert isinstance(row["is_active"], bool)
    starter = body[0]
    assert starter["included"]["sms_segments"] == 500
    assert starter["overage_rates"]["voice_minutes"] == 12_000


async def test_plans_endpoint_returns_plans_with_and_without_a_stripe_price_id(
    client, session
):
    """The pair that stops the null case passing vacuously: one plan priced, one not,
    and BOTH must come back with stripe_price_id verbatim."""
    token = await register_and_login(client, "plans-price-ids@example.com")
    org = await create_org(client, token, "Plans Price Ids Org")
    org_id = uuid.UUID(org["id"])
    await _seed_plans(session)
    await _set_plan_fields(session, "standard", stripe_price_id="price_live_standard")

    r = await client.get("/api/v1/billing/plans", headers=auth_headers(token, org_id))

    assert r.status_code == 200, r.text
    by_code = {row["code"]: row for row in r.json()}
    assert set(by_code) == {"starter", "standard", "professional"}

    # Not filtered out, not coerced to "": the field is present and is null.
    assert "stripe_price_id" in by_code["starter"]
    assert by_code["starter"]["stripe_price_id"] is None
    assert "stripe_price_id" in by_code["professional"]
    assert by_code["professional"]["stripe_price_id"] is None
    # ... and the configured one survives intact.
    assert by_code["standard"]["stripe_price_id"] == "price_live_standard"


async def test_plans_endpoint_returns_inactive_plans_flagged_not_hidden(client, session):
    token = await register_and_login(client, "plans-inactive@example.com")
    org = await create_org(client, token, "Plans Inactive Org")
    org_id = uuid.UUID(org["id"])
    await _seed_plans(session)
    await _set_plan_fields(session, "professional", is_active=False)

    r = await client.get("/api/v1/billing/plans", headers=auth_headers(token, org_id))

    assert r.status_code == 200, r.text
    by_code = {row["code"]: row for row in r.json()}
    assert set(by_code) == {"starter", "standard", "professional"}
    assert by_code["professional"]["is_active"] is False
    assert by_code["starter"]["is_active"] is True


async def test_plans_endpoint_is_gated_on_settings_read(client, session):
    token = await register_and_login(client, "plans-perm-owner@example.com")
    org = await create_org(client, token, "Plans Permission Org")
    org_id = uuid.UUID(org["id"])
    await _seed_plans(session)

    denied_token = await _scoped_member(
        client, session, org_id, "plans-perm-denied@example.com", ["calls:read"]
    )
    denied = await client.get(
        "/api/v1/billing/plans", headers=auth_headers(denied_token, org_id)
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "permission_denied"

    allowed_token = await _scoped_member(
        client, session, org_id, "plans-perm-allowed@example.com", ["settings:read"]
    )
    allowed = await client.get(
        "/api/v1/billing/plans", headers=auth_headers(allowed_token, org_id)
    )
    assert allowed.status_code == 200, allowed.text
    assert [row["code"] for row in allowed.json()] == [
        "starter",
        "standard",
        "professional",
    ]


async def test_plans_endpoint_requires_authentication(client):
    r = await client.get("/api/v1/billing/plans")
    assert r.status_code in (401, 403), r.text
