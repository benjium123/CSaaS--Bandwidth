"""Card payments as the customer paid them (billing_payments): top-ups, auto-recharges and
message bundles, with list price, discount and Stripe fee - the admin console's revenue.

Money-owned. ``credits`` stays the only writer of credit_ledger and ``bundles`` of
bundle_ledger; this module records the payment and calls them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import BillingPayment, Org
from app.services import bundles

log = structlog.get_logger("payments")

BUNDLE_METADATA_KINDS: dict[str, str] = {"sms_bundle": "sms", "mms_bundle": "mms"}
#: Payments whose Stripe fee is looked up per sweeper pass.
FEE_BATCH = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _by_intent(session: AsyncSession, intent_id: str) -> BillingPayment | None:
    return (
        await session.execute(
            sa.select(BillingPayment)
            .where(BillingPayment.stripe_payment_intent_id == intent_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()


async def start_bundle_payment(
    session: AsyncSession, org_id: uuid.UUID, *, kind: str, qty: int
) -> tuple[BillingPayment, dict[str, int]]:
    """Price the purchase and create its pending row (before Stripe is called). Does not
    commit."""
    q = await bundles.quote(session, kind, qty)
    set_org_context(session, org_id)
    row = BillingPayment(
        id=uuid.uuid4(),
        org_id=org_id,
        kind=f"{kind}_bundle",
        state="pending",
        quantity=qty,
        list_micros=q["list"],
        paid_micros=q["paid"],
        discount_micros=q["discount"],
        units_credited=0,
        credited_micros=0,
        detail={"unit_paid_micros": q["unit_paid"], "units": q["units"]},
    )
    session.add(row)
    await session.flush()
    return row, q


async def handle_bundle_intent(session: AsyncSession, intent: dict) -> bool:
    """payment_intent.succeeded for a bundle: credit the units once, mark the row paid.
    Returns True when the intent was a bundle (handled or safely ignored). Commits."""
    metadata = intent.get("metadata") or {}
    kind = BUNDLE_METADATA_KINDS.get(str(metadata.get("kind") or ""))
    if kind is None:
        return False
    try:
        org_id = uuid.UUID(str(metadata.get("org_id")))
        qty = int(metadata.get("qty") or 0)
    except (TypeError, ValueError):
        log.warning("bundle_intent_unattributable", metadata=metadata)
        return True
    intent_id = str(intent.get("id") or "")
    amount_received = int(intent.get("amount_received") or 0)
    if not intent_id or qty <= 0 or amount_received <= 0:
        log.warning("bundle_intent_incomplete", intent_id=intent_id, qty=qty)
        return True
    if await session.get(Org, org_id) is None:
        log.warning("bundle_intent_unknown_org", org_id=str(org_id))
        return True

    set_org_context(session, org_id)
    row = None
    payment_id = metadata.get("payment_id")
    if payment_id:
        try:
            row = await session.get(BillingPayment, uuid.UUID(str(payment_id)))
        except ValueError:
            row = None
    if row is None:
        row = await _by_intent(session, intent_id)
    paid = amount_received * 10_000
    units = bundles.UNITS_PER_BUNDLE[kind] * qty
    if row is None:
        # Paid for a checkout we have no row for (row lost or created elsewhere): record it
        # from what Stripe says, list price from today's price list.
        list_each = await bundles.bundle_list_price(session, kind)
        row = BillingPayment(
            id=uuid.uuid4(),
            org_id=org_id,
            kind=f"{kind}_bundle",
            quantity=qty,
            list_micros=list_each * qty,
            discount_micros=max(list_each * qty - paid, 0),
        )
        session.add(row)
    await bundles.credit(
        session,
        org_id,
        kind,
        units,
        reference=f"pi:{intent_id}",
        note=f"{qty} x {kind.upper()} bundle",
    )
    row.state = "paid"
    row.stripe_payment_intent_id = intent_id
    row.paid_micros = paid
    row.units_credited = units
    row.paid_at = row.paid_at or _now()
    await session.commit()
    return True


async def record_topup_paid(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    intent_id: str,
    amount_micros: int,
    kind: str = "topup",
) -> None:
    """Record a paid top-up / auto-recharge once (idempotent on the intent id). Does not
    commit; never raises (the credit itself already happened)."""
    try:
        if await _by_intent(session, intent_id) is not None:
            return
        set_org_context(session, org_id)
        async with session.begin_nested():
            session.add(
                BillingPayment(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    kind=kind,
                    state="paid",
                    stripe_payment_intent_id=intent_id,
                    quantity=1,
                    list_micros=amount_micros,
                    paid_micros=amount_micros,
                    discount_micros=0,
                    credited_micros=amount_micros,
                    paid_at=_now(),
                )
            )
            await session.flush()
    except Exception:
        log.exception("payments.record_topup_failed", intent_id=intent_id)


async def fee_tick(session: AsyncSession, settings) -> int:  # noqa: ANN001
    """Fill in Stripe's fee for recent paid payments that do not have it yet."""
    rows = (
        await session.execute(
            sa.select(BillingPayment.id, BillingPayment.org_id, BillingPayment.stripe_payment_intent_id)
            .where(
                BillingPayment.state == "paid",
                BillingPayment.stripe_fee_micros.is_(None),
                BillingPayment.stripe_payment_intent_id.is_not(None),
                BillingPayment.stripe_payment_intent_id.like("pi_%"),
            )
            .order_by(BillingPayment.created_at.desc())
            .limit(FEE_BATCH)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    from app.services import stripe_client

    filled = 0
    for pid, org_id, intent_id in rows:
        try:
            fee = await stripe_client.payment_fee_micros(settings, intent_id)
            if fee is None:
                continue
            set_org_context(session, org_id)
            row = await session.get(BillingPayment, pid)
            if row is not None:
                row.stripe_fee_micros = fee
                await session.commit()
                filled += 1
        except Exception:
            await session.rollback()
            log.exception("payments.fee_lookup_failed", intent_id=intent_id)
    return filled

