# ruff: noqa: F811 - pytest fixtures imported from test_ops_console
"""Per-workspace discounts: pricing, charges, bundles, 10DLC, ops routes, P&L, Stripe."""

from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import CreditLedgerEntry, Message
from app.models.billing_v2 import OrgDiscount
from app.services import (
    bundles,
    credits,
    discounts,
    plan_billing,
    stripe_client,
    telephony_billing,
    tendlc,
)
from tests.conftest import auth_headers
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401


async def _query_discount_rows(session, org_id: uuid.UUID) -> list[OrgDiscount]:
    """Return org discount rows, with tenant context set."""
    set_org_context(session, org_id)
    return list(
        (
            await session.execute(sa.select(OrgDiscount).where(OrgDiscount.org_id == org_id))
        ).scalars()
    )


async def test_apply():
    assert discounts.apply(15_000, 1000) == 13_500
    assert discounts.apply(15_000, 0) == 15_000
    assert discounts.apply(15_000, 10_000) == 0
    assert discounts.apply(0, 500) == 0
    assert discounts.apply(1, 3333) == 0


def test_category_for_metric():
    assert discounts.category_for_metric("number_mrc") == "numbers"
    assert discounts.category_for_metric("number_setup") == "numbers"
    assert discounts.category_for_metric("sms_out") == "usage"
    assert discounts.category_for_metric("voice_min_in") == "usage"
    assert discounts.category_for_metric("fax_page_out") == "usage"


async def test_set_discount_validation(session):
    org_id = await _new_org(session, "Validation Org")

    with pytest.raises(ValidationFailedError):
        await discounts.set_discount(
            session, org_id, "usage", percent_bps=0, ends_at=None, note=None, actor_user_id=None
        )

    with pytest.raises(ValidationFailedError):
        await discounts.set_discount(
            session,
            org_id,
            "usage",
            percent_bps=10_001,
            ends_at=None,
            note=None,
            actor_user_id=None,
        )

    past = datetime.now(timezone.utc) - timedelta(days=1)
    with pytest.raises(ValidationFailedError):
        await discounts.set_discount(
            session, org_id, "usage", percent_bps=1000, ends_at=past, note=None, actor_user_id=None
        )

    with pytest.raises(ValidationFailedError):
        await discounts.set_discount(
            session,
            org_id,
            "not-a-category",
            percent_bps=1000,
            ends_at=None,
            note=None,
            actor_user_id=None,
        )


async def test_unit_price_usage_discount(session):
    org_id = await _new_org(session, "Usage Discount Org")
    await discounts.set_discount(
        session, org_id, "usage", percent_bps=1000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    assert await telephony_billing.unit_price(session, org_id, "bandwidth", "sms_out") == 13_500
    assert (
        await telephony_billing.list_unit_price(session, org_id, "bandwidth", "sms_out") == 15_000
    )
    assert (
        await telephony_billing.unit_price(session, org_id, "bandwidth", "number_mrc") == 15_000_000
    )


async def test_unit_price_numbers_discount(session):
    org_id = await _new_org(session, "Numbers Discount Org")
    await discounts.set_discount(
        session, org_id, "numbers", percent_bps=5000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    assert (
        await telephony_billing.unit_price(session, org_id, "bandwidth", "number_mrc") == 7_500_000
    )
    assert await telephony_billing.unit_price(session, org_id, "bandwidth", "sms_out") == 15_000


async def test_expired_discount_is_ignored(session):
    org_id = await _new_org(session, "Expired Discount Org")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    await discounts.set_discount(
        session, org_id, "usage", percent_bps=2000, ends_at=future, note=None, actor_user_id=None
    )
    await session.commit()

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OrgDiscount).where(
                OrgDiscount.org_id == org_id, OrgDiscount.category == "usage"
            )
        )
    ).scalar_one()
    row.ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.commit()

    discounts.invalidate(session, org_id)
    assert await telephony_billing.unit_price(session, org_id, "bandwidth", "sms_out") == 15_000


