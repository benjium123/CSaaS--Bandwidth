"""Admin console aggregates (billing v2): every org's purchases, discounts, usage, traffic,
blocked attempts and our profit, over a date range. Read-only, operator-only (the routes
enforce require_operator). Cross-tenant by design - every query is explicitly unscoped.

Money definitions (integer micros):
- paid            = what customers actually paid by card (billing_payments, state paid)
- list            = what those purchases list at before discounts
- discount        = list - paid (bundle volume discounts)
- stripe_fees     = Stripe's processing fees on those payments (filled hourly)
- usage_revenue   = $ usage debited from balances (ledger 'usage'); bundle usage is prepaid
- carrier_cost    = our carrier cost estimate (provider_spend_daily)
- telnyx_actual   = Telnyx's own billed cost (telnyx_cost_daily, when reconciled)
- cash_profit     = paid - stripe_fees - (telnyx_actual when present else carrier_cost)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import (
    BillingPayment,
    BillingRefusal,
    BundleLedgerEntry,
    Call,
    CreditLedgerEntry,
    Message,
    MessageThread,
    Org,
    OrgNumber,
)
from app.models.compliance import ComplianceBlock
from app.models.spend import ProviderSpendDaily

U = {ALLOW_UNSCOPED_KEY: True}


def day_range(start: date | None, end: date | None) -> tuple[datetime, datetime, date, date]:
    """[start, end] inclusive days (UTC) -> half-open datetimes. Default: last 30 days."""
    today = datetime.now(timezone.utc).date()
    end = end or today
    start = start or (end - timedelta(days=29))
    if start > end:
        start, end = end, start
    lo = datetime.combine(start, time.min, tzinfo=timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return lo, hi, start, end


def _blank() -> dict[str, int]:
    return defaultdict(int)


async def _rows(session: AsyncSession, stmt) -> list:  # noqa: ANN001
    return (await session.execute(stmt.execution_options(**U))).all()


async def org_metrics(
    session: AsyncSession, lo: datetime, hi: datetime, start: date, end: date,
    org_ids: list[uuid.UUID] | None = None,
) -> dict[uuid.UUID, dict[str, int]]:
    """Per-org metric dict for the window. Missing keys mean 0."""
    out: dict[uuid.UUID, dict[str, int]] = defaultdict(_blank)

    def scope(col):  # noqa: ANN001, ANN202
        return col.in_(org_ids) if org_ids else sa.true()

    # --- texts: segments / messages by direction and type (SMS channel only) --------
    msg_rows = await _rows(
        session,
        sa.select(
            Message.org_id,
            Message.direction,
            Message.status,
            Message.error_code,
            Message.moderation_state,
            Message.media,
            sa.func.coalesce(Message.segment_count_carrier, Message.segment_count_est, 1),
            Message.carrier,
        )
        .join(MessageThread, MessageThread.id == Message.thread_id)
        .where(
            Message.created_at >= lo,
            Message.created_at < hi,
            MessageThread.channel == "sms",
            scope(Message.org_id),
        ),
    )
    for org_id, direction, status, error_code, moderation, media, segments, carrier in msg_rows:
        m = out[org_id]
        m[f"carrier_{carrier or 'unknown'}_texts"] += 1
        mms = bool(media)
        d = "out" if direction == "outbound" else "in"
        if moderation == "blocked":
            m["blocked_moderation"] += 1
            continue
        if error_code == "insufficient_credits":
            m["blocked_credit_sms"] += 1
            continue
        if direction == "outbound" and status in ("rejected", "failed", "undelivered"):
            m[f"{'mms' if mms else 'sms'}_out_failed"] += 1
        if mms:
            m[f"mms_{d}"] += 1
        else:
            m[f"sms_{d}_messages"] += 1
            m[f"sms_{d}_segments"] += int(segments or 1)

    # --- calls ------------------------------------------------------------------------
    call_rows = await _rows(
        session,
        sa.select(
            Call.org_id, Call.direction, Call.answered_at, Call.created_at, Call.ended_at,
            Call.duration_seconds, Call.extra, Call.carrier,
        ).where(Call.created_at >= lo, Call.created_at < hi, scope(Call.org_id)),
    )
    from app.services.telephony_billing import INBOUND_MIN_SECONDS, _as_utc

    for org_id, direction, answered_at, created_at, ended_at, duration, extra, carrier in call_rows:
        m = out[org_id]
        m[f"carrier_{carrier or 'unknown'}_calls"] += 1
        d = "out" if direction == "outbound" else "in"
        if (extra or {}).get("refused"):
            m["blocked_credit_inbound_call"] += 1
            continue
        m[f"calls_{d}"] += 1
        if answered_at is not None:
            m[f"calls_{d}_answered"] += 1
        if ended_at is not None:
            if direction == "outbound":
                secs = max(int(duration or 0), 0)
            else:
                secs = max(
                    int((_as_utc(ended_at) - _as_utc(created_at)).total_seconds()),
                    INBOUND_MIN_SECONDS,
                )
            m[f"minutes_{d}"] += (secs + 59) // 60 if secs > 0 else 0
            m[f"talk_seconds_{d}"] += int(duration or 0)

    # --- money: ledger ------------------------------------------------------------------
    for org_id, entry_type, amount in await _rows(
        session,
        sa.select(
            CreditLedgerEntry.org_id,
            CreditLedgerEntry.entry_type,
            sa.func.coalesce(sa.func.sum(CreditLedgerEntry.amount_micros), 0),
        )
        .where(
            CreditLedgerEntry.created_at >= lo,
            CreditLedgerEntry.created_at < hi,
            scope(CreditLedgerEntry.org_id),
        )
        .group_by(CreditLedgerEntry.org_id, CreditLedgerEntry.entry_type),
    ):
        if entry_type == "usage":
            out[org_id]["usage_revenue"] += -int(amount)
        elif entry_type in ("adjustment", "refund"):
            out[org_id][f"{entry_type}s"] += int(amount)

    # --- money: payments ------------------------------------------------------------------
    for org_id, kind, n, lst, paid, disc, fee, units in await _rows(
        session,
        sa.select(
            BillingPayment.org_id,
            BillingPayment.kind,
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(BillingPayment.list_micros), 0),
            sa.func.coalesce(sa.func.sum(BillingPayment.paid_micros), 0),
            sa.func.coalesce(sa.func.sum(BillingPayment.discount_micros), 0),
            sa.func.coalesce(sa.func.sum(BillingPayment.stripe_fee_micros), 0),
            sa.func.coalesce(sa.func.sum(BillingPayment.units_credited), 0),
        )
        .where(
            BillingPayment.state == "paid",
            BillingPayment.paid_at >= lo,
            BillingPayment.paid_at < hi,
            scope(BillingPayment.org_id),
        )
        .group_by(BillingPayment.org_id, BillingPayment.kind),
    ):
        m = out[org_id]
        m["payments"] += int(n)
        m["paid"] += int(paid)
        m["list"] += int(lst)
        m["discount"] += int(disc)
        m["stripe_fees"] += int(fee)
        m[f"paid_{kind}"] += int(paid)
        if kind.endswith("_bundle"):
            m[f"{kind}s_bought"] += int(units)

    # --- bundle usage -----------------------------------------------------------------
    for org_id, kind, used in await _rows(
        session,
        sa.select(
            BundleLedgerEntry.org_id,
            BundleLedgerEntry.kind,
            sa.func.coalesce(sa.func.sum(-BundleLedgerEntry.delta_units), 0),
        )
        .where(
            BundleLedgerEntry.entry_type == "usage",
            BundleLedgerEntry.created_at >= lo,
            BundleLedgerEntry.created_at < hi,
            scope(BundleLedgerEntry.org_id),
        )
        .group_by(BundleLedgerEntry.org_id, BundleLedgerEntry.kind),
    ):
        out[org_id][f"{kind}_bundle_units_used"] += int(used)

    # --- blocked: compliance (DNC/opt-out/quiet hours...), credit refusals -------------
    for org_id, reason, n in await _rows(
        session,
        sa.select(ComplianceBlock.org_id, ComplianceBlock.reason, sa.func.count())
        .where(
            ComplianceBlock.created_at >= lo,
            ComplianceBlock.created_at < hi,
            scope(ComplianceBlock.org_id),
        )
        .group_by(ComplianceBlock.org_id, ComplianceBlock.reason),
    ):
        out[org_id]["blocked_compliance"] += int(n)
        out[org_id][f"blocked_compliance_{reason}"] += int(n)
    for org_id, kind, n in await _rows(
        session,
        sa.select(BillingRefusal.org_id, BillingRefusal.kind, sa.func.count())
        .where(
            BillingRefusal.created_at >= lo,
            BillingRefusal.created_at < hi,
            scope(BillingRefusal.org_id),
        )
        .group_by(BillingRefusal.org_id, BillingRefusal.kind),
    ):
        # inbound_call refusals are already counted from the call rows above.
        if kind != "inbound_call":
            out[org_id][f"blocked_credit_{kind}"] += int(n)

    # --- carrier cost estimate --------------------------------------------------------
    for org_id, cost in await _rows(
        session,
        sa.select(
            ProviderSpendDaily.org_id,
            sa.func.coalesce(sa.func.sum(ProviderSpendDaily.cost_micros), 0),
        )
        .where(
            ProviderSpendDaily.period_date >= start,
            ProviderSpendDaily.period_date <= end,
            scope(ProviderSpendDaily.org_id),
        )
        .group_by(ProviderSpendDaily.org_id),
    ):
        out[org_id]["carrier_cost_est"] += int(cost)

    # --- Telnyx actual cost (B5 reconciliation), when the table exists ---------------
    try:
        from app.models import TelnyxCostDaily  # type: ignore[attr-defined]

        for org_id, cost in await _rows(
            session,
            sa.select(
                TelnyxCostDaily.org_id,
                sa.func.coalesce(sa.func.sum(TelnyxCostDaily.cost_micros), 0),
            )
            .where(
                TelnyxCostDaily.period_date >= start,
                TelnyxCostDaily.period_date <= end,
                TelnyxCostDaily.org_id.is_not(None),
                scope(TelnyxCostDaily.org_id),
            )
            .group_by(TelnyxCostDaily.org_id),
        ):
            out[org_id]["telnyx_actual_cost"] += int(cost)
    except ImportError:
        pass

    for m in out.values():
        finish(m)
    return out


def finish(m: dict[str, int]) -> dict[str, int]:
    """Derived fields: blocked totals, carrier cost used, cash profit."""
    m["blocked_credit"] = sum(v for k, v in m.items() if k.startswith("blocked_credit_"))
    m["blocked_total"] = m["blocked_credit"] + m["blocked_compliance"] + m["blocked_moderation"]
    m["carrier_cost"] = m["telnyx_actual_cost"] or m["carrier_cost_est"]
    m["cash_profit"] = m["paid"] - m["stripe_fees"] - m["carrier_cost"]
    m["usage_margin"] = m["usage_revenue"] - m["carrier_cost"]
    return m


async def _balances(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Newest balance_after per org, one query."""
    newest = (
        sa.select(CreditLedgerEntry.org_id, sa.func.max(CreditLedgerEntry.seq).label("seq"))
        .group_by(CreditLedgerEntry.org_id)
        .subquery()
    )
    rows = await _rows(
        session,
        sa.select(CreditLedgerEntry.org_id, CreditLedgerEntry.balance_after_micros).join(
            newest,
            sa.and_(
                newest.c.org_id == CreditLedgerEntry.org_id,
                newest.c.seq == CreditLedgerEntry.seq,
            ),
        ),
    )
    return {org_id: int(b) for org_id, b in rows}


