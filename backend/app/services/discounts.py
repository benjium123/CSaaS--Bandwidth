"""Per-workspace discounts (admin operators set them in Ops -> Console).

One optional percentage per (workspace, category):

- ``usage``        per-unit prices charged from the balance: texts, calls, fax, AI usage.
- ``numbers``      number rental and setup charged from the balance (``number_*`` metrics),
                   and the extra-number items on a Stripe plan subscription.
- ``bundles``      SMS/MMS/call-minute bundles, applied after the 5+ volume discount.
- ``subscription`` the plan and extra users on the Stripe subscription.
- ``tendlc``       the Ringlite service fee in the 10DLC checkout (carrier fees pass through).

In-app prices apply the discount when they are computed (``telephony_billing.unit_price``,
``bundles.quote``), so what the customer is shown, what the credit gate checks and what is
charged all agree. Each usage charge records what the discount took off
(``credit_ledger.discount_micros``) so the console can show profit before and after
discounts. Stripe-billed items carry the same percentage as a Stripe coupon.

An expired discount (``ends_at`` in the past) is simply not applied; the row stays until an
operator removes or renews it, so the console can show that it ran out.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models.billing_v2 import DISCOUNT_CATEGORIES, OrgDiscount

CATEGORIES = DISCOUNT_CATEGORIES
MAX_BPS = 10_000
_MEMO_KEY = "org_discounts"


def category_for_metric(metric: str) -> str:
    """Which discount a per-unit price metric falls under."""
    return "numbers" if metric.startswith("number_") else "usage"


def apply(price_micros: int, bps: int) -> int:
    """``price_micros`` less ``bps`` basis points, rounded down (in the customer's favour).
    Integer math only."""
    if bps <= 0 or price_micros <= 0:
        return max(int(price_micros), 0)
    return int(price_micros) * (MAX_BPS - min(int(bps), MAX_BPS)) // MAX_BPS


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_active(row: OrgDiscount, now: datetime | None = None) -> bool:
    if row.ends_at is None:
        return True
    return _as_utc(row.ends_at) > (now or datetime.now(timezone.utc))


async def _rows(session: AsyncSession, org_id: uuid.UUID) -> list[OrgDiscount]:
    memo: dict = session.info.setdefault(_MEMO_KEY, {})
    if org_id in memo:
        return memo[org_id]
    set_org_context(session, org_id)
    rows = list(
        (
            await session.execute(sa.select(OrgDiscount).where(OrgDiscount.org_id == org_id))
        ).scalars()
    )
    memo[org_id] = rows
    return rows


def invalidate(session: AsyncSession, org_id: uuid.UUID) -> None:
    memo = session.info.get(_MEMO_KEY)
    if memo is not None:
        memo.pop(org_id, None)


async def active_bps(
    session: AsyncSession,
    org_id: uuid.UUID | None,
    category: str,
    *,
    now: datetime | None = None,
) -> int:
    """The discount in basis points the workspace has on ``category`` right now (0 = none)."""
    if org_id is None:
        return 0
    for row in await _rows(session, org_id):
        if row.category == category and is_active(row, now):
            return int(row.percent_bps)
    return 0


def to_dict(row: OrgDiscount, now: datetime | None = None) -> dict:
    return {
        "category": row.category,
        "percent_bps": int(row.percent_bps),
        "percent": int(row.percent_bps) / 100,
        "ends_at": _as_utc(row.ends_at).isoformat() if row.ends_at else None,
        "active": is_active(row, now),
        "note": row.note,
        "stripe_coupon_id": row.stripe_coupon_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def for_org(session: AsyncSession, org_id: uuid.UUID) -> list[dict]:
    order = {c: i for i, c in enumerate(CATEGORIES)}
    rows = sorted(await _rows(session, org_id), key=lambda r: order.get(r.category, 99))
    return [to_dict(r) for r in rows]


def _check(category: str) -> None:
    if category not in CATEGORIES:
        raise ValidationFailedError(f"Unknown discount category: {category}")


async def set_discount(
    session: AsyncSession,
    org_id: uuid.UUID,
    category: str,
    *,
    percent_bps: int,
    ends_at: datetime | None,
    note: str | None,
    actor_user_id: uuid.UUID | None,
) -> tuple[OrgDiscount, int | None]:
    """Create or change the discount on one category. Returns (row, previous bps or None).
    Does NOT commit."""
    _check(category)
    if not 0 < int(percent_bps) <= MAX_BPS:
        raise ValidationFailedError("A discount must be between 0.01% and 100%")
    if ends_at is not None and _as_utc(ends_at) <= datetime.now(timezone.utc):
        raise ValidationFailedError("The end date must be in the future")
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OrgDiscount).where(
                OrgDiscount.org_id == org_id, OrgDiscount.category == category
            )
        )
    ).scalar_one_or_none()
    previous = int(row.percent_bps) if row is not None else None
    if row is None:
        row = OrgDiscount(org_id=org_id, category=category)
        session.add(row)
    elif int(row.percent_bps) != int(percent_bps):
        # A different percentage needs a different Stripe coupon (coupons are immutable).
        row.stripe_coupon_id = None
    row.percent_bps = int(percent_bps)
    row.ends_at = ends_at
    row.note = (note or "").strip() or None
    row.updated_by = actor_user_id
    invalidate(session, org_id)
    return row, previous


async def remove(session: AsyncSession, org_id: uuid.UUID, category: str) -> OrgDiscount | None:
    """Delete the discount on one category. Returns the removed row (for audit/Stripe), or
    None if there was none. Does NOT commit."""
    _check(category)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OrgDiscount).where(
                OrgDiscount.org_id == org_id, OrgDiscount.category == category
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
    invalidate(session, org_id)
    return row


# ------------------------------------------------------------------------------------
# Stripe: the plan subscription carries the same percentages as coupons
# ------------------------------------------------------------------------------------
async def stripe_coupon(settings, row: OrgDiscount) -> str:  # noqa: ANN001
    """The Stripe coupon for this discount, created on first use. Coupons cannot change, so
    a new percentage gets a new coupon (set_discount clears the id). Does NOT commit."""
    if row.stripe_coupon_id:
        return row.stripe_coupon_id
    from app.services import stripe_client

    stripe = stripe_client._stripe(settings)
    coupon = await stripe_client._run_sync(
        stripe.Coupon.create,
        percent_off=int(row.percent_bps) / 100,
        duration="forever",
        name=f"Workspace {row.category} discount {int(row.percent_bps) / 100:g}%",
        metadata={"org_id": str(row.org_id), "category": row.category, "kind": "org_discount"},
        idempotency_key=f"org-discount-{row.id}-{int(row.percent_bps)}",
    )
    row.stripe_coupon_id = coupon["id"]
    return row.stripe_coupon_id


async def _active_row(
    session: AsyncSession, org_id: uuid.UUID, category: str
) -> OrgDiscount | None:
    for row in await _rows(session, org_id):
        if row.category == category and is_active(row):
            return row
    return None


async def checkout_discounts(session: AsyncSession, settings, org_id: uuid.UUID) -> list[dict]:  # noqa: ANN001
    """``discounts`` for a plan Checkout Session. Checkout takes one discount for the whole
    cart, so it carries the subscription percentage; ``sync_subscription`` right after
    fulfilment moves it onto the items (numbers get their own percentage from then on)."""
    row = await _active_row(session, org_id, "subscription")
    if row is None:
        return []
    return [{"coupon": await stripe_coupon(settings, row)}]


async def sync_subscription(session: AsyncSession, settings, org_id: uuid.UUID) -> bool:  # noqa: ANN001
    """Put the workspace's current percentages on its plan subscription, per item: the plan
    and extra users get the ``subscription`` coupon, extra numbers the ``numbers`` coupon; an
    expired or removed discount clears it. Applies from the next invoice (no proration).
    Returns False when there is no plan subscription. Does NOT commit (a new coupon id is
    stored on the discount row)."""
    from app.services import plan_billing, stripe_client

    ent = await plan_billing.entitlement(session, org_id)
    sub_id = getattr(getattr(ent, "subscription", None), "stripe_subscription_id", None)
    if not sub_id:
        return False
    coupons: dict[str, str | None] = {}
    for category in ("subscription", "numbers"):
        row = await _active_row(session, org_id, category)
        coupons[category] = await stripe_coupon(settings, row) if row else None
    number_prices = {
        pid
        for interval in plan_billing.INTERVALS
        if (pid := plan_billing.number_price_id(settings, interval))
    }
    stripe = stripe_client._stripe(settings)
    remote = await stripe_client._run_sync(stripe.Subscription.retrieve, sub_id)
    items = []
    for item in (remote.get("items") or {}).get("data", []):
        price_id = (item.get("price") or {}).get("id")
        coupon = coupons["numbers" if price_id in number_prices else "subscription"]
        items.append({"id": item["id"], "discounts": [{"coupon": coupon}] if coupon else ""})
    if not items:
        return False
    # The Checkout-level discount (if any) is cleared: item-level coupons replace it, so a
    # discount is never counted twice.
    await stripe_client._run_sync(
        stripe.Subscription.modify,
        sub_id,
        items=items,
        discounts="",
        proration_behavior="none",
    )
    return True


async def has_stripe_discount(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether the plan subscription needs a coupon sync: an active subscription/numbers
    discount, or an expired one whose coupon may still be on Stripe."""
    return any(
        row.category in ("subscription", "numbers") and (is_active(row) or row.stripe_coupon_id)
        for row in await _rows(session, org_id)
    )


async def expire_stripe_discounts(session: AsyncSession, settings) -> int:  # noqa: ANN001
    """Hourly: take ended subscription/numbers discounts off the Stripe subscription. The
    row stays (the console shows it as expired); its coupon id is dropped once Stripe no
    longer carries it. Returns how many workspaces were synced. Commits per workspace."""
    from app.db.base import ALLOW_UNSCOPED_KEY

    now = datetime.now(timezone.utc)
    rows = (
        (
            await session.execute(
                sa.select(OrgDiscount)
                .where(
                    OrgDiscount.category.in_(("subscription", "numbers")),
                    OrgDiscount.stripe_coupon_id.is_not(None),
                    OrgDiscount.ends_at.is_not(None),
                    OrgDiscount.ends_at <= now,
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    synced = 0
    for org_id in sorted({row.org_id for row in rows}, key=str):
        invalidate(session, org_id)
        await sync_subscription(session, settings, org_id)
        for row in rows:
            if row.org_id == org_id:
                row.stripe_coupon_id = None
        await session.commit()
        synced += 1
    return synced
