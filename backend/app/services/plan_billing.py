"""Workspace plans: Solo, Team and Business, plus paid add-on users and phone numbers.

    Solo      $15/month   1 user  + 1 number
    Team      $45/month   3 users + 3 numbers
    Business  $75/month   5 users + 5 numbers
    add-on user     $15/month (comes without a number)
    add-on number    $5/month

Every user brings 200 call minutes a month to a pool the whole workspace shares (inbound and
outbound, no rollover). Beyond the pool, and for all messaging, the prepaid credit balance
pays (services/telephony_billing.py).

ONE Stripe subscription per workspace carries it all: the plan price (quantity 1) and, when
bought, an add-on user item and an add-on number item whose quantities are the add-ons.
Stripe is the source of truth; ``subscriptions`` mirrors it on every event
(``upsert_from_stripe``), and what the workspace may use is derived from that mirror:

    users   = plan users   + extra_users      (seats.py enforces it)
    numbers = plan numbers + extra_numbers    (number_purchases.py enforces it)
    minutes = 200 x users                      (plans.py seeds the monthly allowance)

Anything that costs more money needs the caller to echo the exact monthly increase it was
shown (``accept_cents``). A stale screen or a double click therefore cannot charge a card an
amount the person never saw; they get the fresh quote back instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import (
    FeatureUnavailableError,
    PriceConfirmationRequiredError,
    ValidationFailedError,
)
from app.models import Org, OrgNumber, Plan
from app.models.subscriptions import (
    ENTITLED_SUBSCRIPTION_STATUSES,
    Subscription,
    is_entitled,
)
from app.services import stripe_client

log = structlog.get_logger("plan_billing")

_UNSCOPED = {ALLOW_UNSCOPED_KEY: True}

EXTRA_USER_CENTS = 1500  # Solo and Team; Business sets its own on PlanSpec
EXTRA_NUMBER_CENTS = 500
#: Stripe metadata kind on the subscription itself (the checkout session says number_purchase).
SUBSCRIPTION_KIND = "workspace_plan"


@dataclass(frozen=True)
class PlanSpec:
    code: str
    name: str
    users: int
    numbers: int
    price_cents: int
    setting: str
    extra_user_cents: int = EXTRA_USER_CENTS
    extra_user_setting: str = "stripe_extra_user_price_id"
    #: Call minutes a month for the whole workspace (a fixed pool; add-on users add none).
    minutes: int = 0


PLANS: dict[str, PlanSpec] = {
    "solo": PlanSpec("solo", "Starter", 1, 1, 1500, "stripe_plan_solo_price_id"),
    "team": PlanSpec("team", "Team", 3, 3, 4500, "stripe_plan_team_price_id", minutes=200),
    "business": PlanSpec(
        "business", "Business", 10, 10, 13000, "stripe_plan_business_price_id",
        extra_user_cents=1200, extra_user_setting="stripe_business_extra_user_price_id",
        minutes=1000,
    ),
}


def plan_price_id(settings: Settings, code: str) -> str:
    return getattr(settings, PLANS[code].setting)


def monthly_cents(spec: PlanSpec, extra_users: int, extra_numbers: int) -> int:
    return spec.price_cents + extra_users * spec.extra_user_cents + extra_numbers * EXTRA_NUMBER_CENTS


def extra_user_price_id(settings: Settings, code: str) -> str:
    """The add-on user price for a plan: Business has its own, the others share one."""
    return getattr(settings, PLANS[code].extra_user_setting)


# ------------------------------------------------------------------------------------
# Catalogue
# ------------------------------------------------------------------------------------
async def ensure_catalog(session: AsyncSession, settings: Settings) -> None:
    """Write the three plans into ``plans`` exactly as this module defines them, and retire
    every other catalogue row. Code is the single source of truth for what a plan includes."""
    for spec in PLANS.values():
        row = await session.get(Plan, spec.code)
        values = {
            "name": spec.name,
            "monthly_price_micros": spec.price_cents * 10_000,
            "included": {
                "seats": spec.users,
                "numbers": spec.numbers,
                "voice_minutes": spec.minutes,
                "sms_segments": 0,
            },
            "is_active": True,
            "stripe_price_id": plan_price_id(settings, spec.code),
        }
        if row is None:
            session.add(Plan(code=spec.code, overage_rates={}, **values))
        else:
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
    await session.execute(
        sa.update(Plan)
        .where(Plan.code.not_in(tuple(PLANS)), Plan.is_active.is_(True))
        .values(is_active=False)
    )
    await session.flush()


# ------------------------------------------------------------------------------------
# Entitlement
# ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Entitlement:
    subscription: Subscription
    spec: PlanSpec
    extra_users: int
    extra_numbers: int

    @property
    def users(self) -> int:
        return self.spec.users + self.extra_users

    @property
    def numbers(self) -> int:
        return self.spec.numbers + self.extra_numbers

    @property
    def minutes(self) -> int:
        return self.spec.minutes

    @property
    def monthly_cents(self) -> int:
        return monthly_cents(self.spec, self.extra_users, self.extra_numbers)


async def entitlement(session: AsyncSession, org_id: uuid.UUID) -> Entitlement | None:
    """The workspace's live plan, or None when it has none (or it lapsed)."""
    row = (
        await session.execute(
            sa.select(Subscription)
            .where(
                Subscription.org_id == org_id,
                Subscription.plan_code.in_(tuple(PLANS)),
                Subscription.status.in_(ENTITLED_SUBSCRIPTION_STATUSES),
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .execution_options(**_UNSCOPED)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return Entitlement(row, PLANS[row.plan_code], int(row.extra_users), int(row.extra_numbers))


async def numbers_held(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Numbers the workspace holds (anything not released or failed)."""
    return int(
        (
            await session.execute(
                sa.select(sa.func.count(OrgNumber.id))
                .where(OrgNumber.org_id == org_id, OrgNumber.status.not_in(("released", "failed")))
                .execution_options(**_UNSCOPED)
            )
        ).scalar_one()
    )


async def users_taken(session: AsyncSession, org_id: uuid.UUID) -> int:
    from app.services import seats

    usage = await seats.usage(session, org_id)
    return usage.members + usage.pending_invites


async def is_plan_subscription(session: AsyncSession, stripe_subscription_id: str | None) -> bool:
    if not stripe_subscription_id:
        return False
    return (
        await session.execute(
            sa.select(Subscription.id)
            .where(
                Subscription.stripe_subscription_id == stripe_subscription_id,
                Subscription.plan_code.in_(tuple(PLANS)),
            )
            .execution_options(**_UNSCOPED)
        )
    ).first() is not None


# ------------------------------------------------------------------------------------
# Stripe prices and subscription items
# ------------------------------------------------------------------------------------
async def validate_price(stripe, price_id: str, cents: int) -> None:
    """Refuse to sell on a price that is not exactly what this module promises."""
    price = await stripe_client._run_sync(stripe.Price.retrieve, price_id)
    recurring = price.get("recurring") or {}
    if (
        price.get("unit_amount") != cents
        or price.get("currency") != "usd"
        or recurring.get("interval") != "month"
        or recurring.get("interval_count") != 1
        or recurring.get("usage_type", "licensed") != "licensed"
        or price.get("transform_quantity")
        or not price.get("active")
    ):
        log.error("plan_price_mismatch", price_id=price_id, expected_cents=cents)
        raise FeatureUnavailableError("Plan pricing needs administrator attention.")


@dataclass
class ParsedItems:
    plan_code: str
    plan_item_id: str
    extra_users: int
    extra_users_item_id: str | None
    extra_numbers: int
    extra_numbers_item_id: str | None
    extra_users_price_id: str | None = None


def parse_items(settings: Settings, subscription: dict) -> ParsedItems:
    """Read plan and add-on quantities off a Stripe subscription. Anything unexpected on it
    (an unknown price, two plans, a plan quantity other than 1) is refused, not guessed."""
    by_price = {plan_price_id(settings, code): code for code in PLANS}
    plan_code = plan_item = users_item = numbers_item = users_price = None
    extra_users = extra_numbers = 0
    user_prices = {extra_user_price_id(settings, code) for code in PLANS} - {""}
    for item in (subscription.get("items") or {}).get("data", []):
        price_id = (item.get("price") or {}).get("id")
        quantity = int(item.get("quantity") or 0)
        if price_id in by_price:
            if plan_code is not None or quantity != 1:
                raise ValidationFailedError("Subscription does not match a Ringlite plan")
            plan_code, plan_item = by_price[price_id], item.get("id")
        elif price_id in user_prices:
            extra_users, users_item, users_price = quantity, item.get("id"), price_id
        elif price_id == settings.stripe_extra_number_price_id:
            extra_numbers, numbers_item = quantity, item.get("id")
        else:
            raise ValidationFailedError("Subscription has an item that is not a Ringlite plan")
    if plan_code is None:
        raise ValidationFailedError("Subscription does not include a Ringlite plan")
    return ParsedItems(
        plan_code, plan_item, extra_users, users_item, extra_numbers, numbers_item, users_price
    )


def checkout_line_items(settings: Settings, code: str, numbers: int) -> list[dict]:
    items = [{"price": plan_price_id(settings, code), "quantity": 1}]
    extra = max(numbers - PLANS[code].numbers, 0)
    if extra:
        items.append({"price": settings.stripe_extra_number_price_id, "quantity": extra})
    return items


async def validate_checkout_prices(settings: Settings, stripe, code: str, numbers: int) -> None:
    await validate_price(stripe, plan_price_id(settings, code), PLANS[code].price_cents)
    if numbers > PLANS[code].numbers:
        await validate_price(stripe, settings.stripe_extra_number_price_id, EXTRA_NUMBER_CENTS)


def verify_checkout_subscription(
    settings: Settings, subscription: dict, code: str, numbers: int
) -> ParsedItems:
    """The paid subscription is exactly the plan and add-on numbers the cart asked for."""
    parsed = parse_items(settings, subscription)
    if (
        parsed.plan_code != code
        or parsed.extra_users != 0
        or parsed.extra_numbers != max(numbers - PLANS[code].numbers, 0)
    ):
        raise ValidationFailedError("Subscription does not match the selected plan and numbers")
    return parsed


# ------------------------------------------------------------------------------------
# Mirroring Stripe
# ------------------------------------------------------------------------------------
def _period_end(subscription: dict) -> datetime | None:
    items = (subscription.get("items") or {}).get("data", [])
    raw = subscription.get("current_period_end") or (
        items[0].get("current_period_end") if items else None
    )
    return datetime.fromtimestamp(int(raw), tz=timezone.utc) if raw else None


async def upsert_from_stripe(
    session: AsyncSession, settings: Settings, subscription: dict, org_id: uuid.UUID
) -> Subscription:
    """Make the local mirror, the org's plan link and every number's billing state match
    this Stripe subscription. Does not commit."""
    from app.models import NumberPurchase

    parsed = parse_items(settings, subscription)
    set_org_context(session, org_id)
    await ensure_catalog(session, settings)
    row = (
        await session.execute(
            sa.select(Subscription)
            .where(Subscription.stripe_subscription_id == subscription["id"])
            .execution_options(**_UNSCOPED)
        )
    ).scalar_one_or_none()
    if row is None:
        row = Subscription(org_id=org_id, stripe_subscription_id=subscription["id"])
        session.add(row)
    elif row.org_id != org_id:
        raise ValidationFailedError("Subscription belongs to another workspace")
    row.plan_code = parsed.plan_code
    row.status = subscription.get("status") or "incomplete"
    row.stripe_customer_id = subscription.get("customer") or row.stripe_customer_id
    row.extra_users = parsed.extra_users
    row.extra_numbers = parsed.extra_numbers
    row.current_period_end = _period_end(subscription)
    row.cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))

    org = await session.get(Org, org_id)
    entitled = is_entitled(row.status)
    if entitled:
        org.plan_code = parsed.plan_code
        org.plan_started_at = org.plan_started_at or datetime.now(timezone.utc)
        org.number_subscription_required = True
    elif org.plan_code in PLANS:
        org.plan_code = None

    purchases = (
        await session.execute(
            sa.select(NumberPurchase).where(
                NumberPurchase.org_id == org_id,
                NumberPurchase.subscription_id == subscription["id"],
            )
        )
    ).scalars()
    purchase_ids = set()
    for purchase in purchases:
        purchase.subscription_status = row.status
        purchase_ids.add(str(purchase.id))
    for number in (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.org_id == org_id))
    ).scalars():
        prov = number.provisioning or {}
        if prov.get("number_purchase_id") in purchase_ids and number.status == "active":
            number.is_active = entitled
    await session.flush()
    if entitled:
        await refresh_voice_allowance(session, org_id)
    return row


async def handle_event(session: AsyncSession, request, event: dict) -> bool:
    """customer.subscription.* for a workspace plan. Re-reads Stripe rather than trusting
    the event body, which can arrive out of order."""
    if not event.get("type", "").startswith("customer.subscription."):
        return False
    obj = event.get("data", {}).get("object", {})
    metadata = obj.get("metadata") or {}
    if metadata.get("kind") != SUBSCRIPTION_KIND:
        return False
    try:
        org_id = uuid.UUID(metadata["org_id"])
    except (KeyError, ValueError):
        raise ValidationFailedError("Invalid plan subscription metadata") from None
    settings = request.app.state.settings
    stripe = stripe_client._stripe(settings)
    latest = await stripe_client._run_sync(stripe.Subscription.retrieve, obj["id"])
    await upsert_from_stripe(session, settings, latest, org_id)
    await session.commit()
    return True


async def refresh_voice_allowance(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Raise this period's pooled minutes to the plan's pool after a mid-period upgrade.
    Never lowers them: minutes already paid for last until the period ends."""
    from app.models.plans import PlanAllowance
    from app.services import plans

    ent = await entitlement(session, org_id)
    if ent is None:
        return
    rows = await plans.ensure_period(session, org_id)
    row = rows.get("voice_minutes")
    if row is not None and row.included_units < ent.minutes:
        await session.execute(
            sa.update(PlanAllowance)
            .where(PlanAllowance.id == row.id, PlanAllowance.included_units < ent.minutes)
            .values(included_units=ent.minutes)
            .execution_options(synchronize_session=False)
        )
        await session.flush()


# ------------------------------------------------------------------------------------
# Changing what the workspace pays for
# ------------------------------------------------------------------------------------
def _require_accepted(accept_cents: int | None, increase_cents: int, detail: dict) -> None:
    if accept_cents != increase_cents:
        raise PriceConfirmationRequiredError({"monthly_increase_cents": increase_cents, **detail})


def _stripe_failure(exc: Exception) -> ValidationFailedError:
    message = getattr(exc, "user_message", None) or "Your card was declined."
    return ValidationFailedError(
        f"Payment failed: {message} Update your card in Billing and try again.",
        code="payment_failed",
    )


async def _apply(
    session: AsyncSession,
    settings: Settings,
    ent: Entitlement,
    *,
    plan_code: str | None = None,
    extra_users: int | None = None,
    extra_numbers: int | None = None,
    charge: bool,
    key: str,
) -> Entitlement:
    """Set the subscription to these quantities. ``charge`` bills the prorated difference
    now and fails without changing anything if the card is declined."""
    stripe = stripe_client._stripe(settings)
    remote = await stripe_client._run_sync(
        stripe.Subscription.retrieve, ent.subscription.stripe_subscription_id
    )
    parsed = parse_items(settings, remote)
    items: list[dict] = []
    if plan_code and plan_code != parsed.plan_code:
        await validate_price(
            stripe, plan_price_id(settings, plan_code), PLANS[plan_code].price_cents
        )
        items.append({"id": parsed.plan_item_id, "price": plan_price_id(settings, plan_code)})
    target = plan_code or parsed.plan_code
    user_price = extra_user_price_id(settings, target)
    if parsed.extra_users_item_id and parsed.extra_users_price_id != user_price:
        # A plan switch moves existing add-on users to the new plan's user price.
        await validate_price(stripe, user_price, PLANS[target].extra_user_cents)
        keep = parsed.extra_users if extra_users is None else extra_users
        if keep > 0:
            items.append({"id": parsed.extra_users_item_id, "price": user_price, "quantity": keep})
        else:
            items.append({"id": parsed.extra_users_item_id, "deleted": True})
        extra_users = None  # handled
    for wanted, current, item_id, price_id, cents in (
        (
            extra_users,
            parsed.extra_users,
            parsed.extra_users_item_id,
            user_price,
            PLANS[target].extra_user_cents,
        ),
        (
            extra_numbers,
            parsed.extra_numbers,
            parsed.extra_numbers_item_id,
            settings.stripe_extra_number_price_id,
            EXTRA_NUMBER_CENTS,
        ),
    ):
        if wanted is None or wanted == current:
            continue
        if item_id and wanted > 0:
            items.append({"id": item_id, "quantity": wanted})
        elif item_id:
            items.append({"id": item_id, "deleted": True})
        else:
            await validate_price(stripe, price_id, cents)
            items.append({"price": price_id, "quantity": wanted})
    if not items:
        return ent
    kwargs: dict = {"items": items, "idempotency_key": key}
    if charge:
        kwargs.update(proration_behavior="always_invoice", payment_behavior="error_if_incomplete")
    else:
        kwargs.update(proration_behavior="none")
    try:
        updated = await stripe_client._run_sync(
            stripe.Subscription.modify, ent.subscription.stripe_subscription_id, **kwargs
        )
    except Exception as exc:  # the SDK's CardError family; see stripe_client.charge_off_session
        if type(exc).__name__ in ("CardError", "InvalidRequestError") or getattr(
            exc, "user_message", None
        ):
            raise _stripe_failure(exc) from exc
        raise
    await upsert_from_stripe(session, settings, updated, ent.subscription.org_id)
    await session.commit()
    fresh = await entitlement(session, ent.subscription.org_id)
    assert fresh is not None
    return fresh


async def require_entitlement(session: AsyncSession, org_id: uuid.UUID) -> Entitlement:
    ent = await entitlement(session, org_id)
    if ent is None:
        raise ValidationFailedError(
            "Choose a plan first: it comes with your first phone numbers and users.",
            code="plan_required",
        )
    return ent


async def add_users(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    count: int,
    accept_cents: int | None,
) -> Entitlement:
    """Buy ``count`` more users at the plan's add-on price each, charged now (prorated)."""
    if not 1 <= count <= 50:
        raise ValidationFailedError("Add between 1 and 50 users at a time")
    ent = await require_entitlement(session, org_id)
    increase = count * ent.spec.extra_user_cents
    _require_accepted(accept_cents, increase, {"users": ent.users + count})
    return await _apply(
        session,
        settings,
        ent,
        extra_users=ent.extra_users + count,
        charge=True,
        key=f"add-users-{ent.subscription.id}-{ent.extra_users}-{count}",
    )


async def reserve_numbers(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    count: int,
    accept_cents: int | None,
) -> Entitlement:
    """Make room for ``count`` new numbers: free inside the plan, $5/month each beyond it."""
    ent = await require_entitlement(session, org_id)
    free = max(ent.numbers - await numbers_held(session, org_id), 0)
    need = max(count - free, 0)
    if need == 0:
        return ent
    _require_accepted(
        accept_cents, need * EXTRA_NUMBER_CENTS, {"included_free": free, "paid_numbers": need}
    )
    return await _apply(
        session,
        settings,
        ent,
        extra_numbers=ent.extra_numbers + need,
        charge=True,
        key=f"add-numbers-{ent.subscription.id}-{ent.extra_numbers}-{need}",
    )


async def change_plan(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    code: str,
    accept_cents: int | None,
) -> Entitlement:
    """Move to another plan, keeping every user and number in use. Add-ons shrink to what
    the new plan does not already include. ``accept_cents`` is the NEW monthly total."""
    if code not in PLANS:
        raise ValidationFailedError("Unknown plan")
    ent = await require_entitlement(session, org_id)
    if code == ent.spec.code:
        raise ValidationFailedError("You are already on this plan")
    spec = PLANS[code]
    extra_users = max(await users_taken(session, org_id) - spec.users, 0)
    extra_numbers = max(await numbers_held(session, org_id) - spec.numbers, 0)
    new_total = monthly_cents(spec, extra_users, extra_numbers)
    if accept_cents != new_total:
        raise PriceConfirmationRequiredError({"monthly_total_cents": new_total, "plan": code})
    return await _apply(
        session,
        settings,
        ent,
        plan_code=code,
        extra_users=extra_users,
        extra_numbers=extra_numbers,
        charge=True,
        key=f"change-plan-{ent.subscription.id}-{ent.spec.code}-{code}-{extra_users}-{extra_numbers}",
    )


async def trim_unused(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    *,
    users: bool = True,
    numbers: bool = True,
) -> Entitlement | None:
    """Stop paying for add-ons nobody is using (no refund for the current month).

    Called after a number is released or a person is removed, and from Billing."""
    ent = await entitlement(session, org_id)
    if ent is None:
        return None
    new_users = ent.extra_users
    new_numbers = ent.extra_numbers
    if users and ent.extra_users:
        spare = ent.users - await users_taken(session, org_id)
        new_users = max(ent.extra_users - max(spare, 0), 0)
    if numbers and ent.extra_numbers:
        spare = ent.numbers - await numbers_held(session, org_id)
        new_numbers = max(ent.extra_numbers - max(spare, 0), 0)
    if (new_users, new_numbers) == (ent.extra_users, ent.extra_numbers):
        return ent
    return await _apply(
        session,
        settings,
        ent,
        extra_users=new_users,
        extra_numbers=new_numbers,
        charge=False,
        key=f"trim-{ent.subscription.id}-{new_users}-{new_numbers}-{uuid.uuid4().hex[:8]}",
    )


async def summary(session: AsyncSession, settings: Settings, org_id: uuid.UUID) -> dict:
    """What Billing and the buying screens show: the plan, usage, the monthly total, and
    what every other plan would cost with the same people and numbers."""
    from app.services import plans

    ent = await entitlement(session, org_id)
    taken = await users_taken(session, org_id)
    held = await numbers_held(session, org_id)
    catalog = []
    for spec in PLANS.values():
        extra_u = max(taken - spec.users, 0)
        extra_n = max(held - spec.numbers, 0)
        catalog.append(
            {
                "code": spec.code,
                "name": spec.name,
                "users": spec.users,
                "numbers": spec.numbers,
                "price_cents": spec.price_cents,
                "extra_user_cents": spec.extra_user_cents,
                "minutes": spec.minutes,
                "monthly_total_cents_if_switched": monthly_cents(spec, extra_u, extra_n),
            }
        )
    out: dict = {
        "plan": None,
        "users": {"limit": None, "in_use": taken},
        "numbers": {"limit": None, "in_use": held},
        "extra_user_cents": EXTRA_USER_CENTS,
        "extra_number_cents": EXTRA_NUMBER_CENTS,
        "catalog": catalog,
    }
    if ent is None:
        return out
    voice = await plans.remaining(session, org_id, "voice_minutes")
    out.update(
        extra_user_cents=ent.spec.extra_user_cents,
        plan={
            "code": ent.spec.code,
            "name": ent.spec.name,
            "status": ent.subscription.status,
            "price_cents": ent.spec.price_cents,
            "monthly_total_cents": ent.monthly_cents,
            "renews_at": ent.subscription.current_period_end.isoformat()
            if ent.subscription.current_period_end
            else None,
            "cancel_at_period_end": ent.subscription.cancel_at_period_end,
        },
        users={
            "limit": ent.users,
            "in_use": taken,
            "included": ent.spec.users,
            "extra": ent.extra_users,
        },
        numbers={
            "limit": ent.numbers,
            "in_use": held,
            "included": ent.spec.numbers,
            "extra": ent.extra_numbers,
        },
        minutes={"included": ent.minutes, "remaining": voice},
    )
    return out
