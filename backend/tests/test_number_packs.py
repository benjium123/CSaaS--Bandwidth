"""Number packs: 25 for $105, 50 for $195, 100 for $350 a month, discounted off the $5 add-on
number. Bought from Billing on top of a plan; pack numbers count before $5 extras, and the
extras a new pack covers are dropped in the same change.

Uses the stateful Stripe double from test_workspace_plans, so these tests prove what the
subscription ends up carrying and what the workspace may use.
"""

from __future__ import annotations

import pytest

from app.errors import (
    FeatureUnavailableError,
    PriceConfirmationRequiredError,
    ValidationFailedError,
)
from app.services import number_purchases, plan_billing, stripe_client
from tests.conftest import make_settings
from tests import test_workspace_plans as twp
from unittest.mock import AsyncMock

SETTINGS = make_settings(
    stripe_webhook_secret="whsec_test",
    stripe_number_pack_25_price_id="price_pack_25",
    stripe_number_pack_50_price_id="price_pack_50",
    stripe_number_pack_100_price_id="price_pack_100",
)
CENTS = {
    **twp.CENTS,
    "price_pack_25": 10500,
    "price_pack_50": 19500,
    "price_pack_100": 35000,
}


class PackStripe(twp.FakeStripe):
    def _price(self, price_id):
        out = super()._price(price_id) if price_id in twp.CENTS else {
            "id": price_id,
            "active": True,
            "currency": "usd",
            "recurring": {"interval": "month", "interval_count": 1},
        }
        out["unit_amount"] = CENTS[price_id]
        return out

    def monthly_cents(self, sid) -> int:
        return sum(CENTS[i["price"]["id"]] * i["quantity"] for i in self.subs[sid]["items"]["data"])


@pytest.fixture
def stripe(monkeypatch):
    fake = PackStripe()
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)
    monkeypatch.setattr(number_purchases, "require_carrier_funds", AsyncMock())
    return fake


async def _on_plan(session, stripe, org, plan="team", extras=()):
    remote = stripe.add_subscription(org.id, plan_billing.plan_price_id(SETTINGS, plan), extras)
    await plan_billing.upsert_from_stripe(session, SETTINGS, remote, org.id)
    await session.commit()
    return remote["id"]


async def test_a_pack_replaces_the_5_dollar_extras_it_covers(session, stripe):
    org = await twp._org(session)
    sid = await _on_plan(session, stripe, org, "team", [(SETTINGS.stripe_extra_number_price_id, 27)])
    await twp._hold_numbers(session, org, 30)

    # 27 extras ($135) become one 25-pack ($105) + 2 extras ($10): $20 a month less.
    with pytest.raises(PriceConfirmationRequiredError) as quote:
        await plan_billing.buy_pack(session, SETTINGS, org.id, "25", None)
    assert quote.value.quote["monthly_increase_cents"] == -2000

    ent = await plan_billing.buy_pack(session, SETTINGS, org.id, "25", -2000)
    assert ent.pack_dict == {"25": 1}
    assert ent.extra_numbers == 2
    assert ent.numbers == 30
    assert stripe.monthly_cents(sid) == 4500 + 10500 + 2 * 500
    assert stripe.modify_calls[-1]["proration_behavior"] == "always_invoice"


async def test_a_stale_quote_is_refused_and_nothing_changes(session, stripe):
    org = await twp._org(session)
    sid = await _on_plan(session, stripe, org, "team")
    with pytest.raises(PriceConfirmationRequiredError):
        await plan_billing.buy_pack(session, SETTINGS, org.id, "50", 10500)
    assert stripe.modify_calls == []
    assert stripe.monthly_cents(sid) == 4500


async def test_packs_are_not_sold_until_their_stripe_price_exists(session, stripe):
    org = await twp._org(session)
    await _on_plan(session, stripe, org, "team")
    bare = make_settings(
        stripe_webhook_secret="whsec_test",
        stripe_number_pack_25_price_id="",
        stripe_number_pack_50_price_id="",
        stripe_number_pack_100_price_id="",
    )
    with pytest.raises(FeatureUnavailableError):
        await plan_billing.buy_pack(session, bare, org.id, "25", 10500)
    with pytest.raises(ValidationFailedError):
        await plan_billing.buy_pack(session, SETTINGS, org.id, "30", 0)