async def test_charge_sms_with_usage_discount(session):
    org_id = await _new_org(session, "SMS Discount Org", prepaid=True)
    set_org_context(session, org_id)
    await credits.topup(session, org_id, 1_000_000, reference=f"topup-{uuid.uuid4()}")
    await session.commit()

    await discounts.set_discount(
        session, org_id, "usage", percent_bps=2000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    set_org_context(session, org_id)
    message = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=uuid.uuid4(),
        direction="outbound",
        status="accepted",
        from_e164="+12145550108",
        to_e164="+19725550108",
        body="x",
        media=[],
        carrier="bandwidth",
        segment_count_est=1,
    )
    await telephony_billing.charge_sms(session, org_id, message)
    await session.commit()

    set_org_context(session, org_id)
    rows = list(
        (
            await session.execute(
                sa.select(CreditLedgerEntry)
                .where(CreditLedgerEntry.entry_type == "usage")
                .order_by(CreditLedgerEntry.seq)
            )
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].amount_micros == -12_000
    assert rows[0].discount_micros == 3_000


def test_quote_from_list_workspace_discount():
    q = bundles.quote_from_list("sms", 5, 13_000_000, workspace_bps=1000)
    assert q["list"] == 65_000_000
    assert q["unit_paid"] == 9_360_000
    assert q["paid"] == 46_800_000
    assert q["discount"] == 65_000_000 - 46_800_000

    q = bundles.quote_from_list("sms", 1, 13_000_000, workspace_bps=2500)
    assert q["unit_paid"] == 9_750_000
    assert q["paid"] == 9_750_000
    assert q["discount"] == 3_250_000

    q = bundles.quote_from_list("sms", 1, 1_234_567, workspace_bps=1000)
    assert q["unit_paid"] == 1_110_000
    assert q["paid"] == 1_110_000


async def test_bundle_quote_uses_org_discount(session):
    org_id = await _new_org(session, "Bundle Discount Org")

    base = await bundles.quote(session, "sms", 1, org_id)
    expected_base = bundles.quote_from_list("sms", 1, 13_000_000, workspace_bps=0)
    assert base == expected_base

    await discounts.set_discount(
        session, org_id, "bundles", percent_bps=1000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    quoted = await bundles.quote(session, "sms", 1, org_id)
    expected = bundles.quote_from_list("sms", 1, 13_000_000, workspace_bps=1000)
    assert quoted == expected


def test_tendlc_quote_service_discount():
    tier = next(iter(tendlc.MONTHLY_CENTS))
    base = tendlc.quote(tier)
    discounted = tendlc.quote(tier, service_discount_bps=2000)

    assert discounted["service_fee_cents"] == 400
    assert discounted["service_discount_cents"] == 100
    assert discounted["due_today_cents"] == base["due_today_cents"] - 100


async def test_discount_route_put_and_get(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Route Discount Org")

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={
            "categories": ["usage", "bundles"],
            "percent": 12.5,
            "ends_at": None,
            "note": "loyal",
        },
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stripe"] is None

    by_cat = {d["category"]: d for d in body["discounts"]}
    assert by_cat["usage"]["percent_bps"] == 1250
    assert by_cat["bundles"]["percent_bps"] == 1250

    rows = await _query_discount_rows(session, org_id)
    assert len(rows) == 2
    assert {r.category for r in rows} == {"usage", "bundles"}
    assert all(int(r.percent_bps) == 1250 for r in rows)

    r = await ops.get(f"/api/v1/ops/console/orgs/{org_id}/discounts", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    cats = [d["category"] for d in body["discounts"]]
    assert set(cats) >= {"usage", "bundles"}
    for d in body["discounts"]:
        assert d["active"] is True


async def test_discount_route_subscription_sync(ops, session, ops_settings, monkeypatch):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Sub Sync Route Org")

    async def fake_sync(session, settings, org_id):
        return True

    monkeypatch.setattr(discounts, "sync_subscription", fake_sync)

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["subscription"], "percent": 10, "ends_at": None, "note": None},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["stripe"] == "synced"

    async def fake_raise(session, settings, org_id):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(discounts, "sync_subscription", fake_raise)

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["subscription"], "percent": 12, "ends_at": None, "note": None},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["stripe"] == "failed"

    session.expire_all()
    rows = await _query_discount_rows(session, org_id)
    assert len(rows) == 1
    assert rows[0].percent_bps == 1200


async def test_discount_route_reviewer_permissions(ops, session, ops_settings):
    org_id = await _new_org(session, "Reviewer Discount Org")
    reviewer = await _operator(ops, session, email="discount-reviewer@example.com", role="reviewer")

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["usage"], "percent": 10, "ends_at": None, "note": None},
        headers=auth_headers(reviewer),
    )
    assert r.status_code == 403, r.text

    r = await ops.get(
        f"/api/v1/ops/console/orgs/{org_id}/discounts", headers=auth_headers(reviewer)
    )
    assert r.status_code == 200, r.text


