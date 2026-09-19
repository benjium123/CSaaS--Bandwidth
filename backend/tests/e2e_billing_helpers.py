"""Shared helpers for the end-to-end billing / tenancy tests.

Not a test module (the name does not start with ``test_``), so pytest never collects it.
Every expected money amount a test needs is read from the live pricing service at run
time through :func:`price_of` - never hardcoded - so the suite survives the platform's
move to flat rates.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import CreditLedgerEntry, Message, Org
from app.models.spend import ProviderRate
from app.services import credits, telephony_billing

#: The provider/carrier name the loopback test carrier is registered under.
LOOPBACK_PROVIDER = "loopback"

#: The metrics the loopback provider must be priced on for any charge to happen at all.
LOOPBACK_METRICS: tuple[str, ...] = ("sms_out", "sms_in", "voice_min_out", "voice_min_in")

#: The test-only unit price seeded on the loopback provider, in micros.
SEEDED_UNIT_MICROS = 10_000


async def seed_rates(session: AsyncSession, org_id) -> None:
    """Ensure provider "loopback" is actually priced, so nothing here is ever free.

    This is a FALLBACK, never an override of the platform price. The platform now prices
    most metrics from the flat ``PLATFORM_PRICE_MICROS`` table, and an explicit per-org
    ``provider_rates`` row WINS over that table - so seeding one unconditionally would make
    every test measure our own seeded number and keep passing even against a platform that
    charges nothing, which is exactly the failure this suite exists to catch.

    For every metric we therefore ask the live pricing code FIRST: if it already returns a
    positive price we insert nothing and let the test measure the real price. Only when it
    reports no price at all (0) do we insert the fallback ``ProviderRate`` row, which keeps
    the suite runnable if flat pricing is ever reverted and "loopback" goes back to being
    an unpriced carrier.
    """
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    added = False
    for metric in LOOPBACK_METRICS:
        existing = await telephony_billing.unit_price(
            session, org_uuid, LOOPBACK_PROVIDER, metric
        )
        if existing > 0:
            # The platform already prices this metric; an override would mask the real
            # (possibly later zeroed) price, so leave it alone.
            continue
        session.add(
            ProviderRate(
                id=uuid.uuid4(),
                org_id=org_uuid,
                provider=LOOPBACK_PROVIDER,
                metric=metric,
                scope="traffic",
                currency="USD",
                unit_cost_micros=SEEDED_UNIT_MICROS,
                price_micros=SEEDED_UNIT_MICROS,
            )
        )
        added = True
    if added:
        await session.commit()


async def price_of(session: AsyncSession, org_id, metric: str) -> int:
    """The live customer price in micros for one unit of ``metric`` on the loopback
    provider. Fails loudly when nothing is priced, because a 0 price cannot prove charging."""
    org_uuid = uuid.UUID(str(org_id))
    price = await telephony_billing.unit_price(session, org_uuid, LOOPBACK_PROVIDER, metric)
    assert price > 0, f"no price configured for {metric}; this test cannot prove charging"
    return int(price)


async def enable_prepaid(
    session: AsyncSession, org_id, *, balance_micros: int = 0
) -> None:
    """Switch the org's prepaid gate on, dated an hour ago so this test's traffic is in
    scope, and top its balance up when asked."""
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    org = await session.get(Org, org_uuid)
    assert org is not None, f"org {org_uuid} not found"
    org.telephony_prepaid = True
    org.telephony_prepaid_since = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    if balance_micros > 0:
        await credits.topup(
            session, org_uuid, balance_micros, reference=f"topup-{uuid.uuid4()}"
        )
        await session.commit()


async def usage_entries(session: AsyncSession, org_id) -> list[CreditLedgerEntry]:
    """Every usage charge booked against the org, in ledger order."""
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    return list(
        (
            await session.execute(
                sa.select(CreditLedgerEntry)
                .where(CreditLedgerEntry.entry_type == "usage")
                .order_by(CreditLedgerEntry.seq)
            )
        )
        .scalars()
        .all()
    )


async def messages_of(session: AsyncSession, org_id) -> list[Message]:
    """Every message the org can see, read through the tenant hook with the org's own
    context. Deliberately NOT unscoped - reading the way a real request does is part of
    what proves isolation."""
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    return list(
        (
            await session.execute(sa.select(Message).order_by(Message.created_at))
        )
        .scalars()
        .all()
    )


async def balance(session: AsyncSession, org_id) -> int:
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    return await credits.balance(session, org_uuid)