async def test_pack_room_is_used_before_any_extra_number_is_bought(session, stripe):
    org = await twp._org(session)
    await _on_plan(session, stripe, org, "team", [("price_pack_25", 1)])
    await twp._hold_numbers(session, org, 3)
    ent = await plan_billing.reserve_numbers(session, SETTINGS, org.id, 25, None)
    assert ent.numbers == 28
    assert ent.extra_numbers == 0
    assert stripe.modify_calls == []


async def test_removing_a_pack_needs_the_numbers_to_fit_and_never_charges(session, stripe):
    org = await twp._org(session)
    sid = await _on_plan(session, stripe, org, "team", [("price_pack_25", 2)])
    await twp._hold_numbers(session, org, 20)  # room 53; without one pack 28
    with pytest.raises(ValidationFailedError) as refused:
        await plan_billing.remove_pack(session, SETTINGS, org.id, "50")
    assert "no pack" in str(refused.value).lower()

    ent = await plan_billing.remove_pack(session, SETTINGS, org.id, "25")
    assert ent.pack_dict == {"25": 1}
    assert stripe.modify_calls[-1]["proration_behavior"] == "none"
    assert stripe.monthly_cents(sid) == 4500 + 10500

    with pytest.raises(ValidationFailedError) as in_use:
        await plan_billing.remove_pack(session, SETTINGS, org.id, "25")
    assert in_use.value.code == "pack_in_use"


async def test_changing_plan_keeps_packs_and_resizes_only_the_extras(session, stripe):
    org = await twp._org(session)
    sid = await _on_plan(
        session,
        stripe,
        org,
        "team",
        [("price_pack_25", 1), (SETTINGS.stripe_extra_number_price_id, 2)],
    )
    await twp._hold_numbers(session, org, 30)
    # Business includes 10 numbers: 25 in the pack + 10 >= 30, so both extras go.
    with pytest.raises(PriceConfirmationRequiredError) as quote:
        await plan_billing.change_plan(session, SETTINGS, org.id, "business", None)
    assert quote.value.quote["monthly_total_cents"] == 13000 + 10500
    ent = await plan_billing.change_plan(session, SETTINGS, org.id, "business", 23500)
    assert ent.pack_dict == {"25": 1}
    assert ent.extra_numbers == 0
    assert stripe.monthly_cents(sid) == 13000 + 10500


async def test_a_first_checkout_carrying_a_pack_is_refused(session, stripe):
    org = await twp._org(session)
    remote = stripe.add_subscription(
        org.id, SETTINGS.stripe_plan_team_price_id, [("price_pack_25", 1)]
    )
    with pytest.raises(ValidationFailedError):
        plan_billing.verify_checkout_subscription(SETTINGS, remote, "team", 3)


async def test_summary_lists_each_pack_and_what_the_workspace_holds(session, stripe):
    org = await twp._org(session)
    await _on_plan(session, stripe, org, "team", [("price_pack_50", 1)])
    out = await plan_billing.summary(session, SETTINGS, org.id)
    packs = {p["code"]: p for p in out["number_packs"]}
    assert packs["25"]["price_cents"] == 10500 and packs["25"]["per_number_cents"] == 420
    assert packs["50"]["owned"] == 1 and packs["100"]["owned"] == 0
    assert all(p["available"] for p in packs.values())
    assert out["numbers"]["limit"] == 53 and out["numbers"]["in_packs"] == 50
    assert out["plan"]["monthly_total_cents"] == 4500 + 19500


async def test_a_subscription_with_the_same_pack_twice_is_refused(session, stripe):
    org = await twp._org(session)
    remote = stripe.add_subscription(
        org.id, SETTINGS.stripe_plan_team_price_id, [("price_pack_25", 1), ("price_pack_25", 1)]
    )
    with pytest.raises(ValidationFailedError):
        plan_billing.parse_items(SETTINGS, remote)
