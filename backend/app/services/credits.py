"""P24 prepaid credits: the ONLY module allowed to write credit_ledger (Fable-owned).

Invariants (see models/billing.py):
- append-only; every write is one new row carrying balance_after_micros
- a (org, entry_type, reference) pair is written once - replays return the existing row
- Postgres: a per-org transaction advisory lock serialises concurrent appends so two
  writers cannot compute the same balance_after; SQLite (tests) is single-writer anyway
- integer micros everywhere

Flow for one AI call: reserve(estimate) at call start -> release(same reference) at call
end -> charge_usage(actual price, reference=usage event id). Reserves older than the
sweeper's window are released automatically (a crashed worker must not lock credits).
"""

from __future__ import annotations

import uuid
import zlib
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import InsufficientCreditsError, ValidationFailedError
from app.models import LEDGER_ENTRY_TYPES, CreditLedgerEntry, Org
from app.models.billing import DEFAULT_AI_MARKUP_BPS

#: Low-balance thresholds as a fraction of the most recent top-up (plan: 20% and 5%).
WARNING_FRACTIONS: tuple[float, ...] = (0.20, 0.05)
#: A reserve older than this with no release is auto-released by the sweeper.
STALE_RESERVE_AFTER = timedelta(minutes=45)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _lock(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Serialise ledger appends per org. Postgres advisory lock (released at commit);
    no-op on SQLite, whose file lock already serialises writers."""
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        key = zlib.crc32(str(org_id).encode()) & 0x7FFFFFFF
        await session.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


async def balance(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Newest row's balance_after; 0 for an org with no ledger yet."""
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(CreditLedgerEntry.balance_after_micros)
            .order_by(CreditLedgerEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row or 0)


async def _existing(
    session: AsyncSession, org_id: uuid.UUID, entry_type: str, reference: str | None
) -> CreditLedgerEntry | None:
    if reference is None:
        return None
    return (
        await session.execute(
            sa.select(CreditLedgerEntry).where(
                CreditLedgerEntry.entry_type == entry_type,
                CreditLedgerEntry.reference == reference,
            )
        )
    ).scalar_one_or_none()


async def _append(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    entry_type: str,
    amount_micros: int,
    reference: str | None,
    note: str | None = None,
    created_by: uuid.UUID | None = None,
) -> CreditLedgerEntry:
    if entry_type not in LEDGER_ENTRY_TYPES:
        raise ValidationFailedError(f"Unknown ledger entry type: {entry_type}")
    set_org_context(session, org_id)
    await _lock(session, org_id)
    existing = await _existing(session, org_id, entry_type, reference)
    if existing is not None:
        return existing
    current = await balance(session, org_id)
    last_seq = (
        await session.execute(sa.select(sa.func.coalesce(sa.func.max(CreditLedgerEntry.seq), 0)))
    ).scalar_one()
    entry = CreditLedgerEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        seq=int(last_seq) + 1,
        entry_type=entry_type,
        amount_micros=int(amount_micros),
        balance_after_micros=current + int(amount_micros),
        reference=reference,
        note=note,
        created_by=created_by,
    )
    session.add(entry)
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race on the same reference (only possible without the advisory lock,
        # i.e. SQLite under concurrent tasks): the other writer's row is the truth.
        await session.rollback()
        set_org_context(session, org_id)
        found = await _existing(session, org_id, entry_type, reference)
        if found is None:
            raise
        return found
    return entry


async def topup(
    session: AsyncSession,
    org_id: uuid.UUID,
    amount_micros: int,
    *,
    reference: str,
    created_by: uuid.UUID | None = None,
    note: str | None = None,
) -> CreditLedgerEntry:
    if amount_micros <= 0:
        raise ValidationFailedError("A top-up must be a positive amount")
    return await _append(
        session,
        org_id,
        entry_type="topup",
        amount_micros=amount_micros,
        reference=reference,
        note=note,
        created_by=created_by,
    )


async def reserve(
    session: AsyncSession, org_id: uuid.UUID, amount_micros: int, *, reference: str
) -> CreditLedgerEntry:
    """Hold `amount_micros` for an in-flight call. Refuses (402) when the balance cannot
    cover it - the caller turns that into "Add credits to keep your assistant answering"."""
    if amount_micros < 0:
        raise ValidationFailedError("A reserve cannot be negative")
    set_org_context(session, org_id)
    await _lock(session, org_id)
    existing = await _existing(session, org_id, "reserve", reference)
    if existing is not None:
        return existing
    if await balance(session, org_id) < amount_micros:
        raise InsufficientCreditsError()
    return await _append(
        session, org_id, entry_type="reserve", amount_micros=-amount_micros, reference=reference
    )


async def release(
    session: AsyncSession, org_id: uuid.UUID, *, reference: str
) -> CreditLedgerEntry | None:
    """Give a reserve back. Idempotent; None when no reserve exists for the reference."""
    set_org_context(session, org_id)
    held = await _existing(session, org_id, "reserve", reference)
    if held is None:
        return None
    return await _append(
        session,
        org_id,
        entry_type="release",
        amount_micros=-held.amount_micros,
        reference=reference,
    )


async def charge_usage(
    session: AsyncSession,
    org_id: uuid.UUID,
    price_micros: int,
    *,
    reference: str,
    note: str | None = None,
) -> CreditLedgerEntry:
    """Debit the customer price of a usage event. May take the balance below zero (the
    reserve was the guard; the actual cost is owed regardless)."""
    if price_micros < 0:
        raise ValidationFailedError("A usage charge cannot be negative")
    return await _append(
        session,
        org_id,
        entry_type="usage",
        amount_micros=-price_micros,
        reference=reference,
        note=note,
    )


async def adjust(
    session: AsyncSession,
    org_id: uuid.UUID,
    amount_micros: int,
    *,
    reference: str,
    note: str,
    created_by: uuid.UUID | None,
    entry_type: str = "adjustment",
) -> CreditLedgerEntry:
    """Platform-ops only (the route enforces it). A note is mandatory - money moves with a
    reason or not at all."""
    if entry_type not in ("adjustment", "refund"):
        raise ValidationFailedError("entry_type must be adjustment or refund")
    if not note or not note.strip():
        raise ValidationFailedError("Say why this adjustment is being made")
    return await _append(
        session,
        org_id,
        entry_type=entry_type,
        amount_micros=amount_micros,
        reference=reference,
        note=note.strip(),
        created_by=created_by,
    )


async def outstanding_reserves(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Sum of reserves not yet released (positive number of micros)."""
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(
                CreditLedgerEntry.entry_type,
                CreditLedgerEntry.reference,
                CreditLedgerEntry.amount_micros,
            ).where(
                CreditLedgerEntry.entry_type.in_(("reserve", "release"))
            )
        )
    ).all()
    held: dict[str | None, int] = {}
    for entry_type, reference, amount in rows:
        if entry_type == "reserve":
            held[reference] = held.get(reference, 0) - int(amount)
        else:
            held[reference] = held.get(reference, 0) - int(amount)
    return sum(v for v in held.values() if v > 0)


async def release_stale_reserves(
    session: AsyncSession, org_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """Sweeper: release every reserve older than STALE_RESERVE_AFTER with no release."""
    set_org_context(session, org_id)
    cutoff = (now or _now()) - STALE_RESERVE_AFTER
    reserves = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(
                CreditLedgerEntry.entry_type == "reserve",
                CreditLedgerEntry.created_at < cutoff,
            )
        )
    ).scalars().all()
    released = 0
    for held in reserves:
        if await _existing(session, org_id, "release", held.reference) is None:
            await release(session, org_id, reference=held.reference or "")
            released += 1
    return released


async def integrity_check(session: AsyncSession, org_id: uuid.UUID) -> tuple[bool, int, int]:
    """(ok, sum_of_amounts, last_balance_after). Nightly job asserts ok."""
    set_org_context(session, org_id)
    total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CreditLedgerEntry.amount_micros), 0))
        )
    ).scalar_one()
    last = await balance(session, org_id)
    return int(total) == last, int(total), last


def price_for(
    *,
    cost_micros: int,
    kind: str,
    quantity: int,
    org: Org,
) -> int:
    """Customer price for one usage event.

    platform mode: cost x (1 + markup)            (markup = org override or platform default)
    byok voice:    platform fee per minute x minutes (pro-rated by seconds)
    byok other:    0 (the org pays its own provider)
    """
    mode = getattr(org, "ai_key_mode", "platform") or "platform"
    if mode == "byok":
        if kind == "voice":
            fee = int(org.ai_platform_fee_per_minute_micros or 0)
            return (fee * int(quantity) + 59) // 60  # quantity = seconds, rounded up
        return 0
    bps = org.ai_markup_bps if org.ai_markup_bps is not None else DEFAULT_AI_MARKUP_BPS
    return (int(cost_micros) * (10_000 + int(bps)) + 9_999) // 10_000


def warning_level(current_balance: int, last_topup: int) -> str | None:
    """'low' at 20% of the last top-up, 'critical' at 5%, 'empty' at or below zero."""
    if current_balance <= 0:
        return "empty"
    if last_topup <= 0:
        return None
    frac = current_balance / last_topup
    if frac <= WARNING_FRACTIONS[1]:
        return "critical"
    if frac <= WARNING_FRACTIONS[0]:
        return "low"
    return None
