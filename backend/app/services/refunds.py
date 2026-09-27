"""Refunds (user policy 2026-09-28).

Refundable: ONLY unused, paid-for account credit (credit top-ups, auto-recharges, credit
lines on custom invoices), less Stripe's processing fee on the original payment (pro rata
to the part refunded) - customers bear the card fees both ways. NOT refundable: bundle
units of any kind (used or unused), credit already spent, plans/seats for the current
period, number/porting/10DLC fees, Stripe fees.

Two ways a refund happens:

- ``refund_unused_credit`` (Ops -> workspace -> "Refund unused credit"): works out the
  refundable credit newest payment first, takes that credit back and issues each Stripe
  refund for the credit less its fee share. Nothing else is ever refunded from here.
- ``handle_charge_refunded``: a refund an operator issued straight in Stripe (outside the
  button). The UNUSED part of what that payment granted is taken back - credit grossed up
  by the fee the customer bears, bundle units pro rata (the money went back, so the units
  go too) - never below zero. Anything the refund exceeded is recorded on the payment and
  raised to ops (``refund_shortfall``).

Chargebacks stay with services/card_risk.py (hold, card ban, pause, ops alert); a credit
already held there for the same payment counts as taken back, never twice.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import BillingPayment, BundleLedgerEntry, CreditLedgerEntry

log = structlog.get_logger("refunds")

HANDLED_EVENT_TYPES = frozenset({"charge.refunded"})
_CENT = 10_000  # micros
#: Payments whose credit is refundable (bundles never are).
CREDIT_KINDS = ("topup", "auto_recharge", "invoice")


def _grant_refs(row: BillingPayment, intent_id: str) -> list[str]:
    if row.kind in ("topup", "auto_recharge"):
        return [intent_id]
    if row.kind.endswith("_bundle"):
        return [f"pi:{intent_id}"]
    if row.kind == "invoice":
        invoice_id = row.stripe_checkout_id or ""
        lines = (row.detail or {}).get("lines") or []
        return [f"invoice:{invoice_id}:{i}" for i in range(len(lines))]
    return []


async def _grants(session: AsyncSession, row: BillingPayment, intent_id: str) -> list[dict]:
    """What this payment put on the workspace: [{ref, ledger: credit|units, kind, amount}]."""
    out: list[dict] = []
    for ref in _grant_refs(row, intent_id):
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
    session: AsyncSession, org_id: uuid.UUID, grant: dict, intent_id: str, *, source: str = ""
) -> int:
    """Credit micros / units of this grant taken back so far. ``source`` "stripe" counts
    only earlier manual-Stripe refunds (the button's own refunds are accounted separately);
    "" counts every refund. A top-up's net card_risk fraud hold always counts."""
    prefix = f"refund:{grant['ref']}:{source + ':' if source else ''}"
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


def _fee_share(row: BillingPayment, credit_micros: int) -> int:
    """The customer's share of Stripe's fee on the original payment for ``credit_micros``
    of it (rounded up: the customer bears the fee)."""
    fee = int(row.stripe_fee_micros or 0)
    paid = int(row.paid_micros or 0)
    if fee <= 0 or paid <= 0:
        return 0
    return -(-fee * credit_micros // paid)


# --------------------------------------------------------------------------------------
# Ops: refund the unused credit
# --------------------------------------------------------------------------------------
async def refundable(session: AsyncSession, org_id: uuid.UUID) -> dict:
    """What "Refund unused credit" would do now: the unused paid credit (never more than the
    balance), newest payment first, each less its fee share. Read-only."""
    from app.services import credits

    set_org_context(session, org_id)
    left = max(await credits.balance(session, org_id), 0)
    rows = (
        (
            await session.execute(
                sa.select(BillingPayment)
                .where(
                    BillingPayment.org_id == org_id,
                    BillingPayment.state == "paid",
                    BillingPayment.kind.in_(CREDIT_KINDS),
                    BillingPayment.stripe_payment_intent_id.is_not(None),
                )
                .order_by(BillingPayment.paid_at.desc())
            )
        )
        .scalars()
        .all()
    )
    payments: list[dict] = []
    waiting_on_fee = 0
    for row in rows:
        if left <= 0:
            break
        intent_id = row.stripe_payment_intent_id
        grants = [g for g in await _grants(session, row, intent_id) if g["ledger"] == "credit"]
        unused_here = 0
        for grant in grants:
            unused_here += max(
                grant["amount"] - await _already_taken(session, org_id, grant, intent_id), 0
            )
        take = min(unused_here, left)
        if take <= 0:
            continue
        if row.stripe_fee_micros is None and int(row.paid_micros or 0) > 0:
            # Stripe has not settled the fee yet (payments.fee_tick fills it): not refundable
            # until we know what the customer bears.
            waiting_on_fee += take
            left -= take
            continue
        refunded_so_far = int(((row.detail or {}).get("refund") or {}).get("refunded_micros", 0))
        fee = _fee_share(row, take)
        cash = min(take - fee, int(row.paid_micros or 0) - refunded_so_far)
        cash = (max(cash, 0) // _CENT) * _CENT  # whole cents, rounded down
        if cash <= 0:
            left -= take
            continue
        payments.append(
            {
                "payment_id": str(row.id),
                "kind": row.kind,
                "intent": intent_id,
                "paid_at": row.paid_at.isoformat() if row.paid_at else None,
                "credit_micros": take,
                "fee_micros": take - cash,
                "refund_micros": cash,
            }
        )
        left -= take
    return {
        "balance_micros": await credits.balance(session, org_id),
        "credit_micros": sum(p["credit_micros"] for p in payments),
        "fee_micros": sum(p["fee_micros"] for p in payments),
        "refund_micros": sum(p["refund_micros"] for p in payments),
        "waiting_on_fee_micros": waiting_on_fee,
        "payments": payments,
    }


async def _stripe_refund(settings, intent_id: str, cents: int, *, key: str, org_id) -> dict:  # noqa: ANN001
    from app.services import stripe_client

    stripe = stripe_client._stripe(settings)
    return await stripe_client._run_sync(
        stripe.Refund.create,
        payment_intent=intent_id,
        amount=int(cents),
        reason="requested_by_customer",
        metadata={"kind": "refund_unused", "org_id": str(org_id)},
        idempotency_key=key,
    )


async def refund_unused_credit(
    session: AsyncSession, settings, org_id: uuid.UUID, *, actor_user_id: uuid.UUID | None  # noqa: ANN001
) -> dict:
    """Refund the workspace's unused paid credit, less card fees (see ``refundable``). Each
    payment is its own step: take its credit back, refund the card, commit - so a Stripe
    failure part-way stops cleanly with the earlier refunds recorded. Returns what was done."""
    from app.services import credits

    plan = await refundable(session, org_id)
    if not plan["payments"]:
        raise ValidationFailedError("There is no unused paid credit to refund")
    op_id = uuid.uuid4().hex[:12]
    done: list[dict] = []
    for item in plan["payments"]:
        row = await session.get(BillingPayment, uuid.UUID(item["payment_id"]))
        intent_id = item["intent"]
        remaining = item["credit_micros"]
        taken_by_ref: list[tuple[str, int]] = []
        for grant in [g for g in await _grants(session, row, intent_id) if g["ledger"] == "credit"]:
            unused = max(
                grant["amount"] - await _already_taken(session, org_id, grant, intent_id), 0
            )
            take = min(unused, remaining)
            if take <= 0:
                continue
            await credits.adjust(
                session,
                org_id,
                -take,
                reference=f"refund:{grant['ref']}:ops:{op_id}"[:120],
                note=f"Refund of unused credit (payment {intent_id}), less card fees",
                created_by=actor_user_id,
                entry_type="refund",
            )
            remaining -= take
            taken_by_ref.append((grant["ref"], take))
        cents = item["refund_micros"] // _CENT
        # Recorded BEFORE Stripe is called: its charge.refunded webhook can arrive before we
        # return, and must already see this part as the button's (not a manual refund).
        before = dict((row.detail or {}).get("refund") or {})
        report = dict(before)
        report["ops_refunded_micros"] = int(report.get("ops_refunded_micros", 0)) + cents * _CENT
        report["refunded_micros"] = int(report.get("refunded_micros", 0)) + cents * _CENT
        for key, add in (
            ("fees_withheld_micros", item["fee_micros"]),
            ("taken_credit_micros", item["credit_micros"]),
        ):
            report[key] = int(report.get(key, 0)) + add
        row.detail = {**(row.detail or {}), "refund": report}
        await session.commit()
        try:
            await _stripe_refund(
                settings, intent_id, cents, key=f"refund-unused-{op_id}-{intent_id}", org_id=org_id
            )
        except Exception as exc:  # noqa: BLE001 - undo this step, report, stop
            log.warning("refunds.stripe_refund_failed", intent=intent_id, error=str(exc)[:200])
            set_org_context(session, org_id)
            row = await session.get(BillingPayment, uuid.UUID(item["payment_id"]))
            for ref, take in taken_by_ref:
                # Same "refund:<ref>:" prefix, so the grant counts as unused again.
                await credits.adjust(
                    session,
                    org_id,
                    take,
                    reference=f"refund:{ref}:ops-undo:{op_id}"[:120],
                    note="Card refund failed: credit given back",
                    created_by=actor_user_id,
                    entry_type="refund",
                )
            row.detail = {**(row.detail or {}), "refund": before}
            await session.commit()
            return {
                "refunded": done,
                "failed": {"payment_id": item["payment_id"], "error": str(exc)[:200]},
            }
        done.append(item)
    return {"refunded": done, "failed": None}


# --------------------------------------------------------------------------------------
# Stripe webhook: a refund issued straight in Stripe
# --------------------------------------------------------------------------------------
async def handle_charge_refunded(session: AsyncSession, event: dict) -> bool:
    """Apply one ``charge.refunded``. Stripe sends the CUMULATIVE ``amount_refunded``; the
    part the Ops button already refunded (and took back itself) is left out, so only a
    manual Stripe refund takes anything back here. Returns False when the charge is not
    one of our recorded payments. Does not commit."""
    from app.services import bundles, card_risk, credits, payments

    charge = (event.get("data") or {}).get("object") or {}
    intent_id = str(charge.get("payment_intent") or "")
    amount = int(charge.get("amount") or 0)
    refunded = min(int(charge.get("amount_refunded") or 0), amount)
    if not intent_id or amount <= 0 or refunded <= 0:
        return False
    row = await payments._by_intent(session, intent_id)
    if row is None:
        log.warning("refunds.unknown_payment", intent=intent_id)
        return False
    org_id = row.org_id
    set_org_context(session, org_id)

    report = dict((row.detail or {}).get("refund") or {})
    manual = refunded - int(report.get("ops_refunded_micros", 0)) // _CENT
    fee_cents = int(row.stripe_fee_micros or 0) // _CENT
    shortfall: dict[str, int] = {}
    if manual > 0:
        for grant in await _grants(session, row, intent_id):
            if grant["ledger"] == "credit":
                # The customer bears the card fee: $28.95 back on a $50 top-up ($1.75 fee)
                # is $30 of credit. Floor, never more than was granted.
                base = max(amount - fee_cents, 1)
                target = min(grant["amount"], grant["amount"] * manual // base)
            else:
                target = grant["amount"] * manual // amount
            wanted = target - await _already_taken(
                session, org_id, grant, intent_id, source="stripe"
            )
            if wanted <= 0:
                continue
            reference = f"refund:{grant['ref']}:stripe:{refunded}"[:120]
            note = f"Refund of payment {intent_id} in Stripe: unused part taken back"
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
