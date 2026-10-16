"""Profit by day and by service, for Switchboard > Profit (owner order 2026-10-16: "the total
usage per day and the profit we made; pick an organization; toggle calls / SMS / fax /
recording / transcription / everything we give").

Read-only and operator-only (the route enforces it); every query is explicitly unscoped.
Same money definitions as services/console.py (accrual view), split by day (UTC) and
service so the page can filter without another request:

- charged      = usage debited from balances (ledger 'usage', by reference prefix), less
                 usage refunded to the balance (ledger 'refund' of "refund:<usage ref>")
                 + bundle units consumed x the org's average paid price per unit
                 + payments that are not prepayments (plans, 10DLC, invoices), cash back off
- carrier_cost = provider_spend_daily estimate; Telnyx's own billed cost (telnyx_cost_daily)
                 replaces the Telnyx estimate for the same org/day/service when present
- ai_cost      = ai_usage_events cost (what we paid AI providers)
- stripe_fees  = Stripe's fee on each payment (refunded ones too: Stripe keeps it), on the
                 service the payment was for; top-ups and auto-recharges are not one
                 service, so their fees show only under "all"
- fixed_costs  = fixed_costs pro-rated per day; only in the all-workspaces view
- profit       = charged - carrier_cost - ai_cost - stripe_fees (- fixed in the total)

Telnyx traffic with no workspace (the CRM sharing the account) is NOT Ringlite's and is
left out; console.platform_costs still reports it as "unattributed".
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiUsageEvent,
    BillingPayment,
    BundleLedgerEntry,
    CreditLedgerEntry,
    FixedCost,
    Message,
    Org,
)
from app.models.billing_v2 import TelnyxCostDaily
from app.models.spend import ProviderSpendDaily
from app.services.console import U, _fixed_cost_micros, day_range

SERVICES: tuple[tuple[str, str], ...] = (
    ("calls", "Calls"),
    ("sms", "SMS"),
    ("mms", "MMS"),
    ("fax", "Fax"),
    ("numbers", "Numbers"),
    ("recording", "Recording"),
    ("transcription", "Transcription"),
    ("summaries", "Summaries"),
    ("ai", "AI"),
    ("plans", "Plans & 10DLC"),
    ("other", "Other"),
)
FIELDS = ("charged", "carrier_cost", "ai_cost", "stripe_fees", "units")
MAX_DAYS = 366


def service_for_reference(reference: str | None) -> str:
    ref = reference or ""
    prefix = ref.split(":", 1)[0]
    if prefix == "call":
        return "transcription" if ref.endswith(":transcript") else "calls"
    return {
        "sms": "sms",  # split into sms / mms by the message's media below
        "fax": "fax",
        "num": "numbers",
        "rec": "recording",
        "sum": "summaries",
        "vmstt": "transcription",
        "usage": "ai",
    }.get(prefix, "other")


def service_for_metric(metric: str) -> str:
    if metric.startswith("voice_"):
        return "calls"
    if metric.startswith("sms_"):
        return "sms"
    if metric.startswith("mms_"):
        return "mms"
    if metric.startswith("fax_"):
        return "fax"
    if metric.startswith("number_"):
        return "numbers"
    return "other"


def service_for_payment(kind: str) -> str | None:
    """None = a prepayment (balance top-up): not revenue, and not one service."""
    if kind in ("topup", "auto_recharge"):
        return None
    if kind.endswith("_bundle"):
        return {"voice": "calls", "sms": "sms", "mms": "mms"}.get(kind[: -len("_bundle")], "other")
    if kind.startswith("number"):
        return "numbers"
    return "plans"


def service_for_ai(metric: str, kind: str) -> str:
    text = f"{metric} {kind}".lower()
    if "stt" in text or "transcri" in text:
        return "transcription"
    if "summar" in text:
        return "summaries"
    return "ai"


async def _rows(session: AsyncSession, stmt) -> list:  # noqa: ANN001
    return (await session.execute(stmt.execution_options(**U))).all()


async def daily(
    session: AsyncSession,
    start: date | None,
    end: date | None,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    lo, hi, start, end = day_range(start, end)
    if (end - start).days + 1 > MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
        lo, hi, start, end = day_range(start, end)

    def scope(col):  # noqa: ANN001, ANN202
        return col == org_id if org_id is not None else sa.true()

    # cell[(day, service)][field]
    cell: dict[tuple[date, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unallocated_fees: dict[date, int] = defaultdict(int)

    # --- charged: balance usage ---------------------------------------------------------
    usage = await _rows(
        session,
        sa.select(
            CreditLedgerEntry.created_at,
            CreditLedgerEntry.reference,
            CreditLedgerEntry.amount_micros,
            CreditLedgerEntry.entry_type,
        ).where(
            CreditLedgerEntry.entry_type.in_(("usage", "refund")),
            CreditLedgerEntry.created_at >= lo,
            CreditLedgerEntry.created_at < hi,
            scope(CreditLedgerEntry.org_id),
        ),
    )
    # A usage charge refunded to the balance comes back off what we charged, on the refund's
    # day. Card refunds (top-ups) are handled with the payments below, so a refund whose
    # inner reference is not a usage charge is skipped here.
    usage = [
        (created_at, reference, amount)
        if entry_type == "usage"
        else (created_at, reference[len("refund:"):], int(amount))  # +: comes off charged
        for created_at, reference, amount, entry_type in usage
        if entry_type == "usage"
        or (
            (reference or "").startswith("refund:")
            and service_for_reference(reference[len("refund:"):]) != "other"
        )
    ]
    sms_ids: set[uuid.UUID] = set()
    for _, reference, _ in usage:
        if (reference or "").startswith("sms:"):
            try:
                sms_ids.add(uuid.UUID(reference.split(":")[1]))
            except (ValueError, IndexError):
                pass
    mms_ids: set[uuid.UUID] = set()
    if sms_ids:
        for mid, media in await _rows(
            session, sa.select(Message.id, Message.media).where(Message.id.in_(sms_ids))
        ):
            if media:
                mms_ids.add(mid)
    for created_at, reference, amount in usage:
        service = service_for_reference(reference)
        if service == "sms":
            try:
                if uuid.UUID(reference.split(":")[1]) in mms_ids:
                    service = "mms"
            except (ValueError, IndexError):
                pass
        cell[(created_at.date(), service)]["charged"] += -int(amount)

    # --- charged: bundle units consumed, valued at the org's average paid unit price ----
    used = await _rows(
        session,
        sa.select(
            BundleLedgerEntry.org_id,
            BundleLedgerEntry.kind,
            BundleLedgerEntry.created_at,
            -BundleLedgerEntry.delta_units,
        ).where(
            BundleLedgerEntry.entry_type == "usage",
            BundleLedgerEntry.created_at >= lo,
            BundleLedgerEntry.created_at < hi,
            scope(BundleLedgerEntry.org_id),
        ),
    )
    if used:
        unit_price: dict[tuple[uuid.UUID, str], tuple[int, int]] = {}
        for oid, kind, paid_sum, units_sum in await _rows(
            session,
            sa.select(
                BillingPayment.org_id,
                BillingPayment.kind,
                sa.func.coalesce(sa.func.sum(BillingPayment.paid_micros), 0),
                sa.func.coalesce(sa.func.sum(BillingPayment.units_credited), 0),
            )
            .where(
                BillingPayment.state == "paid",
                BillingPayment.kind.in_(("sms_bundle", "mms_bundle", "voice_bundle")),
                BillingPayment.paid_at < hi,
                scope(BillingPayment.org_id),
            )
            .group_by(BillingPayment.org_id, BillingPayment.kind),
        ):
            unit_price[(oid, kind[: -len("_bundle")])] = (int(paid_sum), int(units_sum))
        for oid, kind, created_at, units in used:
            paid_sum, units_sum = unit_price.get((oid, kind), (0, 0))
            service = {"voice": "calls", "sms": "sms", "mms": "mms"}.get(kind, "other")
            if units_sum > 0 and int(units) > 0:
                cell[(created_at.date(), service)]["charged"] += int(units) * paid_sum // units_sum

    # --- payments: direct revenue (not prepayments) and Stripe fees ----------------------
    for paid_at, kind, state, paid, fee, detail in await _rows(
        session,
        sa.select(
            BillingPayment.paid_at,
            BillingPayment.kind,
            BillingPayment.state,
            BillingPayment.paid_micros,
            BillingPayment.stripe_fee_micros,
            BillingPayment.detail,
        ).where(
            BillingPayment.state.in_(("paid", "refunded")),
            BillingPayment.paid_at >= lo,
            BillingPayment.paid_at < hi,
            scope(BillingPayment.org_id),
        ),
    ):
        day = paid_at.date()
        service = service_for_payment(kind)
        if service is None:
            unallocated_fees[day] += int(fee or 0)
            continue
        cell[(day, service)]["stripe_fees"] += int(fee or 0)
        if kind.endswith("_bundle"):
            continue  # bundles are revenue when consumed (above), never when bought
        refund = (detail or {}).get("refund") or {}
        if "refunded_micros" in refund:
            back = min(int(refund["refunded_micros"]), int(paid or 0))
        else:
            back = int(paid or 0) if state == "refunded" else 0
        cell[(day, service)]["charged"] += int(paid or 0) - back

    # --- carrier cost: estimate, with Telnyx actuals replacing the Telnyx estimate --------
    telnyx_actual: dict[tuple[uuid.UUID, date, str], tuple[int, int]] = {}
    for oid, day, record_type, qty, cost in await _rows(
        session,
        sa.select(
            TelnyxCostDaily.org_id,
            TelnyxCostDaily.period_date,
            TelnyxCostDaily.record_type,
            sa.func.coalesce(sa.func.sum(TelnyxCostDaily.quantity), 0),
            sa.func.coalesce(sa.func.sum(TelnyxCostDaily.cost_micros), 0),
        )
        .where(
            TelnyxCostDaily.period_date >= start,
            TelnyxCostDaily.period_date <= end,
            TelnyxCostDaily.org_id.is_not(None),
            scope(TelnyxCostDaily.org_id),
        )
        .group_by(TelnyxCostDaily.org_id, TelnyxCostDaily.period_date, TelnyxCostDaily.record_type),
    ):
        service = {"voice": "calls", "sms": "sms", "mms": "mms", "fax": "fax"}.get(
            record_type, "other"
        )
        q, c = telnyx_actual.get((oid, day, service), (0, 0))
        telnyx_actual[(oid, day, service)] = (q + int(qty), c + int(cost))

    replaced: set[tuple[uuid.UUID, date, str]] = set()
    for oid, day, provider, metric, qty, cost in await _rows(
        session,
        sa.select(
            ProviderSpendDaily.org_id,
            ProviderSpendDaily.period_date,
            ProviderSpendDaily.provider,
            ProviderSpendDaily.metric,
            sa.func.coalesce(sa.func.sum(ProviderSpendDaily.quantity), 0),
            sa.func.coalesce(sa.func.sum(ProviderSpendDaily.cost_micros), 0),
        )
        .where(
            ProviderSpendDaily.period_date >= start,
            ProviderSpendDaily.period_date <= end,
            scope(ProviderSpendDaily.org_id),
        )
        .group_by(
            ProviderSpendDaily.org_id,
            ProviderSpendDaily.period_date,
            ProviderSpendDaily.provider,
            ProviderSpendDaily.metric,
        ),
    ):
        service = service_for_metric(metric)
        c = cell[(day, service)]
        if provider == "telnyx" and (oid, day, service) in telnyx_actual:
            replaced.add((oid, day, service))
            continue
        c["carrier_cost"] += int(cost)
        if service != "numbers":  # number rows count number-days, not usage
            c["units"] += int(qty)
    for key, (qty, cost) in telnyx_actual.items():
        _, day, service = key
        cell[(day, service)]["carrier_cost"] += cost
        cell[(day, service)]["units"] += qty

    # --- AI provider cost -------------------------------------------------------------------
    for occurred_at, metric, kind, cost in await _rows(
        session,
        sa.select(
            AiUsageEvent.occurred_at,
            AiUsageEvent.metric,
            AiUsageEvent.kind,
            AiUsageEvent.cost_micros,
        ).where(
            AiUsageEvent.occurred_at >= lo,
            AiUsageEvent.occurred_at < hi,
            scope(AiUsageEvent.org_id),
        ),
    ):
        cell[(occurred_at.date(), service_for_ai(metric or "", kind or ""))]["ai_cost"] += int(
            cost or 0
        )

    # --- fixed costs (all-workspaces view only) ---------------------------------------------
    fixed_rows = []
    if org_id is None:
        fixed_rows = list((await session.execute(sa.select(FixedCost))).scalars().all())

    days = []
    totals: dict[str, int] = defaultdict(int)
    by_service_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    d = start
    while d <= end:
        services = {}
        for key, _label in SERVICES:
            c = cell.get((d, key))
            if not c or not any(c.values()):
                continue
            row = {f: int(c.get(f, 0)) for f in FIELDS}
            row["profit"] = row["charged"] - row["carrier_cost"] - row["ai_cost"] - row["stripe_fees"]
            services[key] = row
            for f, v in row.items():
                by_service_totals[key][f] += v
        fixed = _fixed_cost_micros(fixed_rows, d, d) if fixed_rows else 0
        fees_unallocated = int(unallocated_fees.get(d, 0))
        day_row = {f: sum(s[f] for s in services.values()) for f in (*FIELDS, "profit")}
        day_row["stripe_fees"] += fees_unallocated
        day_row["fixed_costs"] = fixed
        day_row["profit"] -= fees_unallocated + fixed
        days.append({
            "date": d.isoformat(),
            "services": services,
            "unallocated_stripe_fees": fees_unallocated,
            "fixed_costs": fixed,
            "total": {k: v for k, v in day_row.items() if k != "units"},
        })
        for f, v in day_row.items():
            if f != "units":
                totals[f] += v
        d += timedelta(days=1)

    org_name = None
    if org_id is not None:
        org = await session.get(Org, org_id, execution_options=U)
        org_name = org.name if org is not None else None
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "org_id": str(org_id) if org_id else None,
        "org_name": org_name,
        "services": [{"key": k, "label": label} for k, label in SERVICES],
        "days": days,
        "totals": dict(totals),
        "service_totals": {k: dict(v) for k, v in by_service_totals.items()},
        "notes": [
            "Days are UTC. Carrier cost uses Telnyx's own bill where it has arrived, else our "
            "estimate; SignalWire estimates leave out its SIP audio leg (~$0.003/min).",
            "Telnyx traffic with no workspace (the CRM on the shared account) is left out.",
        ],
    }
