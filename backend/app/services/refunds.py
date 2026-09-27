"""Refunds (user policy 2026-09-28, the telecom norm): used minutes, messages and credit are
never refundable. A refund an operator issues in Stripe takes back the UNUSED part of what
that payment granted, in proportion to the amount refunded:

- credit top-ups / auto-recharges: the balance credit (ledger ``topup`` row, reference =
  the payment intent id)
- bundles: the units (bundle ``purchase`` row, reference ``pi:<intent>``)
- custom invoices: every line's credit and package units (``invoice:<id>:<line>``)

Nothing is taken below zero. When the refund is bigger than what is left unused, the
difference is recorded on the payment and raised to ops (``refund_shortfall``) - the money
already went back to the card, so a person decides what happens next. Plan and number
subscriptions grant no balance: their refunds only mark the payment.

Chargebacks stay with services/card_risk.py (hold, card ban, pause, ops alert); a credit
already held there for the same payment is counted as taken back here, never twice.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import BillingPayment, BundleLedgerEntry, CreditLedgerEntry

log = structlog.get_logger("refunds")

HANDLED_EVENT_TYPES = frozenset({"charge.refunded"})
_CENT = 10_000  # micros


async def _grants(session: AsyncSession, row: BillingPayment, intent_id: str) -> list[dict]:
    """What this payment put on the workspace: [{ref, ledger: credit|units, kind, amount}]."""
    if row.kind in ("topup", "auto_recharge"):
        refs = [intent_id]
    elif row.kind.endswith("_bundle"):
        refs = [f"pi:{intent_id}"]
    elif row.kind == "invoice":
        invoice_id = row.stripe_checkout_id or ""
        lines = (row.detail or {}).get("lines") or []
        refs = [f"invoice:{invoice_id}:{i}" for i in range(len(lines))]
    else:
        return []
    out: list[dict] = []
    for ref in refs:
        credit = (
            await session.execute(
                sa.select(CreditLedgerEntry.amount_micros).where(
                    CreditLedgerEntry.org_id == row.org_id,
                    CreditLedgerEntry.entry_type == "topup",
                    CreditLedgerEntry.reference == ref,
                )
            )
        ).scalar_one_or_none()
        if credit:
            out.append({"ref": ref, "ledger": "credit", "kind": None, "amount": int(credit)})
        units = (
            await session.execute(
                sa.select(BundleLedgerEntry.kind, BundleLedgerEntry.delta_units).where(
                    BundleLedgerEntry.org_id == row.org_id,
                    BundleLedgerEntry.entry_type == "purchase",
                    BundleLedgerEntry.reference == ref,
                )
            )
        ).all()
        for kind, delta in units:
            out.append({"ref": ref, "ledger": "units", "kind": kind, "amount": int(delta)})
    return out


async def _already_taken(
    session: AsyncSession, org_id: uuid.UUID, grant: dict, intent_id: str
) -> int:
    """Credit micros / units of this grant taken back so far (earlier partial refunds, and
    for a top-up the net fraud hold card_risk placed on the same payment)."""
    prefix = f"refund:{grant['ref']}:"
    if grant["ledger"] == "credit":
        refs = [CreditLedgerEntry.reference.like(prefix + "%")]
        if grant["ref"] == intent_id:
            refs += [
                CreditLedgerEntry.reference == f"fraud:{intent_id}"[:120],
                CreditLedgerEntry.reference == f"fraud-release:{intent_id}"[:120],
            ]
        total = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(CreditLedgerEntry.amount_micros), 0)).where(
                    CreditLedgerEntry.org_id == org_id, sa.or_(*refs)
                )
            )
        ).scalar_one()
    else:
        total = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(BundleLedgerEntry.delta_units), 0)).where(
                    BundleLedgerEntry.org_id == org_id,
                    BundleLedgerEntry.kind == grant["kind"],
                    BundleLedgerEntry.entry_type == "refund",
                    BundleLedgerEntry.reference.like(prefix + "%"),
                )
            )
        ).scalar_one()
    return max(-int(total), 0)


async def handle_charge_refunded(session: AsyncSession, event: dict) -> bool:
    """Apply one ``charge.refunded`` (Stripe sends the CUMULATIVE ``amount_refunded``, so a
    second partial refund takes back only the new share). Returns False when the charge is
    not one of our recorded payments. Does not commit."""
    from app.services import bundles, card_risk, credits

    charge = (event.get("data") or {}).get("object") or {}
    intent_id = str(charge.get("payment_intent") or "")
    amount = int(charge.get("amount") or 0)
    refunded = min(int(charge.get("amount_refunded") or 0), amount)
    if not intent_id or amount <= 0 or refunded <= 0:
        return False
    from app.services import payments

    row = await payments._by_intent(session, intent_id)
    if row is None:
        log.warning("refunds.unknown_payment", intent=intent_id)
        return False
    org_id = row.org_id
    set_org_context(session, org_id)

    report = dict((row.detail or {}).get("refund") or {})
    shortfall: dict[str, int] = {}
    for grant in await _grants(session, row, intent_id):
        # Floor: rounding favours the customer, as discounts do.
        target = grant["amount"] * refunded // amount
        wanted = target - await _already_taken(session, org_id, grant, intent_id)
        if wanted <= 0:
            continue
        reference = f"refund:{grant['ref']}:{refunded}"[:120]
        note = f"Refund of payment {intent_id}: unused part taken back"
        if grant["ledger"] == "credit":
            taken = min(wanted, max(await credits.balance(session, org_id), 0))
            if taken > 0:
                await credits.adjust(
                    session,
                    org_id,
                    -taken,
                    reference=reference,
                    note=note,
                    created_by=None,
                    entry_type="refund",
                )
            key = "credit_micros"
        else:
            taken = await bundles.claw_back(
                session, org_id, grant["kind"], wanted, reference=reference, note=note
            )
            key = f"{grant['kind']}_units"
        report[f"taken_{key}"] = int(report.get(f"taken_{key}", 0)) + taken
        if wanted > taken:
            shortfall[key] = shortfall.get(key, 0) + wanted - taken

    report["refunded_micros"] = refunded * _CENT
    for key, value in shortfall.items():
        report[f"shortfall_{key}"] = int(report.get(f"shortfall_{key}", 0)) + value
    row.detail = {**(row.detail or {}), "refund": report}
    if refunded >= amount:
        row.state = "refunded"
    if shortfall:
        # The refund was bigger than what was left unused: used service went back to the
        # card. Not reversible from here - ops decides (invoice, write-off, review).
        await card_risk.open_alert(
            session,
            org_id,
            "refund_shortfall",
            {
                "intent": intent_id,
                "payment_id": str(row.id),
                "refunded_micros": refunded * _CENT,
                "shortfall": shortfall,
            },
        )
    await session.flush()
    return True
