"""Stripe adapter for customer card top-ups and auto-recharge.

The ``stripe`` Python package is imported lazily: the tests and some deployments do not
have it installed, and importing it at module load would prevent the whole API from
booting. Money amounts in this module are always integer micros at the boundary; the SDK
wants cents, so we convert with ``amount_micros // 10_000``.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import structlog

from app.errors import (
    FeatureUnavailableError,
    UnauthenticatedError,
    ValidationFailedError,
)

log = structlog.get_logger("stripe_client")

MIN_TOPUP_MICROS = 5_000_000
MAX_TOPUP_MICROS = 5_000_000_000


def _stripe(settings) -> Any:
    """Lazily import and configure the Stripe SDK."""
    configured = getattr(settings, "stripe_secret_key", None)
    try:
        secret = configured.get_secret_value().strip() if configured else ""
    except Exception:
        secret = ""
    if not secret:
        raise FeatureUnavailableError("Card payments are not set up yet.")

    try:
        import stripe
    except ImportError as exc:
        log.warning("stripe_import_failed", error=str(exc))
        raise FeatureUnavailableError("Card payments are not set up yet.") from exc

    stripe.api_key = secret
    return stripe


def is_configured(settings) -> bool:
    configured = getattr(settings, "stripe_secret_key", None)
    if configured is None:
        return False
    try:
        secret = configured.get_secret_value()
    except Exception:
        return False
    return bool(secret and secret.strip())


def _plain(obj: Any) -> Any:
    """Convert a Stripe SDK object into plain dicts, recursively.

    Since stripe-python 8 a StripeObject is NOT a dict: ``obj.get(...)`` raises
    AttributeError, and every caller here reads responses with ``.get``. That failed only
    in production - the tests mock the SDK with plain dicts. A ListObject is left alone so
    callers can still page through it with ``auto_paging_iter``.
    """
    try:
        from stripe import ListObject, StripeObject
    except ImportError:
        return obj
    if isinstance(obj, StripeObject) and not isinstance(obj, ListObject):
        return obj.to_dict()
    return obj


async def _run_sync(func, *args, **kwargs):
    """Run a blocking Stripe SDK call off the event loop; responses come back as dicts."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        functools.partial(func, *args, **kwargs),
    )
    return _plain(result)


#: P44c: ask for 3D Secure on every Checkout payment the card supports it for. An
#: authenticated payment shifts fraud-chargeback liability to the card issuer, and a thief
#: holding only the card number cannot pass the bank's challenge.
THREE_DS_OPTIONS: dict[str, Any] = {"card": {"request_three_d_secure": "any"}}


