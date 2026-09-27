"""Custom invoices an operator sends a workspace (Ops -> Console): packages, balance credit,
free-text items and a discount line, charged to the workspace's card on file as a real
Stripe Invoice.

Stored as one ``billing_payments`` row of kind ``invoice`` (the invoice id in
``stripe_checkout_id``), so the console P&L counts it like any other payment and the
customer's Billing page lists it. Nothing on the invoice is granted until Stripe says it is
paid: ``apply_paid`` credits packages (``bundles.credit``) and balance (``credits.topup``),
each idempotent on ``invoice:<id>:<line>``, so the synchronous pay result and the
``invoice.paid`` webhook can both run it safely.

Money-owned.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import BillingPayment, Org, OrgMembership, PaymentMethod, Role, User
from app.models.subscriptions import Subscription
from app.models.billing_v2 import BUNDLE_KINDS
from app.services import bundles, credits

log = structlog.get_logger("custom_invoices")

KIND = "invoice"
METADATA_KIND = "custom_invoice"
LINE_TYPES = ("package", "credit", "item", "discount")
COLLECTIONS = ("charge_card", "email_link")
MAX_LINES = 20
MAX_LINE_CENTS = 10_000_000  # $100k
_CENT = 10_000  # micros
PACKAGE_LABELS = {"sms": "SMS", "mms": "MMS", "voice": "call-minute"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def price_lines(session: AsyncSession, lines: list[dict]) -> list[dict]:
    """Validate and price the operator's lines. Returns normalized lines, each with
    ``amount_cents`` (negative for the discount) and a customer-facing ``description``.
    A package without an amount is priced at list (volume and workspace discounts are
    not applied: the operator gives any discount as its own line)."""
    if not lines:
        raise ValidationFailedError("Add at least one line")
    if len(lines) > MAX_LINES:
        raise ValidationFailedError(f"At most {MAX_LINES} lines")
    out: list[dict] = []
    discounts = [ln for ln in lines if ln.get("type") == "discount"]
    if len(discounts) > 1:
        raise ValidationFailedError("Only one discount line")
    for ln in lines:
        kind = ln.get("type")
        if kind not in LINE_TYPES:
            raise ValidationFailedError(f"Unknown line type: {kind}")
        if kind == "discount":
            continue
        cents = ln.get("amount_cents")
        if kind == "package":
            pkg = ln.get("package")
            if pkg not in BUNDLE_KINDS:
                raise ValidationFailedError(f"Unknown package: {pkg}")
            qty = int(ln.get("quantity") or 0)
            if not 1 <= qty <= bundles.MAX_QTY:
                raise ValidationFailedError(f"Choose between 1 and {bundles.MAX_QTY} bundles")
            units = bundles.UNITS_PER_BUNDLE[pkg] * qty
            if cents is None:
                cents = await bundles.bundle_list_price(session, pkg) * qty // _CENT
            desc = (ln.get("description") or "").strip() or (
                f"{units:,} {PACKAGE_LABELS[pkg]} units ({qty} bundle{'s' if qty > 1 else ''})"
            )
            line = {"type": kind, "package": pkg, "quantity": qty, "units": units}
        elif kind == "credit":
            desc = (ln.get("description") or "").strip() or "Account balance credit"
            line = {"type": kind}
        else:
            desc = (ln.get("description") or "").strip()
            if not desc:
                raise ValidationFailedError("A custom item needs a description")
            line = {"type": kind}
        cents = int(cents if cents is not None else 0)
        if not 0 < cents <= MAX_LINE_CENTS and not (kind == "package" and cents == 0):
            raise ValidationFailedError("Each line needs an amount above $0")
        if kind == "credit" and cents <= 0:
            raise ValidationFailedError("A balance credit must be above $0")
        out.append({**line, "description": desc[:200], "amount_cents": cents})
    if not out:
        raise ValidationFailedError("Add at least one item besides the discount")
    gross = sum(ln["amount_cents"] for ln in out)
    if discounts:
        d = discounts[0]
        if d.get("percent") is not None:
            pct = float(d["percent"])
            if not 0 < pct <= 100:
                raise ValidationFailedError("A discount must be between 0% and 100%")
            off = gross * round(pct * 100) // 10_000
            label = f"Discount ({pct:g}%)"
        else:
            off = int(d.get("amount_cents") or 0)
            if off <= 0:
                raise ValidationFailedError("A discount needs a percent or an amount")
            label = "Discount"
        if off > gross:
            raise ValidationFailedError("The discount is larger than the invoice")
        if off > 0:
            desc = (d.get("description") or "").strip() or label
            out.append({"type": "discount", "description": desc[:200], "amount_cents": -off})
    return out


def totals(lines: list[dict]) -> dict[str, int]:
    """{list, discount, paid} in micros."""
    gross = sum(ln["amount_cents"] for ln in lines if ln["amount_cents"] > 0)
    off = -sum(ln["amount_cents"] for ln in lines if ln["amount_cents"] < 0)
    return {"list": gross * _CENT, "discount": off * _CENT, "paid": (gross - off) * _CENT}


async def _card(session: AsyncSession, org_id: uuid.UUID) -> PaymentMethod:
    set_org_context(session, org_id)
    pm = (
        await session.execute(
            sa.select(PaymentMethod)
            .where(PaymentMethod.org_id == org_id)
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if pm is None:
        raise ValidationFailedError(
            "This workspace has no card on file, so the invoice cannot be charged"
        )
    return pm


def _card_error(exc: Exception) -> str | None:
    """A declined card is a normal outcome; the SDK's CardError is duck-typed on ``code``
    (as in stripe_client.charge_off_session)."""
    code = getattr(exc, "code", "") or ""
    if not code:
        return None
    return getattr(exc, "user_message", None) or str(code).replace("_", " ")


async def owner_emails(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    set_org_context(session, org_id)
    return list(
        (
            await session.execute(
                sa.select(User.email)
                .join(OrgMembership, OrgMembership.user_id == User.id)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id == org_id, Role.name == "owner")
                .order_by(User.email)
            )
        ).scalars()
    )


async def _customer_for_link(
    session: AsyncSession, settings, org: Org, email: str  # noqa: ANN001
) -> str:
    """The workspace's Stripe customer (from its card or plan subscription, else a new
    one), with ``email`` as its billing email so Stripe mails the invoice there."""
    from app.services import stripe_client

    set_org_context(session, org.id)
    existing = (
        await session.execute(
            sa.select(PaymentMethod.stripe_customer_id)
            .where(PaymentMethod.org_id == org.id)
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none() or (
        await session.execute(
            sa.select(Subscription.stripe_customer_id)
            .where(Subscription.org_id == org.id, Subscription.stripe_customer_id.is_not(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if not existing:
        return await stripe_client.ensure_customer(settings, org=org, email=email)
    stripe = stripe_client._stripe(settings)
    customer = await stripe_client._run_sync(stripe.Customer.retrieve, existing)
    if (customer.get("email") or "").lower() != email.lower():
        await stripe_client._run_sync(stripe.Customer.modify, existing, email=email)
    return existing


async def create_and_charge(
    session: AsyncSession,
    settings,  # noqa: ANN001
    org: Org,
    *,
    lines: list[dict],
    memo: str | None,
    actor_user_id: uuid.UUID | None,
    collection: str = "charge_card",
    email: str | None = None,
    days_until_due: int = 7,
) -> BillingPayment:
    """Price the lines and create the Stripe invoice. ``charge_card``: charged to the
    workspace's card on file right away. ``email_link``: Stripe emails it (to one of the
    workspace's owners) with a pay link and it stays open until paid or voided.
    Commits (the pending row before Stripe is called, then the outcome)."""
    from app.services import stripe_client

    if collection not in COLLECTIONS:
        raise ValidationFailedError(f"Unknown collection: {collection}")
    priced = await price_lines(session, lines)
    t = totals(priced)
    pm = None
    if collection == "charge_card":
        pm = await _card(session, org.id)
    else:
        owners = await owner_emails(session, org.id)
        email = (email or (owners[0] if owners else "")).strip()
        if email.lower() not in {o.lower() for o in owners}:
            raise ValidationFailedError("A pay link can only be emailed to a workspace owner")
        if not 1 <= days_until_due <= 60:
            raise ValidationFailedError("Due in 1 to 60 days")
    set_org_context(session, org.id)
    row = BillingPayment(
        id=uuid.uuid4(),
        org_id=org.id,
        kind=KIND,
        state="pending",
        quantity=1,
        list_micros=t["list"],
        paid_micros=t["paid"],
        discount_micros=t["discount"],
        detail={
            "lines": priced,
            "memo": (memo or "").strip() or None,
            "created_by": str(actor_user_id) if actor_user_id else None,
            "collection": collection,
            "emailed_to": email if collection == "email_link" else None,
        },
    )
    session.add(row)
    await session.commit()

    stripe = stripe_client._stripe(settings)
    currency = settings.stripe_price_currency
    metadata = {"kind": METADATA_KIND, "org_id": str(org.id), "payment_id": str(row.id)}
    try:
        if pm is not None:
            customer = pm.stripe_customer_id
            how = {
                "collection_method": "charge_automatically",
                "default_payment_method": pm.stripe_payment_method_id,
            }
        else:
            customer = await _customer_for_link(session, settings, org, email)
            how = {"collection_method": "send_invoice", "days_until_due": days_until_due}
        invoice = await stripe_client._run_sync(
            stripe.Invoice.create,
            customer=customer,
            auto_advance=False,
            **how,
            currency=currency,
            description=row.detail["memo"],
            pending_invoice_items_behavior="exclude",
            metadata=metadata,
            idempotency_key=f"custom-invoice-{row.id}",
        )
        for i, ln in enumerate(priced):
            await stripe_client._run_sync(
                stripe.InvoiceItem.create,
                customer=customer,
                invoice=invoice["id"],
                amount=ln["amount_cents"],
                currency=currency,
                description=ln["description"],
                metadata=metadata,
                idempotency_key=f"custom-invoice-{row.id}-line-{i}",
            )
        invoice = await stripe_client._run_sync(stripe.Invoice.finalize_invoice, invoice["id"])
        if pm is None and invoice.get("status") != "paid":
            invoice = await stripe_client._run_sync(stripe.Invoice.send_invoice, invoice["id"])
    except Exception:
        log.error("custom_invoice.create_failed", payment_id=str(row.id), exc_info=True)
        row.state = "failed"
        row.detail = {**row.detail, "error": "Stripe could not create the invoice"}
        await session.commit()
        return row
    row.stripe_checkout_id = invoice["id"]
    row.detail = {
        **row.detail,
        "invoice_id": invoice["id"],
        "number": invoice.get("number"),
        "hosted_invoice_url": invoice.get("hosted_invoice_url"),
        "invoice_pdf": invoice.get("invoice_pdf"),
    }
    await session.commit()
    if pm is None:
        # Emailed: open until the customer pays (invoice.paid webhook) or it is voided. A
        # fully discounted ($0) invoice is paid at finalize.
        if invoice.get("status") == "paid":
            await apply_paid(session, invoice)
            await session.commit()
            await session.refresh(row)
        return row
    return await charge(session, settings, row, invoice=invoice)


async def charge(
    session: AsyncSession,
    settings,  # noqa: ANN001
    row: BillingPayment,
    *,
    invoice: dict | None = None,
) -> BillingPayment:
    """Pay a finalized open invoice with the card on file (first attempt or a retry).
    Commits."""
    from app.services import stripe_client

    if row.state == "paid":
        return row
    invoice_id = row.stripe_checkout_id
    if not invoice_id:
        raise ConflictError("This invoice was never created on Stripe")
    stripe = stripe_client._stripe(settings)
    if invoice is None or invoice.get("status") != "paid":
        try:
            invoice = await stripe_client._run_sync(stripe.Invoice.pay, invoice_id)
        except Exception as exc:
            reason = _card_error(exc)
            if reason is None:
                log.error("custom_invoice.pay_failed", invoice_id=invoice_id, exc_info=True)
                reason = "Stripe could not charge the invoice"
            row.state = "failed"
            row.detail = {**(row.detail or {}), "error": reason}
            await session.commit()
            return row
    if invoice.get("status") == "paid":
        await apply_paid(session, invoice)
        await session.commit()
        await session.refresh(row)
    return row


async def _by_invoice(session: AsyncSession, invoice: dict) -> BillingPayment | None:
    invoice_id = str(invoice.get("id") or "")
    payment_id = (invoice.get("metadata") or {}).get("payment_id")
    stmt = sa.select(BillingPayment).where(BillingPayment.kind == KIND)
    if invoice_id:
        stmt = stmt.where(
            sa.or_(
                BillingPayment.stripe_checkout_id == invoice_id,
                BillingPayment.id == _uuid(payment_id),
            )
        )
    else:
        stmt = stmt.where(BillingPayment.id == _uuid(payment_id))
    return (
        await session.execute(
            stmt.with_for_update().execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()


def _uuid(value) -> uuid.UUID | None:  # noqa: ANN001
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def is_custom(invoice: dict) -> bool:
    return (invoice.get("metadata") or {}).get("kind") == METADATA_KIND


async def apply_paid(session: AsyncSession, invoice: dict) -> bool:
    """A custom invoice is paid: mark the row paid and grant its packages and balance
    credit. Idempotent (the row's state, and each grant's ``invoice:<id>:<line>``
    reference). Returns False when the invoice is not one of ours. Does not commit."""
    from app.services import payments

    row = await _by_invoice(session, invoice)
    if row is None:
        log.warning("custom_invoice.unknown", invoice_id=invoice.get("id"))
        return False
    if row.state == "paid":
        return True
    invoice_id = str(invoice.get("id") or row.stripe_checkout_id)
    set_org_context(session, row.org_id)
    units = 0
    credited = 0
    for i, ln in enumerate((row.detail or {}).get("lines") or []):
        ref = f"invoice:{invoice_id}:{i}"
        if ln.get("type") == "package":
            await bundles.credit(
                session, row.org_id, ln["package"], int(ln["units"]),
                reference=ref, note=f"Invoice {invoice.get('number') or invoice_id}",
            )
            units += int(ln["units"])
        elif ln.get("type") == "credit":
            micros = int(ln["amount_cents"]) * _CENT
            await credits.topup(
                session, row.org_id, micros,
                reference=ref, note=f"Invoice {invoice.get('number') or invoice_id}",
            )
            credited += micros
    paid = int(invoice.get("amount_paid") or 0) * _CENT
    paid_at = (invoice.get("status_transitions") or {}).get("paid_at")
    intent = payments._invoice_intent(invoice)
    if intent is not None and await payments._by_intent(session, intent) is not None:
        intent = None
    row.state = "paid"
    row.stripe_checkout_id = invoice_id
    row.stripe_payment_intent_id = row.stripe_payment_intent_id or intent
    row.paid_micros = paid
    row.list_micros = paid + int(row.discount_micros)
    row.units_credited = units
    row.credited_micros = credited
    row.stripe_fee_micros = 0 if paid <= 0 else row.stripe_fee_micros
    row.paid_at = datetime.fromtimestamp(int(paid_at), timezone.utc) if paid_at else _now()
    row.detail = {k: v for k, v in (row.detail or {}).items() if k != "error"}
    await session.flush()
    return True


async def handle_event(session: AsyncSession, event: dict) -> bool:
    """``invoice.paid`` / ``invoice.payment_failed`` / ``invoice.voided`` for a custom
    invoice. Returns True when it was ours (then committed)."""
    invoice = (event.get("data") or {}).get("object") or {}
    if not is_custom(invoice):
        return False
    event_type = event.get("type")
    if event_type == "invoice.paid":
        await apply_paid(session, invoice)
    elif event_type in ("invoice.payment_failed", "invoice.voided"):
        row = await _by_invoice(session, invoice)
        if row is not None and row.state != "paid":
            row.state = "void" if event_type == "invoice.voided" else "failed"
    await session.commit()
    return True


async def void(session: AsyncSession, settings, row: BillingPayment) -> BillingPayment:  # noqa: ANN001
    """Cancel an unpaid invoice (on Stripe too). Commits."""
    from app.services import stripe_client

    if row.state == "paid":
        raise ConflictError("A paid invoice cannot be voided; refund it in Stripe")
    if row.state == "void":
        return row
    if row.stripe_checkout_id:
        stripe = stripe_client._stripe(settings)
        await stripe_client._run_sync(stripe.Invoice.void_invoice, row.stripe_checkout_id)
    row.state = "void"
    await session.commit()
    return row


async def get(session: AsyncSession, org_id: uuid.UUID, payment_id: uuid.UUID) -> BillingPayment:
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(BillingPayment).where(
                BillingPayment.id == payment_id,
                BillingPayment.org_id == org_id,
                BillingPayment.kind == KIND,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Invoice not found")
    return row


async def for_org(session: AsyncSession, org_id: uuid.UUID, limit: int = 100) -> list[dict]:
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(BillingPayment)
            .where(BillingPayment.org_id == org_id, BillingPayment.kind == KIND)
            .order_by(BillingPayment.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [to_dict(r) for r in rows]


def to_dict(row: BillingPayment) -> dict:
    d = row.detail or {}
    return {
        "id": str(row.id),
        "state": row.state,
        "number": d.get("number"),
        "memo": d.get("memo"),
        "lines": d.get("lines") or [],
        "list_micros": int(row.list_micros),
        "discount_micros": int(row.discount_micros),
        "total_micros": int(row.list_micros) - int(row.discount_micros),
        "paid_micros": int(row.paid_micros) if row.state == "paid" else 0,
        "error": d.get("error"),
        "collection": d.get("collection") or "charge_card",
        "emailed_to": d.get("emailed_to"),
        "hosted_invoice_url": d.get("hosted_invoice_url"),
        "invoice_pdf": d.get("invoice_pdf"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
    }
