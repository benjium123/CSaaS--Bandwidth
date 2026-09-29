"""In-app purchases paid with the workspace's saved card, no Checkout page: credit top-ups and
SMS / MMS / call-minute bundles bought from the mobile app.

Money-owned. The charge carries the SAME metadata a Checkout purchase carries, and a successful
charge runs the SAME fulfillment the payment_intent.succeeded webhook runs (fraud screen, then
the bundle credit or the credit top-up). Every step is idempotent on the intent id, so the
webhook arriving afterwards is a no-op, and if the inline fulfillment fails the webhook still
credits the payment.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import Org, PaymentMethod
from app.models.billing_v2 import BillingPayment
from app.services import audit as audit_svc
from app.services import bundles, card_risk, credits, payments, stripe_client

log = structlog.get_logger("inapp_pay")

TOPUP_PRESETS = frozenset({25_000_000, 50_000_000, 100_000_000})
MIN_TOPUP_MICROS = 5_000_000
MAX_TOPUP_MICROS = 5_000_000_000
BUNDLE_KINDS = ("sms", "mms", "voice")

MSG_REQUIRES_ACTION = (
    "Your bank wants to confirm this payment. Finish it on the secure payment page."
)
MSG_REFUSED = (
    "This payment was refunded automatically by our fraud checks, so nothing was added. "
    "Contact support if this is a mistake."
)
MSG_PROCESSING = "Your payment is processing. It will show up in a minute."
MSG_NO_CARD = "There is no card on file. Add one on ringlite.io under Settings, Billing."


def validate_topup(amount_micros: int) -> None:
    if amount_micros in TOPUP_PRESETS:
        return
    if not (MIN_TOPUP_MICROS <= amount_micros <= MAX_TOPUP_MICROS):
        raise ValidationFailedError(
            "Amount must be $25, $50, $100, or a custom amount between $5 and $5,000."
        )


async def default_card(session: AsyncSession, org_id: uuid.UUID) -> PaymentMethod | None:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(PaymentMethod)
            .where(PaymentMethod.org_id == org_id)
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def price(
    session: AsyncSession, org_id: uuid.UUID, *, kind: str, qty: int, amount_micros: int
) -> dict[str, int]:
    """{list_micros, discount_micros, paid_micros, units, qty} - the server's price, never the
    app's."""
    if kind == "credit":
        validate_topup(amount_micros)
        return {
            "list_micros": amount_micros,
            "discount_micros": 0,
            "paid_micros": amount_micros,
            "units": 0,
            "qty": 1,
        }
    if kind not in BUNDLE_KINDS:
        raise ValidationFailedError("Unknown purchase kind.")
    q = await bundles.quote(session, kind, qty, org_id)
    return {
        "list_micros": q["list"],
        "discount_micros": q["discount"],
        "paid_micros": q["paid"],
        "units": q["units"],
        "qty": qty,
    }


def _card_out(card: PaymentMethod | None) -> dict | None:
    if card is None:
        return None
    return {"brand": card.brand, "last4": card.last4}


async def quote(
    session: AsyncSession, org_id: uuid.UUID, *, kind: str, qty: int, amount_micros: int
) -> dict:
    """The checkout summary: price plus the card that will be charged (None = no card)."""
    out: dict = await price(session, org_id, kind=kind, qty=qty, amount_micros=amount_micros)
    out["kind"] = kind
    out["card"] = _card_out(await default_card(session, org_id))
    return out


