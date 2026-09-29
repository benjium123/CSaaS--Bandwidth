"""Card payments as the customer paid them (billing_payments): top-ups, auto-recharges,
message bundles and paid plan invoices, with list price, discount and Stripe fee - the admin
console's revenue.

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

BUNDLE_METADATA_KINDS: dict[str, str] = {
    "sms_bundle": "sms", "mms_bundle": "mms", "voice_bundle": "voice"
}
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
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    kind: str,
    qty: int,
    payment_id: uuid.UUID | None = None,
) -> tuple[BillingPayment, dict[str, int]]:
    """Price the purchase and create its pending row (before Stripe is called). Does not
    commit. ``payment_id`` lets a caller make the row id deterministic (in-app retries)."""
    q = await bundles.quote(session, kind, qty, org_id)
    set_org_context(session, org_id)
    row = BillingPayment(
        id=payment_id or uuid.uuid4(),
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


#: Radar risk score (0-99) above which a credit/bundle payment is refunded, not credited.
RISK_SCORE_BLOCK = 75


def _risk_reason(risk: dict | None) -> str | None:
    if not risk:
        return None
    if risk.get("cvc_check") == "fail":
        return "card security code (CVC) check failed"
    score = risk.get("risk_score")
    if isinstance(score, int) and score > RISK_SCORE_BLOCK:
        return f"Stripe risk score {score} is above {RISK_SCORE_BLOCK}"
    return None


async def refuse_risky_payment(session: AsyncSession, settings, intent: dict) -> bool:  # noqa: ANN001
    """Our stand-in for the Radar rules Stripe offers no API for: before a top-up, bundle or
    auto-recharge payment becomes credit, refund it (and credit nothing) when the card's CVC
    check failed or Radar's risk score is above RISK_SCORE_BLOCK. Returns True when refused.
    Fails open (returns False) when Stripe is not configured or cannot be read: Radar's
    own default blocking still applies to the charge. Commits when it refuses."""
    from app.models import SecurityAlert
    from app.services import audit as audit_svc
    from app.services import stripe_client

    metadata = intent.get("metadata") or {}
    kind = str(metadata.get("kind") or "")
    if kind != "credit_topup" and kind not in BUNDLE_METADATA_KINDS:
        return False
    intent_id = str(intent.get("id") or "")
    if not intent_id or not stripe_client.is_configured(settings):
        return False
    try:
        org_id = uuid.UUID(str(metadata.get("org_id")))
    except (TypeError, ValueError):
        return False
    existing = await _by_intent(session, intent_id)
    if existing is not None and existing.state == "refunded":
        return True  # a replay of a payment already refused
    if existing is not None and existing.state == "paid":
        return False  # already credited before this screen existed; leave it
    try:
        reason = _risk_reason(await stripe_client.charge_risk(settings, intent_id))
    except Exception:
        log.warning("payments.risk_lookup_failed", intent_id=intent_id)
        return False
    if reason is None:
        return False

    await stripe_client.refund_fraudulent(settings, intent_id, reason=reason)
    org = await session.get(Org, org_id)
    if org is None:
        log.error("payments.risk_refund_unknown_org", intent_id=intent_id)
        return True
    set_org_context(session, org_id)
    amount = int(intent.get("amount_received") or 0) * 10_000
    row = existing
    if row is None:
        payment_id = metadata.get("payment_id")
        if payment_id:
            try:
                row = await session.get(BillingPayment, uuid.UUID(str(payment_id)))
            except ValueError:
                row = None
    if row is None:
        bundle_kind = BUNDLE_METADATA_KINDS.get(kind)
        row = BillingPayment(
            id=uuid.uuid4(),
            org_id=org_id,
            kind=(
                f"{bundle_kind}_bundle"
                if bundle_kind
                else ("auto_recharge" if metadata.get("source") == "auto_recharge" else "topup")
            ),
            quantity=int(metadata.get("qty") or 1),
            list_micros=amount,
            discount_micros=0,
        )
        session.add(row)
    row.state = "refunded"
    row.stripe_payment_intent_id = intent_id
    row.paid_micros = amount
    row.credited_micros = 0
    row.units_credited = 0
    if metadata.get("source") == "auto_recharge":
        # A refused auto-recharge would be retried (and refunded, and pay Stripe's fee)
        # every few hours: switch it off until the customer turns it back on.
        auto = dict(org.credit_auto_recharge or {})
        auto["enabled"] = False
        auto.pop("pending_intent", None)
        auto["last_failure"] = f"Refused for fraud risk: {reason}"
        org.credit_auto_recharge = auto
    # Staff are told about every automatic refund (review only - it is already refunded).
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="payment_auto_refunded",
            org_id=org_id,
            detail={
                "intent": intent_id,
                "amount_micros": amount,
                "kind": kind,
                "reason": reason,
                "action": "Refunded automatically at checkout; nothing was credited.",
            },
        )
    )
    audit_svc.record(
        session,
        org_id,
        action="payment.refused_risk",
        target_type="payment",
        target_id=intent_id,
        detail={"reason": reason, "amount_micros": amount, "kind": kind},
    )
    await session.commit()
    log.error("payments.refused_risk", org_id=str(org_id), intent_id=intent_id, reason=reason)
    return True


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
    if row is not None and row.paid_micros and row.paid_micros != paid:
        log.error(
            "bundle_intent_amount_mismatch",
            intent_id=intent_id,
            expected_micros=row.paid_micros,
            received_micros=paid,
        )
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


#: Subscription metadata kind -> billing_payments.kind for the recurring invoices Stripe
#: bills (plan, users and extra numbers; the retired per-number carts). 10DLC fees are not
#: here: their refunds would need recording too.
SUBSCRIPTION_INVOICE_KINDS: dict[str, str] = {
    "workspace_plan": "plan",
    "number_purchase": "numbers",
}
_CENT = 10_000  # micros


def _invoice_subscription(invoice: dict) -> tuple[str | None, dict]:
    """(subscription id, subscription metadata) from an invoice, in both Stripe shapes:
    ``parent.subscription_details`` (2025+) and the older top-level fields."""
    details = ((invoice.get("parent") or {}).get("subscription_details")) or {}
    sub = details.get("subscription") or invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    metadata = details.get("metadata") or (
        (invoice.get("subscription_details") or {}).get("metadata")
    ) or {}
    return (str(sub) if sub else None), dict(metadata)


def _invoice_intent(invoice: dict) -> str | None:
    intent = invoice.get("payment_intent")
    if intent is None:
        for item in ((invoice.get("payments") or {}).get("data")) or []:
            intent = ((item or {}).get("payment") or {}).get("payment_intent")
            if intent:
                break
    if isinstance(intent, dict):
        intent = intent.get("id")
    return str(intent) if intent else None


async def record_subscription_invoice(session: AsyncSession, invoice: dict) -> bool:
    """``invoice.paid`` for a plan (or per-number) subscription -> one paid billing_payments
    row, so the console counts subscription revenue and what the workspace's coupons took
    off it. ``list`` = paid + discount. Idempotent on the invoice id (kept in
    ``stripe_checkout_id``: these rows have no Checkout Session of their own). Returns False
    for invoices that are not ours to record. Does not commit."""
    invoice_id = invoice.get("id")
    sub_id, metadata = _invoice_subscription(invoice)
    kind = SUBSCRIPTION_INVOICE_KINDS.get(str(metadata.get("kind") or ""))
    if not invoice_id or not sub_id or kind is None:
        return False
    try:
        org_id = uuid.UUID(str(metadata.get("org_id")))
    except ValueError:
        log.warning("payments.invoice_without_org", invoice_id=invoice_id)
        return False
    paid = int(invoice.get("amount_paid") or 0) * _CENT
    discount = sum(
        int((d or {}).get("amount") or 0) for d in invoice.get("total_discount_amounts") or []
    ) * _CENT
    if paid <= 0 and discount <= 0:
        return True  # a $0 invoice with nothing given away (e.g. a trial): nothing to count
    existing = (
        await session.execute(
            sa.select(BillingPayment.id)
            .where(BillingPayment.stripe_checkout_id == str(invoice_id))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if existing is not None:
        return True
    paid_at = (invoice.get("status_transitions") or {}).get("paid_at")
    intent = _invoice_intent(invoice)
    if intent is not None and await _by_intent(session, intent) is not None:
        intent = None  # never collide with a row that already owns this intent
    set_org_context(session, org_id)
    session.add(
        BillingPayment(
            id=uuid.uuid4(),
            org_id=org_id,
            kind=kind,
            state="paid",
            stripe_checkout_id=str(invoice_id),
            stripe_payment_intent_id=intent,
            quantity=1,
            list_micros=paid + discount,
            paid_micros=paid,
            discount_micros=discount,
            # A fully discounted invoice has no charge, so no Stripe fee to look up.
            stripe_fee_micros=0 if paid <= 0 else None,
            paid_at=(
                datetime.fromtimestamp(int(paid_at), timezone.utc) if paid_at else _now()
            ),
            detail={
                "invoice_id": str(invoice_id),
                "subscription_id": sub_id,
                "billing_reason": invoice.get("billing_reason"),
            },
        )
    )
    await session.flush()
    return True


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



#: Auto-recharge a workspace gets after its first top-up (the customer can turn it off):
#: below $5, charge $10 to the card that paid.
DEFAULT_AUTO_RECHARGE_THRESHOLD_MICROS = 5_000_000
DEFAULT_AUTO_RECHARGE_AMOUNT_MICROS = 10_000_000


async def save_topup_card(
    session: AsyncSession, settings, org: Org, intent: dict  # noqa: ANN001
) -> None:
    """After a paid Checkout top-up: keep the card it was paid with (saved on the workspace's
    Stripe customer by setup_future_usage) as a payment method, and on the workspace's first
    paid top-up switch auto-recharge on with the defaults unless it was already configured.
    The same checks as adding a card in Billing apply (ban list, card risk); a refused card
    is detached and nothing is saved.
    Never raises: the credit was already granted. Does not commit."""
    from app.errors import PermissionDeniedError
    from app.models import PaymentMethod
    from app.services import audit as audit_svc
    from app.services import ban_list, card_risk, stripe_client

    pm_id = intent.get("payment_method")
    customer_id = intent.get("customer")
    if not (isinstance(pm_id, str) and pm_id and isinstance(customer_id, str) and customer_id):
        return
    try:
        async with session.begin_nested():
            set_org_context(session, org.id)
            rows = (
                await session.execute(
                    sa.select(PaymentMethod).where(PaymentMethod.org_id == org.id)
                )
            ).scalars().all()
            pm = next((r for r in rows if r.stripe_payment_method_id == pm_id), None)
            if pm is None:
                card = await stripe_client.retrieve_payment_method(settings, pm_id)
                fingerprint = card.get("fingerprint")
                refused = bool(fingerprint) and bool(
                    await ban_list.matches(
                        session, [ban_list.identifier("card_fingerprint", fingerprint)]
                    )
                )
                if not refused:
                    try:
                        await card_risk.check_new_card(
                            session,
                            org.id,
                            fingerprint=fingerprint,
                            card_country=card.get("country"),
                        )
                    except PermissionDeniedError:
                        refused = True
                if refused:
                    log.warning("payments.topup_card_refused", org_id=str(org.id))
                    try:
                        await stripe_client.detach_payment_method(settings, payment_method_id=pm_id)
                    except Exception:  # noqa: BLE001 - refusing the card matters more
                        pass
                    return
                pm = PaymentMethod(
                    id=uuid.uuid4(),
                    org_id=org.id,
                    stripe_customer_id=customer_id,
                    stripe_payment_method_id=pm_id,
                    brand=card.get("brand", ""),
                    last4=card.get("last4", ""),
                    is_default=not rows,
                    card_fingerprint=fingerprint,
                )
                session.add(pm)
                await session.flush()
            # Only on the FIRST paid top-up: a customer who later turned auto-recharge off
            # must not have it switched back on by their next top-up.
            earlier = (
                await session.execute(
                    sa.select(sa.func.count(BillingPayment.id)).where(
                        BillingPayment.org_id == org.id,
                        BillingPayment.state == "paid",
                        BillingPayment.kind.in_(("topup", "auto_recharge")),
                        BillingPayment.stripe_payment_intent_id != str(intent.get("id") or ""),
                    )
                )
            ).scalar_one()
            configured = bool((org.credit_auto_recharge or {}).get("threshold_micros"))
            if earlier == 0 and not configured:
                org.credit_auto_recharge = {
                    "enabled": True,
                    "threshold_micros": DEFAULT_AUTO_RECHARGE_THRESHOLD_MICROS,
                    "amount_micros": DEFAULT_AUTO_RECHARGE_AMOUNT_MICROS,
                    "payment_method_id": str(pm.id),
                }
                org.auto_recharge_failures = 0
                audit_svc.record(
                    session,
                    org.id,
                    action="billing.auto_recharge_updated",
                    target_type="org",
                    target_id=str(org.id),
                    detail={"enabled": True, "source": "first_topup_default",
                            "credit_auto_recharge": org.credit_auto_recharge},
                )
    except Exception:
        log.exception("payments.save_topup_card_failed", org_id=str(org.id))