async def test_discount_route_validation(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Validation Route Org")

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["nope"], "percent": 10, "ends_at": None, "note": None},
        headers=auth_headers(token),
    )
    assert 400 <= r.status_code < 500, r.text

    r = await ops.get(f"/api/v1/ops/console/orgs/{org_id}/discounts", headers=auth_headers(token))
    assert r.json()["discounts"] == []

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["usage"], "percent": 0, "ends_at": None, "note": None},
        headers=auth_headers(token),
    )
    assert r.status_code == 422, r.text

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["usage"], "percent": 101, "ends_at": None, "note": None},
        headers=auth_headers(token),
    )
    assert r.status_code == 422, r.text

    r = await ops.get(f"/api/v1/ops/console/orgs/{org_id}/discounts", headers=auth_headers(token))
    assert r.json()["discounts"] == []


async def test_discount_route_delete(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Delete Discount Org")

    await discounts.set_discount(
        session, org_id, "usage", percent_bps=1000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    r = await ops.delete(
        f"/api/v1/ops/console/orgs/{org_id}/discounts/usage", headers=auth_headers(token)
    )
    assert r.status_code == 200, r.text

    r = await ops.get(f"/api/v1/ops/console/orgs/{org_id}/discounts", headers=auth_headers(token))
    assert all(d["category"] != "usage" for d in r.json()["discounts"])

    r = await ops.delete(
        f"/api/v1/ops/console/orgs/{org_id}/discounts/usage", headers=auth_headers(token)
    )
    assert r.status_code == 404, r.text


async def test_discount_route_percent_change_clears_stripe_coupon(
    ops, session, ops_settings, monkeypatch
):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Coupon Clear Org")

    await discounts.set_discount(
        session,
        org_id,
        "subscription",
        percent_bps=1000,
        ends_at=None,
        note=None,
        actor_user_id=None,
    )
    await session.commit()

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OrgDiscount).where(
                OrgDiscount.org_id == org_id, OrgDiscount.category == "subscription"
            )
        )
    ).scalar_one()
    row.stripe_coupon_id = "fake_coupon"
    await session.commit()

    async def fake_sync(session, settings, org_id):
        return True

    monkeypatch.setattr(discounts, "sync_subscription", fake_sync)

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/discounts",
        json={"categories": ["subscription"], "percent": 12.5, "ends_at": None, "note": "change"},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    rows = await _query_discount_rows(session, org_id)
    assert len(rows) == 1
    assert rows[0].percent_bps == 1250
    assert rows[0].stripe_coupon_id is None