def payment_id_for(org_id: uuid.UUID, request_id: str) -> uuid.UUID:
    """The same request (a double tap, a network retry) always maps to the same payment."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"ringlite-inapp:{org_id}:{request_id}")


async def fulfill(session: AsyncSession, settings, org: Org, intent: dict) -> bool:  # noqa: ANN001
    """What the payment_intent.succeeded webhook does, run inline. False = refused (refunded,
    nothing credited). Commits."""
    if await payments.refuse_risky_payment(session, settings, intent):
        return False
    if await payments.handle_bundle_intent(session, intent):
        return True
    amount = int(intent.get("amount_received") or 0) * 10_000
    set_org_context(session, org.id)
    # Idempotent on (org, topup, intent id): the webhook's own call becomes a no-op.
    await credits.topup(session, org.id, amount_micros=amount, reference=intent["id"],
                        note="Card payment")
    await payments.record_topup_paid(
        session, org.id, intent_id=intent["id"], amount_micros=amount, kind="topup"
    )
    await session.commit()
    return True


async def pay(
    session: AsyncSession,
    settings,  # noqa: ANN001
    org: Org,
    *,
    kind: str,
    qty: int,
    amount_micros: int,
    request_id: str,
    actor_user_id: uuid.UUID | None,
    actor_api_key_id: uuid.UUID | None,
) -> dict:
    """Charge the saved card and credit the purchase. Returns {"status": succeeded |
    requires_action | declined | refused | processing, "message"?, ...price}."""
    q = await price(session, org.id, kind=kind, qty=qty, amount_micros=amount_micros)
    card = await default_card(session, org.id)
    if card is None:
        raise ValidationFailedError(MSG_NO_CARD, code="no_card_on_file")
    pid = payment_id_for(org.id, request_id)
    set_org_context(session, org.id)
    row: BillingPayment | None = None
    if kind != "credit":
        row = await session.get(BillingPayment, pid)
        if row is not None and row.state == "paid":
            return {"status": "succeeded", **q}  # a replay of a paid request: charge nothing
    # P44c: the same card-testing velocity and new-account caps as a Checkout purchase.
    await card_risk.check_checkout(session, settings, org.id, q["paid_micros"])
    set_org_context(session, org.id)
    if kind == "credit":
        metadata = {"org_id": str(org.id), "kind": "credit_topup", "source": "in_app"}
        action = "billing.topup_started"
        detail: dict = {"amount_micros": q["paid_micros"], "source": "in_app"}
        description = f"Ringlite credit ${q['paid_micros'] / 1_000_000:,.2f}"
    else:
        if row is None:
            row, _ = await payments.start_bundle_payment(
                session, org.id, kind=kind, qty=qty, payment_id=pid
            )
        metadata = {
            "org_id": str(org.id),
            "kind": f"{kind}_bundle",
            "qty": str(qty),
            "payment_id": str(pid),
        }
        action = "billing.bundle_checkout_started"
        detail = {"kind": kind, "qty": qty, "paid_micros": q["paid_micros"], "source": "in_app"}
        label = "call minutes" if kind == "voice" else kind.upper()
        description = f"Ringlite {qty} x {bundles.UNITS_PER_BUNDLE[kind]:,} {label} bundle"
    audit_svc.record(
        session,
        org.id,
        action=action,
        target_type="org",
        target_id=str(org.id),
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        detail=detail,
    )
    await session.commit()

    result = await stripe_client.charge_saved_card(
        settings,
        amount_micros=q["paid_micros"],
        customer_id=card.stripe_customer_id,
        payment_method_id=card.stripe_payment_method_id,
        metadata=metadata,
        idempotency_key=f"inapp-{pid}",
        description=description,
    )
    status = result.get("status")
    if status in ("requires_action", "failed"):
        if row is not None:
            set_org_context(session, org.id)
            row.state = "failed"
            await session.commit()
        if status == "requires_action":
            return {"status": "requires_action", "message": MSG_REQUIRES_ACTION, **q}
        return {"status": "declined", "message": result.get("reason") or "", **q}
    if status != "succeeded":
        return {"status": "processing", "message": MSG_PROCESSING, **q}

    try:
        credited = await fulfill(session, settings, org, result["intent"])
    except Exception:
        # The charge went through; the webhook will credit it (idempotent), so report it as
        # processing instead of failing a paid purchase.
        await session.rollback()
        log.exception("inapp_pay.fulfill_failed", org_id=str(org.id), intent_id=result.get("id"))
        return {"status": "processing", "message": MSG_PROCESSING, **q}
    if not credited:
        return {"status": "refused", "message": MSG_REFUSED, **q}
    return {"status": "succeeded", **q}
