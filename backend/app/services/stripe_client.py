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


async def _run_sync(func, *args, **kwargs):
    """Run a blocking Stripe SDK call off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(func, *args, **kwargs),
    )


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
    }
    if customer_email:
        params["customer_email"] = customer_email

    session_obj = await _run_sync(stripe.checkout.Session.create, **params)
    return {"id": session_obj["id"], "url": session_obj["url"]}


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
    metadata = {"org_id": str(org.id), "kind": "credit_topup"}

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
        return stripe.Webhook.construct_event(payload, signature, webhook_secret)
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
    }


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