async def _bundle_units(session: AsyncSession) -> dict[tuple[uuid.UUID, str], int]:
    newest = (
        sa.select(
            BundleLedgerEntry.org_id,
            BundleLedgerEntry.kind,
            sa.func.max(BundleLedgerEntry.seq).label("seq"),
        )
        .group_by(BundleLedgerEntry.org_id, BundleLedgerEntry.kind)
        .subquery()
    )
    rows = await _rows(
        session,
        sa.select(
            BundleLedgerEntry.org_id, BundleLedgerEntry.kind, BundleLedgerEntry.balance_after_units
        ).join(
            newest,
            sa.and_(
                newest.c.org_id == BundleLedgerEntry.org_id,
                newest.c.kind == BundleLedgerEntry.kind,
                newest.c.seq == BundleLedgerEntry.seq,
            ),
        ),
    )
    return {(o, k): int(u) for o, k, u in rows}


async def _active_numbers(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await _rows(
        session,
        sa.select(OrgNumber.org_id, sa.func.count())
        .where(OrgNumber.released_at.is_(None), OrgNumber.status == "active")
        .group_by(OrgNumber.org_id),
    )
    return {o: int(n) for o, n in rows}


async def orgs_table(session: AsyncSession, start: date | None, end: date | None) -> dict[str, Any]:
    lo, hi, s, e = day_range(start, end)
    metrics = await org_metrics(session, lo, hi, s, e)
    balances = await _balances(session)
    units = await _bundle_units(session)
    numbers = await _active_numbers(session)
    orgs = (await session.execute(sa.select(Org).order_by(Org.created_at))).scalars().all()
    rows = []
    totals: dict[str, int] = defaultdict(int)
    for org in orgs:
        m = dict(metrics.get(org.id) or finish(_blank()))
        for k, v in m.items():
            totals[k] += v
        auto = org.credit_auto_recharge or {}
        rows.append(
            {
                "org_id": str(org.id),
                "name": org.name,
                "slug": org.slug,
                "created_at": org.created_at.isoformat() if org.created_at else None,
                "prepaid": bool(org.telephony_prepaid),
                "billing_state": org.billing_state,
                "balance_micros": balances.get(org.id, 0),
                "warn_threshold_micros": int(org.warn_threshold_micros or 0),
                "avg_daily_spend_micros": int(org.avg_daily_spend_micros or 0),
                "auto_recharge": bool(auto.get("enabled")),
                "auto_recharge_failures": int(org.auto_recharge_failures or 0),
                "sms_bundle_units": units.get((org.id, "sms"), 0),
                "mms_bundle_units": units.get((org.id, "mms"), 0),
                "numbers": numbers.get(org.id, 0),
                "plan_code": org.plan_code,
                "metrics": m,
            }
        )
    finish(totals)
    return {
        "start": s.isoformat(),
        "end": e.isoformat(),
        "orgs": rows,
        "totals": dict(totals),
        "summary": {
            "orgs": len(rows),
            "prepaid_orgs": sum(1 for r in rows if r["prepaid"]),
            "low_orgs": sum(1 for r in rows if r["billing_state"] == "low"),
            "exhausted_orgs": sum(1 for r in rows if r["billing_state"] == "exhausted"),
            "balances_micros": sum(r["balance_micros"] for r in rows),
            "active_numbers": sum(r["numbers"] for r in rows),
            "sms_bundle_units_outstanding": sum(r["sms_bundle_units"] for r in rows),
            "mms_bundle_units_outstanding": sum(r["mms_bundle_units"] for r in rows),
        },
    }


async def org_detail(
    session: AsyncSession, org_id: uuid.UUID, start: date | None, end: date | None
) -> dict[str, Any] | None:
    org = await session.get(Org, org_id)
    if org is None:
        return None
    lo, hi, s, e = day_range(start, end)
    metrics = (await org_metrics(session, lo, hi, s, e, [org_id])).get(org_id) or finish(_blank())
    # Daily series: one metrics pass per day would be N queries; the window is capped.
    series = []
    day = s
    while day <= e and len(series) < 92:
        dlo = datetime.combine(day, time.min, tzinfo=timezone.utc)
        dm = (await org_metrics(session, dlo, dlo + timedelta(days=1), day, day, [org_id])).get(
            org_id
        ) or finish(_blank())
        series.append({"date": day.isoformat(), **{k: dm.get(k, 0) for k in SERIES_KEYS}})
        day += timedelta(days=1)
    ledger = await _rows(
        session,
        sa.select(CreditLedgerEntry)
        .where(CreditLedgerEntry.org_id == org_id)
        .order_by(CreditLedgerEntry.seq.desc())
        .limit(100),
    )
    payments = await _rows(
        session,
        sa.select(BillingPayment)
        .where(BillingPayment.org_id == org_id)
        .order_by(BillingPayment.created_at.desc())
        .limit(100),
    )
    refusals = await _rows(
        session,
        sa.select(BillingRefusal)
        .where(BillingRefusal.org_id == org_id)
        .order_by(BillingRefusal.created_at.desc())
        .limit(100),
    )
    numbers = await _rows(
        session,
        sa.select(OrgNumber).where(OrgNumber.org_id == org_id).order_by(OrgNumber.created_at),
    )
    balances = await _balances(session)
    units = await _bundle_units(session)
    return {
        "org": {
            "org_id": str(org.id),
            "name": org.name,
            "slug": org.slug,
            "prepaid": bool(org.telephony_prepaid),
            "billing_state": org.billing_state,
            "balance_micros": balances.get(org.id, 0),
            "warn_threshold_micros": int(org.warn_threshold_micros or 0),
            "avg_daily_spend_micros": int(org.avg_daily_spend_micros or 0),
            "auto_recharge": org.credit_auto_recharge,
            "auto_recharge_failures": int(org.auto_recharge_failures or 0),
            "sms_bundle_units": units.get((org.id, "sms"), 0),
            "mms_bundle_units": units.get((org.id, "mms"), 0),
            "telnyx_billing_group_id": org.telnyx_billing_group_id,
        },
        "start": s.isoformat(),
        "end": e.isoformat(),
        "metrics": metrics,
        "series": series,
        "ledger": [
            {
                "seq": r.seq,
                "type": r.entry_type,
                "amount_micros": int(r.amount_micros),
                "balance_after_micros": int(r.balance_after_micros),
                "reference": r.reference,
                "note": r.note,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for (r,) in ledger
        ],
        "payments": [payment_dict(r) for (r,) in payments],
        "refusals": [
            {
                "kind": r.kind,
                "reason": r.reason,
                "price_micros": int(r.price_micros),
                "balance_micros": int(r.balance_micros),
                "detail": r.detail,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for (r,) in refusals
        ],
        "numbers": [
            {
                "e164": n.e164,
                "carrier": n.carrier,
                "status": n.status,
                "rental_paid_through": n.rental_paid_through.isoformat()
                if n.rental_paid_through
                else None,
                "billing": (n.provisioning or {}).get("billing"),
            }
            for (n,) in numbers
        ],
    }


SERIES_KEYS = (
    "sms_out_segments", "sms_in_segments", "mms_out", "mms_in", "calls_out", "calls_in",
    "minutes_out", "minutes_in", "usage_revenue", "paid", "blocked_total",
)


def payment_dict(r: BillingPayment) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "org_id": str(r.org_id),
        "kind": r.kind,
        "state": r.state,
        "quantity": r.quantity,
        "list_micros": int(r.list_micros),
        "paid_micros": int(r.paid_micros),
        "discount_micros": int(r.discount_micros),
        "stripe_fee_micros": None if r.stripe_fee_micros is None else int(r.stripe_fee_micros),
        "credited_micros": int(r.credited_micros),
        "units_credited": int(r.units_credited),
        "stripe_payment_intent_id": r.stripe_payment_intent_id,
        "paid_at": r.paid_at.isoformat() if r.paid_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def payments_list(
    session: AsyncSession, start: date | None, end: date | None, limit: int = 500
) -> list[dict[str, Any]]:
    lo, hi, _s, _e = day_range(start, end)
    rows = await _rows(
        session,
        sa.select(BillingPayment, Org.name)
        .join(Org, Org.id == BillingPayment.org_id)
        .where(BillingPayment.created_at >= lo, BillingPayment.created_at < hi)
        .order_by(BillingPayment.created_at.desc())
        .limit(limit),
    )
    return [{**payment_dict(p), "org_name": name} for p, name in rows]