async def test_pnl_org_discounts(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "PNL Discount Org", prepaid=True)

    set_org_context(session, org_id)
    await credits.topup(session, org_id, 50_000_000, reference=f"topup-{uuid.uuid4()}")
    await session.commit()

    await discounts.set_discount(
        session, org_id, "usage", percent_bps=1000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    set_org_context(session, org_id)
    await credits.charge_usage(session, org_id, 9_000, reference="t1", discount_micros=1_000)
    await session.commit()

    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()

    org = next(o for o in body["orgs"] if o["org_id"] == str(org_id))
    assert any(d["category"] == "usage" and d["active"] for d in org["discounts"])

    metrics = org["metrics"]
    assert metrics["usage_discount"] == 1_000
    assert metrics["discount_given"] == metrics["discount"] + metrics["usage_discount"]
    assert metrics["discount_given"] >= 1_000
    assert metrics["profit_before_discount"] == metrics["cash_profit"] + metrics["discount_given"]

    totals = body["totals"]
    assert totals["discount_given"] >= 1_000
    assert totals["net_profit_before_discount"] == totals["net_profit"] + totals["discount_given"]


async def test_sync_subscription_both_coupons(session, ops_settings, monkeypatch):
    org_id = await _new_org(session, "Sync Both Org")
    await discounts.set_discount(
        session,
        org_id,
        "subscription",
        percent_bps=1000,
        ends_at=None,
        note=None,
        actor_user_id=None,
    )
    await discounts.set_discount(
        session, org_id, "numbers", percent_bps=2000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()

    class FakeCoupon:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            return {"id": f"co_{self.n}"}

    class FakeSubscription:
        def __init__(self):
            self.modify_calls = []
            self.retrieve_calls = 0

        def retrieve(self, sub_id):
            self.retrieve_calls += 1
            return {
                "items": {
                    "data": [
                        {"id": "si_plan", "price": {"id": "price_plan"}},
                        {"id": "si_num", "price": {"id": "price_num"}},
                    ]
                }
            }

        def modify(self, sub_id, **kwargs):
            self.modify_calls.append((sub_id, kwargs))
            return {"id": sub_id}

    fake_coupon = FakeCoupon()
    fake_subscription = FakeSubscription()
    fake_stripe = types.SimpleNamespace(Subscription=fake_subscription, Coupon=fake_coupon)

    async def fake_entitlement(session, org_id):
        return types.SimpleNamespace(
            subscription=types.SimpleNamespace(stripe_subscription_id="sub_1")
        )

    async def fake_run_sync(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(plan_billing, "entitlement", fake_entitlement)
    monkeypatch.setattr(plan_billing, "number_price_id", lambda settings, interval: "price_num")
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake_stripe)
    monkeypatch.setattr(stripe_client, "_run_sync", fake_run_sync)

    assert await discounts.sync_subscription(session, ops_settings, org_id) is True
    assert fake_subscription.retrieve_calls == 1
    assert len(fake_subscription.modify_calls) == 1

    sub_id, kwargs = fake_subscription.modify_calls[0]
    assert sub_id == "sub_1"
    assert kwargs["discounts"] == ""
    assert kwargs["proration_behavior"] == "none"

    items = {it["id"]: it for it in kwargs["items"]}
    assert items["si_plan"]["discounts"] == [{"coupon": "co_1"}]
    assert items["si_num"]["discounts"] == [{"coupon": "co_2"}]
    assert fake_coupon.n == 2

    # Second sync reuses existing Stripe coupon ids.
    assert await discounts.sync_subscription(session, ops_settings, org_id) is True
    assert fake_coupon.n == 2
    assert fake_subscription.retrieve_calls == 2
    assert len(fake_subscription.modify_calls) == 2

    await session.commit()
    rows = await _query_discount_rows(session, org_id)
    by_cat = {r.category: r for r in rows}
    assert by_cat["subscription"].stripe_coupon_id == "co_1"
    assert by_cat["numbers"].stripe_coupon_id == "co_2"


async def test_sync_subscription_only_subscription(session, ops_settings, monkeypatch):
    org_id = await _new_org(session, "Sync Sub Only Org")
    await discounts.set_discount(
        session,
        org_id,
        "subscription",
        percent_bps=1000,
        ends_at=None,
        note=None,
        actor_user_id=None,
    )
    await session.commit()

    class FakeCoupon:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            return {"id": f"co_{self.n}"}

    class FakeSubscription:
        def __init__(self):
            self.modify_calls = []
            self.retrieve_calls = 0

        def retrieve(self, sub_id):
            self.retrieve_calls += 1
            return {
                "items": {
                    "data": [
                        {"id": "si_plan", "price": {"id": "price_plan"}},
                        {"id": "si_num", "price": {"id": "price_num"}},
                    ]
                }
            }

        def modify(self, sub_id, **kwargs):
            self.modify_calls.append((sub_id, kwargs))
            return {"id": sub_id}

    fake_coupon = FakeCoupon()
    fake_subscription = FakeSubscription()
    fake_stripe = types.SimpleNamespace(Subscription=fake_subscription, Coupon=fake_coupon)

    async def fake_entitlement(session, org_id):
        return types.SimpleNamespace(
            subscription=types.SimpleNamespace(stripe_subscription_id="sub_1")
        )

    async def fake_run_sync(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(plan_billing, "entitlement", fake_entitlement)
    monkeypatch.setattr(plan_billing, "number_price_id", lambda settings, interval: "price_num")
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake_stripe)
    monkeypatch.setattr(stripe_client, "_run_sync", fake_run_sync)

    assert await discounts.sync_subscription(session, ops_settings, org_id) is True
    assert fake_coupon.n == 1

    sub_id, kwargs = fake_subscription.modify_calls[0]
    assert sub_id == "sub_1"

    items = {it["id"]: it for it in kwargs["items"]}
    assert items["si_plan"]["discounts"] == [{"coupon": "co_1"}]
    assert items["si_num"]["discounts"] == ""


async def test_sync_subscription_no_subscription(session, ops_settings, monkeypatch):
    org_id = await _new_org(session, "No Sub Org")
    await discounts.set_discount(
        session,
        org_id,
        "subscription",
        percent_bps=1000,
        ends_at=None,
        note=None,
        actor_user_id=None,
    )
    await session.commit()

    async def fake_entitlement(session, org_id):
        return None

    monkeypatch.setattr(plan_billing, "entitlement", fake_entitlement)

    result = await discounts.sync_subscription(session, ops_settings, org_id)
    assert result is False


async def test_expired_stripe_discount_is_taken_off_the_subscription(
    session, ops_settings, monkeypatch
):
    org_id = await _new_org(session, "Expiry Sweep Org")
    await discounts.set_discount(
        session,
        org_id,
        "subscription",
        percent_bps=1500,
        ends_at=datetime.now(timezone.utc) + timedelta(days=1),
        note=None,
        actor_user_id=None,
    )
    await session.commit()
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(OrgDiscount).where(OrgDiscount.org_id == org_id))
    ).scalar_one()
    row.stripe_coupon_id = "co_live"
    row.ends_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    discounts.invalidate(session, org_id)
    assert await discounts.has_stripe_discount(session, org_id) is True

    modify_calls = []

    class FakeSubscription:
        def retrieve(self, sub_id):
            return {"items": {"data": [{"id": "si_plan", "price": {"id": "price_plan"}}]}}

        def modify(self, sub_id, **kwargs):
            modify_calls.append(kwargs)
            return {"id": sub_id}

    class FakeCoupon:
        def create(self, **kwargs):
            raise AssertionError("an expired discount must not create a coupon")

    fake_stripe = types.SimpleNamespace(Subscription=FakeSubscription(), Coupon=FakeCoupon())

    async def fake_entitlement(session, org_id):
        return types.SimpleNamespace(
            subscription=types.SimpleNamespace(stripe_subscription_id="sub_1")
        )

    async def fake_run_sync(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(plan_billing, "entitlement", fake_entitlement)
    monkeypatch.setattr(plan_billing, "number_price_id", lambda settings, interval: "price_num")
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake_stripe)
    monkeypatch.setattr(stripe_client, "_run_sync", fake_run_sync)

    assert await discounts.expire_stripe_discounts(session, ops_settings) == 1
    assert modify_calls[0]["items"] == [{"id": "si_plan", "discounts": ""}]
    session.expire_all()
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(OrgDiscount).where(OrgDiscount.org_id == org_id))
    ).scalar_one()
    assert row.stripe_coupon_id is None
    discounts.invalidate(session, org_id)
    assert await discounts.has_stripe_discount(session, org_id) is False
    # Nothing left to take off: the next sweep does not call Stripe again.
    assert await discounts.expire_stripe_discounts(session, ops_settings) == 0
    assert len(modify_calls) == 1


async def test_no_discount_means_no_stripe_sync_needed(session):
    org_id = await _new_org(session, "No Discount Org")
    assert await discounts.has_stripe_discount(session, org_id) is False
    await discounts.set_discount(
        session, org_id, "usage", percent_bps=500, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()
    assert await discounts.has_stripe_discount(session, org_id) is False
