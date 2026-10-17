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
- 10DLC        = its own service: charged = what the customer paid for 10DLC (the checkout
                 invoice, plus the 10DLC line of each plan invoice, moved out of "Plans");
                 carrier cost = Telnyx's 10DLC fees DERIVED from tendlc_registrations (they
                 are not attributed per workspace in telnyx_cost_daily). An approximation,
                 booked on the registration's paid_at day: brand fee (450c) unless the
                 business was already approved (filing.brand_reused) and the brand was
                 filed; campaign review (1500c) + 3 months upfront once the campaign was
                 filed; then one month on each paid_at + 30*k days (k >= 3) while active.
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
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiUsageEvent,
    BillingPayment,
    Brand,
    BundleLedgerEntry,
    CreditLedgerEntry,
    FixedCost,
    Message,
    Org,
    OrgNumber,
    TenDlcRegistration,
)
from app.models.billing_v2 import TelnyxCostDaily
from app.models.spend import DEFAULT_RATES_MICROS, ProviderRate, ProviderSpendDaily
from app.services.console import U, _fixed_cost_micros, day_range

# Telnyx's 10DLC costs (cents; mirrors services/tendlc.py, which owns the sell side).
_TENDLC_BRAND_CENTS = 450
_TENDLC_REVIEW_CENTS = 1500
_TENDLC_MONTHLY_CENTS = {"standard": 1000, "sole_proprietor": 200}
_TENDLC_UPFRONT_MONTHS = 3
_TENDLC_BRAND_FILED_STAGES = (
    "brand_filed", "otp_pending", "brand_approved", "campaign_filed", "active",
    "brand_rejected", "campaign_rejected", "rejected",
)
_CENT = 10_000

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
    ("plans", "Plans"),
    ("tendlc", "10DLC"),
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
    if kind == "tendlc":
        return "tendlc"
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
        if kind.endswith("_bundle"):
            cell[(day, service)]["stripe_fees"] += int(fee or 0)
            continue  # bundles are revenue when consumed (above), never when bought
        refund = (detail or {}).get("refund") or {}
        if "refunded_micros" in refund:
            back = min(int(refund["refunded_micros"]), int(paid or 0))
        else:
            back = int(paid or 0) if state == "refunded" else 0
        charged = int(paid or 0) - back
        fee_v = int(fee or 0)
        tendlc = min(int((detail or {}).get("tendlc_micros") or 0), int(paid or 0))
        if service == "plans" and tendlc > 0:
            # The 10DLC line of a plan invoice is 10DLC revenue, not plan revenue; a refund
            # of the invoice is shared pro rata.
            tendlc_net = tendlc - back * tendlc // int(paid)
            tendlc_fee = fee_v * tendlc // int(paid)
            cell[(day, "tendlc")]["charged"] += tendlc_net
            cell[(day, "tendlc")]["stripe_fees"] += tendlc_fee
            charged -= tendlc_net
            fee_v -= tendlc_fee
        cell[(day, service)]["stripe_fees"] += fee_v
        cell[(day, service)]["charged"] += charged

    # --- 10DLC carrier fees, derived from the registrations (see the module docstring) ----
    regs = await _rows(
        session,
        sa.select(
            TenDlcRegistration.brand_id,
            TenDlcRegistration.fee_tier,
            TenDlcRegistration.stage,
            TenDlcRegistration.paid_at,
            TenDlcRegistration.filing,
            TenDlcRegistration.rejection,
        ).where(
            TenDlcRegistration.paid_at.is_not(None),
            scope(TenDlcRegistration.org_id),
        ),
    )
    telnyx_brands: set[uuid.UUID] = set()
    brand_ids = {r[0] for r in regs}
    if brand_ids:
        for bid, refs in await _rows(
            session, sa.select(Brand.id, Brand.carrier_refs).where(Brand.id.in_(brand_ids))
        ):
            if (refs or {}).get("telnyx"):
                telnyx_brands.add(bid)
    for brand_id, tier, stage, reg_paid_at, filing, rejection in regs:
        monthly = _TENDLC_MONTHLY_CENTS.get(tier, _TENDLC_MONTHLY_CENTS["standard"]) * _CENT
        paid_day = reg_paid_at.date()
        one_time = 0
        if not (filing or {}).get("brand_reused") and (
            brand_id in telnyx_brands or stage in _TENDLC_BRAND_FILED_STAGES
        ):
            one_time += _TENDLC_BRAND_CENTS * _CENT
        if stage in ("campaign_filed", "active", "campaign_rejected") or (
            stage == "rejected" and (rejection or {}).get("subject") == "campaign"
        ):
            one_time += _TENDLC_REVIEW_CENTS * _CENT + _TENDLC_UPFRONT_MONTHS * monthly
        if start <= paid_day <= end:
            cell[(paid_day, "tendlc")]["units"] += 1
            cell[(paid_day, "tendlc")]["carrier_cost"] += one_time
        if stage == "active":
            k = max(_TENDLC_UPFRONT_MONTHS, -(-((start - paid_day).days) // 30))
            while True:
                day = paid_day + timedelta(days=30 * k)
                if day > end:
                    break
                if day >= start:
                    cell[(day, "tendlc")]["carrier_cost"] += monthly
                k += 1

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
        if metric == "number_mrc":
            continue  # booked whole on each renewal day below, not spread over the month
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

    # --- number rental: the carrier bills a whole month on the purchase day and on each
    # monthly renewal day (owner 2026-10-17), which is also when we charge the customer.
    rates: dict[tuple[uuid.UUID, str], int] = {}
    for oid, carrier, monthly_cents, purchased_at, created_at, released_at in await _rows(
        session,
        sa.select(
            OrgNumber.org_id,
            OrgNumber.carrier,
            OrgNumber.monthly_cost_cents,
            OrgNumber.purchased_at,
            OrgNumber.created_at,
            OrgNumber.released_at,
        ).where(
            OrgNumber.status.in_(("active", "released")),
            scope(OrgNumber.org_id),
        ),
    ):
        bought = (purchased_at or created_at).date()
        gone = released_at.date() if released_at is not None else None
        if monthly_cents is not None:
            monthly = int(monthly_cents) * 10_000
        else:
            if (oid, carrier) not in rates:
                override = await _rows(
                    session,
                    sa.select(ProviderRate.unit_cost_micros).where(
                        ProviderRate.org_id == oid,
                        ProviderRate.provider == carrier,
                        ProviderRate.metric == "number_mrc",
                    ),
                )
                rates[(oid, carrier)] = int(
                    override[0][0] if override
                    else DEFAULT_RATES_MICROS.get(carrier, {}).get("number_mrc", 0)
                )
            monthly = rates[(oid, carrier)]
        day = max(start, bought)
        while day <= end and (gone is None or day < gone):
            renewal_day = min(bought.day, monthrange(day.year, day.month)[1])
            if day.day == renewal_day:
                cell[(day, "numbers")]["carrier_cost"] += monthly
                cell[(day, "numbers")]["units"] += 1
            day += timedelta(days=1)

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
