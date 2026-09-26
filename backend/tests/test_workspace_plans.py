"""Workspace plans: Starter $15 (1 user + 1 number), Team $45 (3 + 3), Business $130 (10 + 10),
add-on users $15/month ($12 on Business), add-on numbers $5/month. Call minutes are a fixed
pool per workspace: Starter none (pay as you go), Team 200, Business 1,000.

Stripe is faked with a small STATEFUL double: modify() really changes the item quantities
that the next retrieve() returns, so these tests prove what the workspace ends up paying for
and what it may use, not merely that some Stripe method was called.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import (
    FeatureUnavailableError,
    PriceConfirmationRequiredError,
    ValidationFailedError,
)
from app.models import KycProfile, Org, OrgNumber
from app.models.subscriptions import Subscription
from app.providers.numbers import OrderResult
from app.services import number_purchases, plan_billing, seats, stripe_client
from tests.conftest import make_settings

SETTINGS = make_settings(
    stripe_webhook_secret="whsec_test",
    stripe_business_extra_user_price_id="price_test_business_user",
    stripe_plan_solo_year_price_id="price_y_solo",
    stripe_plan_team_year_price_id="price_y_team",
    stripe_plan_business_year_price_id="price_y_business",
    stripe_extra_user_year_price_id="price_y_user",
    stripe_business_extra_user_year_price_id="price_y_business_user",
    stripe_extra_number_year_price_id="price_y_number",
)
#: Yearly prices charge ten months once a year.
YEARLY_CENTS = {
    "price_y_solo": 15000,
    "price_y_team": 45000,
    "price_y_business": 130000,
    "price_y_user": 15000,
    "price_y_business_user": 12000,
    "price_y_number": 5000,
}
CENTS = {
    SETTINGS.stripe_plan_solo_price_id: 1500,
    SETTINGS.stripe_plan_team_price_id: 4500,
    SETTINGS.stripe_plan_business_price_id: 13000,
    SETTINGS.stripe_extra_user_price_id: 1500,
    SETTINGS.stripe_business_extra_user_price_id: 1200,
    SETTINGS.stripe_extra_number_price_id: 500,
    **YEARLY_CENTS,
}


class CardError(Exception):
    user_message = "Your card was declined."


class FakeStripe:
    def __init__(self):
        self.subs: dict[str, dict] = {}
        self.modify_calls: list[dict] = []
        self.checkout_calls: list[dict] = []
        self.decline = False
        self._ids = itertools.count(1)
        self.Price = SimpleNamespace(retrieve=self._price)
        self.Subscription = SimpleNamespace(retrieve=self._retrieve, modify=self._modify)
        self.checkout = SimpleNamespace(
            Session=SimpleNamespace(create=self._checkout, retrieve=lambda _id: {})
        )

    def _price(self, price_id):
        return {
            "id": price_id,
            "active": True,
            "unit_amount": CENTS[price_id],
            "currency": "usd",
            "recurring": {
                "interval": "year" if price_id in YEARLY_CENTS else "month",
                "interval_count": 1,
            },
        }

    def _checkout(self, **kwargs):
        self.checkout_calls.append(kwargs)
        return {"id": f"cs_{next(self._ids)}", "url": "https://checkout.stripe.com/x"}

    def add_subscription(self, org_id, plan_price, extras=()):
        sid = f"sub_{next(self._ids)}"
        items = [{"id": f"si_{next(self._ids)}", "price": {"id": plan_price}, "quantity": 1}]
        for price_id, quantity in extras:
            items.append(
                {"id": f"si_{next(self._ids)}", "price": {"id": price_id}, "quantity": quantity}
            )
        self.subs[sid] = {
            "id": sid,
            "status": "active",
            "customer": "cus_1",
            "metadata": {"kind": "workspace_plan", "org_id": str(org_id)},
            "items": {"data": items},
        }
        return copy.deepcopy(self.subs[sid])

    def _retrieve(self, sid):
        return copy.deepcopy(self.subs[sid])

    def _modify(self, sid, items=None, **kwargs):
        self.modify_calls.append({"items": items, **kwargs})
        if self.decline and kwargs.get("payment_behavior") == "error_if_incomplete":
            raise CardError("declined")
        data = self.subs[sid]["items"]["data"]
        for change in items or []:
            if "id" in change:
                item = next(i for i in data if i["id"] == change["id"])
                if change.get("deleted"):
                    data.remove(item)
                    continue
                if "price" in change:
                    item["price"] = {"id": change["price"]}
                if "quantity" in change:
                    item["quantity"] = change["quantity"]
            else:
                data.append(
                    {
                        "id": f"si_{next(self._ids)}",
                        "price": {"id": change["price"]},
                        "quantity": change["quantity"],
                    }
                )
        return copy.deepcopy(self.subs[sid])

    def monthly_cents(self, sid) -> int:
        return sum(CENTS[i["price"]["id"]] * i["quantity"] for i in self.subs[sid]["items"]["data"])


@pytest.fixture
def stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)
    monkeypatch.setattr(number_purchases, "require_carrier_funds", AsyncMock())
    return fake


async def _org(session) -> Org:
    org = Org(id=uuid.uuid4(), name="Plan Co", slug=uuid.uuid4().hex)
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    session.add(KycProfile(org_id=org.id, status="approved"))
    await session.commit()
    return org


async def _on_plan(session, stripe, org, plan="team", extras=(), interval="month") -> str:
    remote = stripe.add_subscription(org.id, plan_billing.plan_price_id(SETTINGS, plan, interval), extras)
    await plan_billing.upsert_from_stripe(session, SETTINGS, remote, org.id)
    await session.commit()
    return remote["id"]


async def _hold_numbers(session, org, count):
    set_org_context(session, org.id)
    for i in range(count):
        session.add(
            OrgNumber(
                id=uuid.uuid4(),
                org_id=org.id,
                e164=f"+1212555{i:04d}",
                carrier="telnyx",
                status="active",
                is_active=True,
                provisioning={"billing": "workspace_plan"},
            )
        )
    await session.commit()


def _request(carrier):
    registry = SimpleNamespace(get=lambda name: carrier)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SETTINGS, carriers=registry))
    ), registry


# ------------------------------------------------------------------------ first purchase
async def test_first_numbers_need_a_plan_and_check_out_plan_plus_extra_numbers(session, stripe):
    org = await _org(session)
    with pytest.raises(ValidationFailedError) as exc:
        await number_purchases.create(session, SETTINGS, org.id, ["+12125550101"])
    assert exc.value.code == "plan_required"

    numbers = ["+12125550101", "+12125550102", "+12125550103", "+12125550104"]
    purchase = await number_purchases.create(session, SETTINGS, org.id, numbers, plan_code="team")
    call = stripe.checkout_calls[-1]
    # Team includes 3 numbers: the 4th is a $5 add-on. $45 + $5.
    assert call["line_items"] == [
        {"price": SETTINGS.stripe_plan_team_price_id, "quantity": 1},
        {"price": SETTINGS.stripe_extra_number_price_id, "quantity": 1},
    ]
    assert call["subscription_data"]["metadata"] == {
        "kind": "workspace_plan",
        "org_id": str(org.id),
        "plan_code": "team",
    }
    assert call["metadata"]["kind"] == "number_purchase"
    assert purchase.plan_code == "team" and purchase.state == "checkout"


async def test_yearly_checkout_bills_ten_months_and_stays_yearly(session, stripe):
    org = await _org(session)
    numbers = ["+12125550101", "+12125550102", "+12125550103", "+12125550104"]
    purchase = await number_purchases.create(
        session, SETTINGS, org.id, numbers, plan_code="team", billing_interval="year"
    )
    assert stripe.checkout_calls[-1]["line_items"] == [
        {"price": "price_y_team", "quantity": 1},
        {"price": "price_y_number", "quantity": 1},
    ]
    assert purchase.billing_interval == "year"

    # A monthly subscription does not satisfy a yearly cart.
    monthly = stripe.add_subscription(
        org.id, SETTINGS.stripe_plan_team_price_id, [(SETTINGS.stripe_extra_number_price_id, 1)]
    )
    with pytest.raises(ValidationFailedError):
        plan_billing.verify_checkout_subscription(SETTINGS, monthly, "team", 4, "year")

    sid = await _on_plan(session, stripe, org, "team", [("price_y_number", 1)], "year")
    ent = await plan_billing.entitlement(session, org.id)
    assert ent.interval == "year" and ent.period_cents == 50000
    # Add-ons on a yearly plan are quoted and bought per year, at the yearly price.
    with pytest.raises(PriceConfirmationRequiredError) as exc:
        await plan_billing.add_users(session, SETTINGS, org.id, 1, None)
    assert exc.value.quote["monthly_increase_cents"] == 15000
    assert exc.value.quote["interval"] == "year"
    await plan_billing.add_users(session, SETTINGS, org.id, 1, 15000)
    assert stripe.monthly_cents(sid) == 45000 + 5000 + 15000
    assert (await plan_billing.summary(session, SETTINGS, org.id))["plan"]["interval"] == "year"


async def test_a_subscription_mixing_monthly_and_yearly_is_refused(session, stripe):
    org = await _org(session)
    remote = stripe.add_subscription(org.id, "price_y_team", [(SETTINGS.stripe_extra_number_price_id, 1)])
    with pytest.raises(ValidationFailedError):
        plan_billing.parse_items(SETTINGS, remote)


async def test_yearly_is_refused_cleanly_while_its_prices_are_missing(session, stripe):
    org = await _org(session)
    bare = make_settings(
        stripe_webhook_secret="whsec_test", stripe_plan_team_year_price_id="", stripe_extra_number_year_price_id=""
    )
    assert plan_billing.yearly_available(bare) is False
    with pytest.raises(FeatureUnavailableError):
        await number_purchases.create(
            session, bare, org.id, ["+12125550101"], plan_code="team", billing_interval="year"
        )


async def _members(session, org, count):
    from app.models import OrgMembership, Role, User

    role = Role(id=uuid.uuid4(), org_id=org.id, name="agent", permissions=[])
    session.add(role)
    for _ in range(count):
        user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@plan.test", hashed_password="x", full_name="M")
        session.add(user)
        await session.flush()
        session.add(OrgMembership(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role_id=role.id))
    await session.commit()


async def test_starter_stops_at_5_users_and_team_at_15(session, stripe):
    org = await _org(session)
    await _on_plan(session, stripe, org, "solo", [(SETTINGS.stripe_extra_user_price_id, 4)])
    with pytest.raises(ValidationFailedError) as exc:
        await plan_billing.add_users(session, SETTINGS, org.id, 1, 1500)
    assert exc.value.code == "plan_user_limit" and "Team" in str(exc.value)

    other = await _org(session)
    await _on_plan(session, stripe, other, "business")
    await _members(session, other, 16)
    with pytest.raises(ValidationFailedError) as exc:
        await plan_billing.change_plan(session, SETTINGS, other.id, "team", None)
    assert exc.value.code == "plan_user_limit"
    assert plan_billing.user_limit_error(plan_billing.PLANS["business"], 500) is None


async def test_paid_checkout_puts_the_workspace_on_the_plan_and_numbers_are_plan_billed(
    session, stripe, monkeypatch
):
    org = await _org(session)
    purchase = await number_purchases.create(
        session, SETTINGS, org.id, ["+12125550101", "+12125550102"], plan_code="solo"
    )
    remote = stripe.add_subscription(
        org.id, SETTINGS.stripe_plan_solo_price_id, [(SETTINGS.stripe_extra_number_price_id, 1)]
    )
    stripe.checkout.Session.retrieve = lambda _id: {
        "status": "complete",
        "payment_status": "paid",
        "subscription": remote["id"],
        "metadata": {"purchase_id": str(purchase.id)},
    }
    carrier = SimpleNamespace(
        name="telnyx",
        order_number=AsyncMock(
            side_effect=lambda e164: OrderResult(e164=e164, provider_ref="o", status="active")
        ),
    )
    monkeypatch.setattr("app.providers.numbers.as_provider", lambda obj: obj)
    request, registry = _request(carrier)
    monkeypatch.setattr(
        "app.providers.registry_org.prime_org_registry", AsyncMock(return_value=registry)
    )

    result = await number_purchases.fulfill(session, request, purchase)

    assert result.state == "complete", result.detail
    ent = await plan_billing.entitlement(session, org.id)
    assert (ent.spec.code, ent.users, ent.numbers, ent.monthly_cents) == ("solo", 1, 2, 2000)
    await session.refresh(org)
    assert org.plan_code == "solo" and org.number_subscription_required is True
    rows = (await session.execute(sa.select(OrgNumber))).scalars().all()
    assert {r.provisioning["billing"] for r in rows} == {"workspace_plan"}
    assert (await seats.usage(session, org.id)).limit == 1


async def test_a_checkout_that_does_not_match_the_cart_is_refused(session, stripe, monkeypatch):
    org = await _org(session)
    purchase = await number_purchases.create(
        session, SETTINGS, org.id, ["+12125550101"], plan_code="team"
    )
    remote = stripe.add_subscription(org.id, SETTINGS.stripe_plan_solo_price_id)  # wrong plan
    stripe.checkout.Session.retrieve = lambda _id: {
        "status": "complete",
        "payment_status": "paid",
        "subscription": remote["id"],
        "metadata": {"purchase_id": str(purchase.id)},
    }
    request, _ = _request(SimpleNamespace(name="telnyx"))
    with pytest.raises(ValidationFailedError):
        await number_purchases.fulfill(session, request, purchase)
    assert await plan_billing.entitlement(session, org.id) is None


# ------------------------------------------------------------------------ users
async def test_users_come_from_the_plan_and_add_ons_are_charged_only_when_confirmed(
    session, stripe
):
    org = await _org(session)
    sid = await _on_plan(session, stripe, org, "team")
    assert (await seats.usage(session, org.id)).limit == 3

    with pytest.raises(PriceConfirmationRequiredError) as exc:
        await plan_billing.add_users(session, SETTINGS, org.id, 1, None)
    assert exc.value.quote["monthly_increase_cents"] == 1500
    assert stripe.modify_calls == []

    await plan_billing.add_users(session, SETTINGS, org.id, 1, 1500)
    call = stripe.modify_calls[-1]
    assert call["proration_behavior"] == "always_invoice"
    assert call["payment_behavior"] == "error_if_incomplete"
    assert stripe.monthly_cents(sid) == 4500 + 1500
    assert (await seats.usage(session, org.id)).limit == 4


async def test_a_declined_card_changes_nothing(session, stripe):
    org = await _org(session)
    sid = await _on_plan(session, stripe, org, "solo")
    stripe.decline = True
    with pytest.raises(ValidationFailedError) as exc:
        await plan_billing.add_users(session, SETTINGS, org.id, 1, 1500)
    assert exc.value.code == "payment_failed"
    assert stripe.monthly_cents(sid) == 1500
    assert (await plan_billing.entitlement(session, org.id)).users == 1


# ------------------------------------------------------------------------ numbers
async def test_numbers_are_free_inside_the_plan_and_5_dollars_beyond(session, stripe):
    org = await _org(session)
    sid = await _on_plan(session, stripe, org, "team")
    await _hold_numbers(session, org, 2)

    # One number still included: no charge, no confirmation, no Stripe change.
    ent = await plan_billing.reserve_numbers(session, SETTINGS, org.id, 1, None)
    assert ent.numbers == 3 and stripe.modify_calls == []

    # Three more with one free slot: two paid add-ons, $10/month, once confirmed.
    with pytest.raises(PriceConfirmationRequiredError) as exc:
        await plan_billing.reserve_numbers(session, SETTINGS, org.id, 3, 500)
    assert exc.value.quote == {
        "monthly_increase_cents": 1000,
        "interval": "month",
        "included_free": 1,
        "paid_numbers": 2,
    }
    ent = await plan_billing.reserve_numbers(session, SETTINGS, org.id, 3, 1000)
    assert ent.numbers == 5 and stripe.monthly_cents(sid) == 4500 + 1000


async def test_releasing_an_add_on_number_stops_billing_it(session, stripe):
    org = await _org(session)
    sid = await _on_plan(session, stripe, org, "solo", [(SETTINGS.stripe_extra_number_price_id, 2)])
    await _hold_numbers(session, org, 2)  # 3 paid for, 2 held: one add-on is spare
    await plan_billing.trim_unused(session, SETTINGS, org.id)
    assert stripe.modify_calls[-1]["proration_behavior"] == "none"
    assert stripe.monthly_cents(sid) == 1500 + 500
    assert (await plan_billing.entitlement(session, org.id)).numbers == 2


# ------------------------------------------------------------------------ plan changes
async def test_moving_up_to_team_absorbs_add_ons_and_needs_the_new_total_confirmed(session, stripe):
    org = await _org(session)
    # Solo + 2 users + 2 numbers = $55: exactly the case Team ($45) exists for.
    sid = await _on_plan(
        session,
        stripe,
        org,
        "solo",
        [(SETTINGS.stripe_extra_user_price_id, 2), (SETTINGS.stripe_extra_number_price_id, 2)],
    )
    assert stripe.monthly_cents(sid) == 5500
    await _hold_numbers(session, org, 3)
    from app.models import OrgMembership, Role, User

    role = Role(id=uuid.uuid4(), org_id=org.id, name="agent", permissions=[])
    session.add(role)
    for i in range(3):
        user = User(id=uuid.uuid4(), email=f"p{i}@plan.test", hashed_password="x", full_name="P")
        session.add(user)
        await session.flush()
        session.add(OrgMembership(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role_id=role.id))
    await session.commit()

    summary = await plan_billing.summary(session, SETTINGS, org.id)
    team = next(p for p in summary["catalog"] if p["code"] == "team")
    assert team["monthly_total_cents_if_switched"] == 4500

    with pytest.raises(PriceConfirmationRequiredError):
        await plan_billing.change_plan(session, SETTINGS, org.id, "team", 5500)
    ent = await plan_billing.change_plan(session, SETTINGS, org.id, "team", 4500)
    assert (ent.spec.code, ent.extra_users, ent.extra_numbers) == ("team", 0, 0)
    assert stripe.monthly_cents(sid) == 4500



async def test_business_add_on_users_are_12_dollars_and_move_to_that_price_on_upgrade(
    session, stripe
):
    org = await _org(session)
    # Team + 8 add-on users = 11 people at $45 + 8 x $15 = $165; Business covers 10 of them.
    sid = await _on_plan(session, stripe, org, "team", [(SETTINGS.stripe_extra_user_price_id, 8)])
    assert stripe.monthly_cents(sid) == 16500
    from app.models import OrgMembership, Role, User

    role = Role(id=uuid.uuid4(), org_id=org.id, name="agent", permissions=[])
    session.add(role)
    for i in range(11):
        user = User(id=uuid.uuid4(), email=f"b{i}@plan.test", hashed_password="x", full_name="B")
        session.add(user)
        await session.flush()
        session.add(OrgMembership(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role_id=role.id))
    await session.commit()

    ent = await plan_billing.change_plan(session, SETTINGS, org.id, "business", 13000 + 1200)
    assert (ent.spec.code, ent.extra_users) == ("business", 1)
    users_item = [
        i for i in stripe.subs[sid]["items"]["data"]
        if i["price"]["id"] == SETTINGS.stripe_business_extra_user_price_id
    ]
    assert [i["quantity"] for i in users_item] == [1]
    assert stripe.monthly_cents(sid) == 14200

    with pytest.raises(PriceConfirmationRequiredError) as exc:
        await plan_billing.add_users(session, SETTINGS, org.id, 1, None)
    assert exc.value.quote["monthly_increase_cents"] == 1200
    await plan_billing.add_users(session, SETTINGS, org.id, 1, 1200)
    assert stripe.monthly_cents(sid) == 15400
    summary = await plan_billing.summary(session, SETTINGS, org.id)
    assert summary["extra_user_cents"] == 1200


# ------------------------------------------------------------------------ minutes
async def test_team_shares_200_minutes_that_admit_calls_without_credit(session, stripe):
    from app.services import plans, telephony_billing

    org = await _org(session)
    await _on_plan(session, stripe, org, "team")
    assert await plans.remaining(session, org.id, "voice_minutes") == 200
    await plans.ensure_period(session, org.id)
    await plan_billing.add_users(session, SETTINGS, org.id, 1, 1500)
    assert await plans.remaining(session, org.id, "voice_minutes") == 200  # a fixed pool

    # No prepaid credit at all: the plan's minutes still let a call in.
    org.telephony_prepaid = True
    await session.commit()
    assert await telephony_billing.inbound_call_allowed(session, org.id, "telnyx") is True


async def test_starter_is_pay_as_you_go_and_business_shares_1000_minutes(session, stripe):
    from app.services import plans

    starter = await _org(session)
    await _on_plan(session, stripe, starter, "solo")
    assert await plans.remaining(session, starter.id, "voice_minutes") == 0
    business = await _org(session)
    await _on_plan(session, stripe, business, "business")
    assert await plans.remaining(session, business.id, "voice_minutes") == 1000


async def test_a_workspace_without_a_plan_gets_no_minutes(session, stripe):
    from app.services import plans, telephony_billing

    org = await _org(session)
    org.telephony_prepaid = True
    await session.commit()
    assert await plans.remaining(session, org.id, "voice_minutes") == 0
    assert await telephony_billing.inbound_call_allowed(session, org.id, "telnyx") is False


# ------------------------------------------------------------------------ Stripe events
async def test_a_cancelled_subscription_removes_the_plan_and_switches_numbers_off(session, stripe):
    from app.models import NumberPurchase

    org = await _org(session)
    sid = await _on_plan(session, stripe, org, "team")
    set_org_context(session, org.id)
    purchase = NumberPurchase(
        id=uuid.uuid4(),
        org_id=org.id,
        numbers=[],
        state="complete",
        plan_code="team",
        subscription_id=sid,
        subscription_status="active",
    )
    session.add(purchase)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12125550199",
            carrier="telnyx",
            status="active",
            is_active=True,
            provisioning={"billing": "workspace_plan", "number_purchase_id": str(purchase.id)},
        )
    )
    await session.commit()

    stripe.subs[sid]["status"] = "canceled"
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SETTINGS)))
    event = {
        "type": "customer.subscription.deleted",
        "data": {
            "object": {"id": sid, "metadata": {"kind": "workspace_plan", "org_id": str(org.id)}}
        },
    }
    assert await plan_billing.handle_event(session, request, event) is True

    assert await plan_billing.entitlement(session, org.id) is None
    await session.refresh(org)
    assert org.plan_code is None
    number = (await session.execute(sa.select(OrgNumber))).scalar_one()
    assert number.is_active is False
    row = (await session.execute(sa.select(Subscription))).scalar_one()
    assert row.status == "canceled"


async def test_the_catalogue_matches_the_prices_sold(session, stripe):
    from app.models import Plan

    await plan_billing.ensure_catalog(session, SETTINGS)
    await session.commit()
    rows = {p.code: p for p in (await session.execute(sa.select(Plan))).scalars()}
    assert {
        code: (rows[code].monthly_price_micros, rows[code].included["seats"])
        for code in plan_billing.PLANS
    } == {
        "solo": (15_000_000, 1),
        "team": (45_000_000, 3),
        "business": (130_000_000, 10),
    }


async def test_plan_numbers_are_never_also_charged_rental_from_credits(session, stripe):
    """Stripe bills plan numbers; the monthly credit-rental sweep must leave them alone,
    while a credit-billed number in the same workspace is still charged."""
    from datetime import date, datetime, timedelta, timezone

    from app.models import CreditLedgerEntry
    from app.services import telephony_billing

    org = await _org(session)
    await _on_plan(session, stripe, org, "solo")
    org.telephony_prepaid = True
    org.telephony_prepaid_since = datetime.now(timezone.utc) - timedelta(days=90)
    set_org_context(session, org.id)
    plan_number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164="+12125550150",
        carrier="telnyx",
        status="active",
        is_active=True,
        provisioning={"billing": "workspace_plan"},
    )
    credit_number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164="+12125550151",
        carrier="telnyx",
        status="active",
        is_active=True,
        provisioning={},
    )
    session.add_all([plan_number, credit_number])
    await session.commit()

    await telephony_billing.renew_number_rentals(session, today=date.today())

    set_org_context(session, org.id)
    references = [
        r for r in (await session.execute(sa.select(CreditLedgerEntry.reference))).scalars() if r
    ]
    assert any(str(credit_number.id) in r for r in references), references
    assert not any(str(plan_number.id) in r for r in references), references