async def create_checkout_session(
    settings,
    *,
    org,
    amount_micros: int,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> dict:
    """Create a Checkout Session for a one-off credit top-up."""
    if amount_micros < MIN_TOPUP_MICROS or amount_micros > MAX_TOPUP_MICROS:
        raise ValidationFailedError("Top-up amount must be between $5 and $5,000.")

    stripe = _stripe(settings)
    cents = amount_micros // 10_000
    metadata = {"org_id": str(org.id), "kind": "credit_topup"}

    params = {
        "mode": "payment",
        "line_items": [
            {
                "price_data": {
                    "currency": settings.stripe_price_currency,
                    "product_data": {"name": "Assistant credits"},
                    "unit_amount": cents,
                },
                "quantity": 1,
            }
        ],
        "success_url": success_url,
        "cancel_url": cancel_url,
        # A Checkout Session's own metadata is NOT copied onto the PaymentIntent.
        # The webhook reads the PAYMENT INTENT's metadata, so it must be set here too.
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
        "payment_method_options": THREE_DS_OPTIONS,
    }
    if customer_email:
        params["customer_email"] = customer_email

    session_obj = await _run_sync(stripe.checkout.Session.create, **params)
    return {"id": session_obj["id"], "url": session_obj["url"]}


async def create_bundle_checkout_session(
    settings,
    *,
    org,
    kind: str,
    qty: int,
    unit_amount_micros: int,
    product_name: str,
    payment_id: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> dict:
    """Checkout Session for `qty` message bundles at an already-discounted unit price.

    The quantity is fixed (not adjustable on Stripe's page) because the discount was
    decided server-side for exactly this quantity.
    """
    stripe = _stripe(settings)
    metadata = {
        "org_id": str(org.id),
        "kind": f"{kind}_bundle",
        "qty": str(int(qty)),
        "payment_id": payment_id,
    }
    params: dict[str, Any] = {
        "mode": "payment",
        "line_items": [
            {
                "price_data": {
                    "currency": settings.stripe_price_currency,
                    "product_data": {"name": product_name},
                    "unit_amount": int(unit_amount_micros) // 10_000,
                },
                "quantity": int(qty),
            }
        ],
        "success_url": success_url,
        "cancel_url": cancel_url,
        # Same trap as the top-up: the webhook reads the PaymentIntent's metadata.
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
        "payment_method_options": THREE_DS_OPTIONS,
    }
    if customer_email:
        params["customer_email"] = customer_email
    session_obj = await _run_sync(stripe.checkout.Session.create, **params)
    return {"id": session_obj["id"], "url": session_obj["url"]}


async def payment_fee_micros(settings, payment_intent_id: str) -> int | None:
    """Stripe's processing fee for a succeeded PaymentIntent, or None when not settled yet."""
    stripe = _stripe(settings)
    intent = await _run_sync(
        stripe.PaymentIntent.retrieve,
        payment_intent_id,
        expand=["latest_charge.balance_transaction"],
    )
    charge = intent.get("latest_charge") if isinstance(intent, dict) else None
    if isinstance(charge, dict):
        bt = charge.get("balance_transaction")
        if isinstance(bt, dict) and bt.get("fee") is not None:
            return int(bt["fee"]) * 10_000
    return None


async def charge_risk(settings, payment_intent_id: str) -> dict | None:
    """Radar's verdict on a succeeded PaymentIntent's charge: risk_score, risk_level and the
    card's cvc_check. None when there is no charge to read."""
    stripe = _stripe(settings)
    intent = await _run_sync(
        stripe.PaymentIntent.retrieve, payment_intent_id, expand=["latest_charge"]
    )
    charge = intent.get("latest_charge") if isinstance(intent, dict) else None
    if not isinstance(charge, dict):
        return None
    outcome = charge.get("outcome") or {}
    card = (charge.get("payment_method_details") or {}).get("card") or {}
    return {
        "risk_score": outcome.get("risk_score"),
        "risk_level": outcome.get("risk_level"),
        "cvc_check": (card.get("checks") or {}).get("cvc_check"),
    }


async def refund_fraudulent(settings, payment_intent_id: str, *, reason: str) -> None:
    """Refund a whole payment as fraudulent (Stripe also feeds the card into Radar's block
    lists). Idempotent per payment intent."""
    stripe = _stripe(settings)
    await _run_sync(
        stripe.Refund.create,
        payment_intent=payment_intent_id,
        reason="fraudulent",
        metadata={"kind": "risk_block", "why": reason[:200]},
        idempotency_key=f"risk-refund-{payment_intent_id}",
    )


async def create_subscription_checkout_session(
    settings,
    *,
    org,
    price_id: str,
    plan_code: str,
    success_url: str,
    cancel_url: str,
    customer_id: str | None = None,
    customer_email: str | None = None,
) -> dict:
    """Create a Checkout Session for a recurring plan subscription.

    No micros conversion here, unlike the top-up above: a recurring Stripe Price owns its
    own amount, and the caller must pass a real ``price_...`` id that an operator created.
    A plan without one cannot reach this function - the route refuses first and names the
    plan - because inventing a price id would charge a customer an amount nobody chose.
    """
    price_id = (price_id or "").strip()
    if not price_id:
        raise ValidationFailedError("This plan is not available for purchase yet.")

    stripe = _stripe(settings)
    metadata = {"org_id": str(org.id), "kind": "subscription", "plan_code": plan_code}

    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        # A Checkout Session's metadata is NOT copied onto the Subscription object, and the
        # customer.subscription.* webhooks carry only the Subscription. Without this second
        # copy, every renewal, cancellation and status change would arrive unattributable
        # to an org. Same trap as payment_intent_data above.
        "metadata": metadata,
        "subscription_data": {"metadata": metadata},
        "payment_method_options": THREE_DS_OPTIONS,
    }
    # Never both: Stripe rejects a session that names a customer and an email.
    if customer_id:
        params["customer"] = customer_id
    elif customer_email:
        params["customer_email"] = customer_email

    session_obj = await _run_sync(stripe.checkout.Session.create, **params)
    log.info("stripe_subscription_checkout_created", org_id=str(org.id), plan_code=plan_code)
    return {
        "id": session_obj["id"],
        "url": session_obj["url"],
        "subscription_id": session_obj.get("subscription"),
    }


async def charge_off_session(
    settings,
    *,
    org,
    amount_micros: int,
    payment_method_id: str,
    customer_id: str,
    idempotency_key: str,
) -> dict:
    """Charge a saved card off-session for auto-recharge.

    A declined card is a normal outcome, so CardError-family exceptions are returned as
    ``{"id": "", "status": "failed", "reason": ...}`` rather than raised.
    """
    if amount_micros < MIN_TOPUP_MICROS:
        raise ValidationFailedError("Auto-recharge amount must be at least $5.")

    stripe = _stripe(settings)
    cents = amount_micros // 10_000
    metadata = {"org_id": str(org.id), "kind": "credit_topup", "source": "auto_recharge"}

    params = {
        "amount": cents,
        "currency": settings.stripe_price_currency,
        "customer": customer_id,
        "payment_method": payment_method_id,
        "confirm": True,
        "off_session": True,
        "metadata": metadata,
        # Stripe's own idempotency_key request option prevents a retried auto-recharge
        # from double-charging a card.
        "idempotency_key": idempotency_key,
    }

    try:
        intent = await _run_sync(stripe.PaymentIntent.create, **params)
    except Exception as exc:
        # The SDK's CardError classes are not importable without the stripe package, so
        # we duck-type on the ``code`` attribute instead.
        code = getattr(exc, "code", "")
        if not code:
            raise

        reason = "We could not charge this card."
        if code == "card_declined":
            reason = "Your card was declined."
        elif code == "expired_card":
            reason = "Your card is expired."
        elif code == "incorrect_cvc":
            reason = "Your card's security code was incorrect."
        elif code == "processing_error":
            reason = "Your card provider had a problem processing this payment."

        return {"id": "", "status": "failed", "reason": reason}

    return {"id": intent.get("id", ""), "status": intent.get("status", "")}


def verify_webhook(settings, payload: bytes, signature: str) -> dict:
    """Verify a Stripe webhook signature and return the event."""
    webhook_secret_obj = getattr(settings, "stripe_webhook_secret", None)
    try:
        webhook_secret = webhook_secret_obj.get_secret_value().strip() if webhook_secret_obj else ""
    except Exception:
        webhook_secret = ""
    if not webhook_secret:
        raise FeatureUnavailableError("Card payments are not set up yet.")

    try:
        import stripe
    except ImportError as exc:
        log.warning("stripe_import_failed", error=str(exc))
        raise FeatureUnavailableError("Card payments are not set up yet.") from exc

    try:
        return _plain(stripe.Webhook.construct_event(payload, signature, webhook_secret))
    except Exception as exc:
        log.warning("stripe_webhook_verification_failed", error=str(exc))
        raise UnauthenticatedError(
            "We could not verify that this came from our payment provider."
        ) from exc


async def list_payment_methods(settings, *, customer_id: str) -> list[dict]:
    stripe = _stripe(settings)
    result = await _run_sync(
        stripe.PaymentMethod.list,
        customer=customer_id,
        type="card",
    )
    out: list[dict] = []
    for pm in result.auto_paging_iter():
        pm = _plain(pm)
        card = pm.get("card") or {}
        out.append(
            {
                "id": pm["id"],
                "brand": card.get("brand", ""),
                "last4": card.get("last4", ""),
            }
        )
    return out


async def ensure_customer(
    settings,
    *,
    org,
    existing_customer_id: str | None = None,
    email: str | None = None,
) -> str:
    """Return an existing Stripe Customer id, or create and return a new one.

    The org schema does not have a stripe_customer_id column, so callers carry
    the id on payment_method rows and pass it back in here when one exists.
    """
    if existing_customer_id and existing_customer_id.strip():
        return existing_customer_id

    stripe = _stripe(settings)
    params = {
        "name": org.name,
        "metadata": {"org_id": str(org.id)},
    }
    if email:
        params["email"] = email

    customer = await _run_sync(stripe.Customer.create, **params)
    return customer["id"]


async def attach_payment_method(
    settings,
    *,
    payment_method_id: str,
    customer_id: str,
) -> dict:
    stripe = _stripe(settings)
    pm = await _run_sync(
        stripe.PaymentMethod.attach,
        payment_method_id,
        customer=customer_id,
    )
    card = pm.get("card") or {}
    return {
        "id": pm["id"],
        "brand": card.get("brand", ""),
        "last4": card.get("last4", ""),
        "fingerprint": card.get("fingerprint"),
        "country": card.get("country"),
    }


async def retrieve_intent_with_charge(settings, payment_intent_id: str) -> dict:
    """P44c: the PaymentIntent (with its metadata) and its latest charge, as plain dicts."""
    stripe = _stripe(settings)
    return await _run_sync(
        stripe.PaymentIntent.retrieve, payment_intent_id, expand=["latest_charge"]
    )


async def refund_intent(settings, payment_intent_id: str, *, idempotency_key: str) -> dict:
    """P44c: refund a payment in full (an early fraud warning was raised on it)."""
    stripe = _stripe(settings)
    return await _run_sync(
        stripe.Refund.create,
        payment_intent=payment_intent_id,
        reason="fraudulent",
        idempotency_key=idempotency_key,
    )


async def detach_payment_method(
    settings,
    *,
    payment_method_id: str,
) -> dict:
    stripe = _stripe(settings)
    pm = await _run_sync(stripe.PaymentMethod.detach, payment_method_id)
    card = pm.get("card") or {}
    return {
        "id": pm["id"],
        "brand": card.get("brand", ""),
        "last4": card.get("last4", ""),
    }


# --------------------------------------------------------------------------------------
# P41 Stripe Identity - owner ID + selfie checks and selfie step-ups
# --------------------------------------------------------------------------------------
IDENTITY_ALLOWED_DOCUMENTS = ["driving_license", "id_card", "passport"]


async def create_verification_session(
    settings, *, metadata: dict[str, str], return_url: str | None = None
) -> dict:
    """Start a document + live selfie check. Returns {id, url, client_secret, status}.

    ``url`` is Stripe's hosted page: the console redirects there, so no Stripe.js is needed
    in the frontend. Nothing about the person is sent - Stripe collects it all.
    """
    stripe = _stripe(settings)
    params: dict[str, Any] = {
        "type": "document",
        "options": {
            "document": {
                "allowed_types": IDENTITY_ALLOWED_DOCUMENTS,
                "require_live_capture": True,
                "require_matching_selfie": True,
            }
        },
        "metadata": metadata,
    }
    if return_url:
        params["return_url"] = return_url
    vs = await _run_sync(stripe.identity.VerificationSession.create, **params)
    return {
        "id": vs["id"],
        "url": vs.get("url"),
        "client_secret": vs.get("client_secret"),
        "status": vs.get("status"),
    }


async def retrieve_verification_outcome(settings, verification_session_id: str) -> dict:
    """The verified outputs we keep: name, date of birth (hashed by the caller, never
    stored), document type and issuing country, plus status and last error code."""
    stripe = _stripe(settings)
    vs = await _run_sync(
        stripe.identity.VerificationSession.retrieve,
        verification_session_id,
        expand=["verified_outputs", "last_verification_report"],
    )
    outputs = vs.get("verified_outputs") or {}
    report = vs.get("last_verification_report") or {}
    document = (report.get("document") if isinstance(report, dict) else None) or {}
    dob = outputs.get("dob") or {}
    last_error = vs.get("last_error") or {}
    return {
        "id": vs["id"],
        "status": vs.get("status"),
        "metadata": dict(vs.get("metadata") or {}),
        "first_name": outputs.get("first_name"),
        "last_name": outputs.get("last_name"),
        "dob": (
            f"{int(dob['year']):04d}-{int(dob['month']):02d}-{int(dob['day']):02d}"
            if dob.get("year") and dob.get("month") and dob.get("day")
            else None
        ),
        "document_type": document.get("type"),
        "document_country": document.get("issuing_country"),
        "error_code": last_error.get("code") if isinstance(last_error, dict) else None,
    }


def verify_webhook_any(settings, payload: bytes, signature: str) -> tuple[dict, str]:
    """Verify against STRIPE_WEBHOOK_SECRET, then STRIPE_IDENTITY_WEBHOOK_SECRET when one is
    configured (a separate Stripe endpoint for Identity events).

    Returns (event, source) where source is "billing" or "identity". The caller MUST use it:
    the two endpoints exist so their blast radii stay separate, and accepting either secret
    for any event type means whoever holds the Identity secret can forge billing events
    (refunds, payment_intent.succeeded) too.
    """
    try:
        return verify_webhook(settings, payload, signature), "billing"
    except (UnauthenticatedError, FeatureUnavailableError):
        identity_secret = settings.stripe_identity_webhook_secret.get_secret_value().strip()
        if not identity_secret:
            raise
    try:
        import stripe
    except ImportError as exc:
        raise FeatureUnavailableError("Card payments are not set up yet.") from exc
    try:
        return (
            _plain(stripe.Webhook.construct_event(payload, signature, identity_secret)),
            "identity",
        )
    except Exception as exc:
        log.warning("stripe_identity_webhook_verification_failed", error=str(exc))
        raise UnauthenticatedError(
            "We could not verify that this came from our payment provider."
        ) from exc
