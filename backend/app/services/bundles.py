"""Message bundles: prepaid SMS/MMS units, spent before the $ balance.

The ONLY writer of bundle_ledger. Same discipline as services/credits.py: append-only, one
row per change carrying balance_after_units, idempotent on (org, kind, entry_type,
reference), serialised per org by the same advisory lock credits uses.

Pricing (defaults, editable in platform_prices):
- SMS bundle: 1,000 segments for $12. Buying VOLUME_MIN_QTY or more in ONE purchase takes
  VOLUME_DISCOUNT_BPS off the whole purchase ($9.60 each at 5+).
- MMS bundle: 100 MMS for $3 ($0.03 each; Telnyx worst case is $0.025). No volume discount.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import BundleLedgerEntry
from app.models.billing_v2 import BUNDLE_ENTRY_TYPES, BUNDLE_KINDS
from app.services import credits

#: Message units in one bundle, per kind.
UNITS_PER_BUNDLE: dict[str, int] = {"sms": 1_000, "mms": 100}
VOLUME_MIN_QTY = 5
VOLUME_DISCOUNT_BPS = 2_000
#: Which bundle kinds get the volume discount.
VOLUME_DISCOUNT_KINDS: frozenset[str] = frozenset({"sms"})
MAX_QTY = 500


def _check_kind(kind: str) -> None:
    if kind not in BUNDLE_KINDS:
        raise ValidationFailedError(f"Unknown bundle kind: {kind}")


async def bundle_list_price(session: AsyncSession, kind: str) -> int:
    """List price in micros of ONE bundle of `kind` (platform_prices, else the constant)."""
    _check_kind(kind)
    from app.services.telephony_billing import platform_price

    return await platform_price(session, f"{kind}_bundle")


def quote_from_list(kind: str, qty: int, list_each: int) -> dict[str, int]:
    """{list, discount, paid, unit_paid} in micros for `qty` bundles. Pure; integer math.

    The discount is computed on the unit price and rounded to whole cents, so what Stripe
    charges (qty x unit, in cents) is exactly `paid`.
    """
    if qty < 1 or qty > MAX_QTY:
        raise ValidationFailedError(f"Choose between 1 and {MAX_QTY} bundles.")
    unit_paid = list_each
    if kind in VOLUME_DISCOUNT_KINDS and qty >= VOLUME_MIN_QTY:
        unit_paid = list_each * (10_000 - VOLUME_DISCOUNT_BPS) // 10_000
    unit_paid = (unit_paid // 10_000) * 10_000  # whole cents
    total_list = list_each * qty
    paid = unit_paid * qty
    return {
        "list": total_list,
        "discount": total_list - paid,
        "paid": paid,
        "unit_paid": unit_paid,
        "units": UNITS_PER_BUNDLE[kind] * qty,
    }


async def quote(session: AsyncSession, kind: str, qty: int) -> dict[str, int]:
    return quote_from_list(kind, qty, await bundle_list_price(session, kind))


async def units(session: AsyncSession, org_id: uuid.UUID, kind: str) -> int:
    """Units left of `kind` for the org (newest row's balance_after_units; 0 if none)."""
    _check_kind(kind)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(BundleLedgerEntry.balance_after_units)
            .where(BundleLedgerEntry.kind == kind)
            .order_by(BundleLedgerEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row or 0)


async def _existing(
    session: AsyncSession, kind: str, entry_type: str, reference: str | None
) -> BundleLedgerEntry | None:
    if reference is None:
        return None
    return (
        await session.execute(
            sa.select(BundleLedgerEntry).where(
                BundleLedgerEntry.kind == kind,
                BundleLedgerEntry.entry_type == entry_type,
                BundleLedgerEntry.reference == reference,
            )
        )
    ).scalar_one_or_none()


async def _append(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    kind: str,
    entry_type: str,
    delta_units: int,
    reference: str | None,
    note: str | None = None,
) -> BundleLedgerEntry:
    _check_kind(kind)
    if entry_type not in BUNDLE_ENTRY_TYPES:
        raise ValidationFailedError(f"Unknown bundle entry type: {entry_type}")
    set_org_context(session, org_id)
    await credits._lock(session, org_id)
    existing = await _existing(session, kind, entry_type, reference)
    if existing is not None:
        return existing
    current = await units(session, org_id, kind)
    last_seq = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.max(BundleLedgerEntry.seq), 0)).where(
                BundleLedgerEntry.kind == kind
            )
        )
    ).scalar_one()
    entry = BundleLedgerEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        kind=kind,
        entry_type=entry_type,
        seq=int(last_seq) + 1,
        delta_units=int(delta_units),
        balance_after_units=current + int(delta_units),
        reference=reference,
        note=note,
    )
    session.add(entry)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        set_org_context(session, org_id)
        found = await _existing(session, kind, entry_type, reference)
        if found is None:
            raise
        return found
    return entry


async def credit(
    session: AsyncSession,
    org_id: uuid.UUID,
    kind: str,
    qty_units: int,
    *,
    reference: str,
    note: str | None = None,
    entry_type: str = "purchase",
) -> BundleLedgerEntry:
    if qty_units <= 0:
        raise ValidationFailedError("A bundle credit must be positive")
    return await _append(
        session,
        org_id,
        kind=kind,
        entry_type=entry_type,
        delta_units=qty_units,
        reference=reference,
        note=note,
    )


async def take(
    session: AsyncSession,
    org_id: uuid.UUID,
    kind: str,
    wanted: int,
    *,
    reference: str,
    note: str | None = None,
) -> int:
    """Spend up to `wanted` units; return how many were covered (0..wanted). Never goes
    below zero. Idempotent on `reference`: a replay returns what the first call covered."""
    if wanted <= 0:
        return 0
    set_org_context(session, org_id)
    await credits._lock(session, org_id)
    existing = await _existing(session, kind, "usage", reference)
    if existing is not None:
        return -int(existing.delta_units)
    have = await units(session, org_id, kind)
    covered = min(have, int(wanted))
    if covered <= 0:
        return 0
    await _append(
        session,
        org_id,
        kind=kind,
        entry_type="usage",
        delta_units=-covered,
        reference=reference,
        note=note,
    )
    return covered
