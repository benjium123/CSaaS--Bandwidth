"""Stripe-paid Telnyx number carts. Payment is verified server-side before ordering."""

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models import KycProfile, NumberPurchase, OrgNumber
from app.services import stripe_client

log = structlog.get_logger("number_purchases")


async def telnyx_available_cents(settings) -> int | None:
    """Telnyx balance plus credit line, in cents. None when Telnyx is not configured."""
    import httpx

    key = settings.telnyx_api_key.get_secret_value()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.telnyx.com/v2/balance", headers={"Authorization": f"Bearer {key}"}
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        return round(float(data.get("available_credit") or data.get("balance") or 0) * 100)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return 0


async def require_carrier_funds(settings, count: int) -> None:
    """Refuse checkout the carrier cannot fulfil. Taking a customer's money for numbers
    Telnyx will then refuse to order leaves a paid cart nobody can complete."""
    available = await telnyx_available_cents(settings)
    if available is None:
        return
    needed = count * settings.telnyx_number_reserve_cents + settings.telnyx_balance_floor_cents
    if available < needed:
        log.error(
            "number_checkout_carrier_underfunded", available_cents=available, needed_cents=needed
        )
        raise FeatureUnavailableError(
            "New numbers are briefly unavailable. Please try again in a little while.",
            code="numbers_temporarily_unavailable",
        )


def public(purchase):
    return {
        "id": str(purchase.id),
        "state": purchase.state,
        "numbers": purchase.numbers,
        "monthly_total_cents": 1500 * sum(n.get("state") != "released" for n in purchase.numbers),
        "detail": purchase.detail,
        "checkout_url": purchase.checkout_url if purchase.state == "checkout" else None,
    }


async def create(session, settings, org_id, numbers, *, emergency_address_id=None):
    from app.api.routes.numbers import to_e164

    normalized = [to_e164(n) for n in numbers]
    if len(set(normalized)) != len(normalized):
        raise ValidationFailedError("Choose each number only once")
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    if not profile or profile.status not in ("approved", "reverification_due"):
        raise ValidationFailedError("Your application must be approved before purchasing numbers")
    stripe = stripe_client._stripe(settings)
    if not settings.stripe_webhook_secret.get_secret_value():
        raise FeatureUnavailableError("Checkout is being configured. Please try again shortly.")
    price = await stripe_client._run_sync(stripe.Price.retrieve, settings.stripe_number_price_id)
    if (
        price.get("unit_amount") != 1500
        or price.get("currency") != "usd"
        or price.get("recurring", {}).get("interval") != "month"
        or price.get("recurring", {}).get("interval_count") != 1
        or price.get("transform_quantity")
        or price.get("recurring", {}).get("usage_type", "licensed") != "licensed"
        or not price.get("active")
    ):
        raise FeatureUnavailableError("The phone-number price needs administrator attention.")
    await require_carrier_funds(settings, len(normalized))
    from app.models import Org

    await session.execute(sa.select(Org).where(Org.id == org_id).with_for_update())
    existing = (
        (
            await session.execute(
                sa.select(NumberPurchase)
                .where(
                    NumberPurchase.org_id == org_id,
                    NumberPurchase.state.in_(
                        ("checkout", "paid", "provisioning", "activating", "needs_attention")
                    ),
                )
                .order_by(NumberPurchase.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if existing:
        if existing.state == "checkout" and existing.checkout_id:
            remote = await stripe_client._run_sync(
                stripe.checkout.Session.retrieve, existing.checkout_id
            )
            if remote.get("status") == "expired":
                existing.state = "expired"
            elif [n["e164"] for n in existing.numbers] == normalized:
                return existing
            else:
                raise ConflictError(
                    "Finish your existing checkout before choosing different numbers."
                )
        elif existing.state != "checkout" or [n["e164"] for n in existing.numbers] != normalized:
            raise ConflictError("Your previous number purchase is still being processed.")
    if (
        await session.execute(
            sa.select(OrgNumber.id)
            .where(OrgNumber.e164.in_(normalized))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).first():
        raise ConflictError("One of those numbers is no longer available. Search again.")
    purchase = (
        existing
        if existing and existing.state == "checkout"
        else NumberPurchase(
            id=uuid.uuid4(),
            org_id=org_id,
            numbers=[{"e164": n, "state": "selected"} for n in normalized],
            state="checkout",
        )
    )
    if emergency_address_id is not None:
        purchase.emergency_address_id = emergency_address_id
        purchase.e911_acknowledged_at = datetime.now(timezone.utc)
    session.add(purchase)
    await session.commit()
    metadata = {"kind": "number_purchase", "purchase_id": str(purchase.id), "org_id": str(org_id)}
    base = settings.public_web_url.rstrip("/")
    checkout = await stripe_client._run_sync(
        stripe.checkout.Session.create,
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": settings.stripe_number_price_id, "quantity": len(normalized)}],
        metadata=metadata,
        subscription_data={"metadata": metadata},
        success_url=f"{base}/choose-numbers?purchase={purchase.id}",
        cancel_url=f"{base}/choose-numbers?purchase={purchase.id}&cancelled=1",
        idempotency_key=f"number-purchase-{purchase.id}",
    )
    purchase.checkout_id = checkout["id"]
    purchase.checkout_url = checkout["url"]
    await session.commit()
    return purchase


async def fulfill(session, request, purchase):
    from app.providers import numbers as numbers_api
    from app.providers import registry_org

    settings = request.app.state.settings
    stripe = stripe_client._stripe(settings)
    purchase = (
        await session.execute(
            sa.select(NumberPurchase)
            .where(NumberPurchase.id == purchase.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if purchase.state == "activating":
        rows = list(
            (
                await session.execute(
                    sa.select(OrgNumber).where(OrgNumber.org_id == purchase.org_id)
                )
            ).scalars()
        )
        purchased = [
            n for n in rows if (n.provisioning or {}).get("number_purchase_id") == str(purchase.id)
        ]
        if any(n.status == "failed" for n in purchased):
            purchase.state = "needs_attention"
            purchase.detail = "Payment received. A carrier order needs support assistance."
        elif len(purchased) == len(purchase.numbers) and all(
            n.status == "active" for n in purchased
        ):
            purchase.state = "complete"
        await session.commit()
        return purchase
    if purchase.state in ("complete", "needs_attention", "provisioning", "expired"):
        return purchase
    if not purchase.checkout_id:
        return purchase
    checkout = await stripe_client._run_sync(stripe.checkout.Session.retrieve, purchase.checkout_id)
    if checkout.get("payment_status") != "paid" or checkout.get("status") != "complete":
        return purchase
    if checkout.get("metadata", {}).get("purchase_id") != str(purchase.id):
        raise ValidationFailedError("Payment does not match this purchase")
    subscription = await stripe_client._run_sync(
        stripe.Subscription.retrieve, checkout["subscription"]
    )
    items = subscription.get("items", {}).get("data", [])
    if subscription.get("status") != "active":
        raise ValidationFailedError(
            "Your subscription is not active. Contact support to review this payment."
        )
    if (
        len(items) != 1
        or items[0].get("price", {}).get("id") != settings.stripe_number_price_id
        or items[0].get("quantity") != len(purchase.numbers)
    ):
        raise ValidationFailedError("Subscription does not match the selected numbers")
    purchase.subscription_id = subscription["id"]
    purchase.subscription_status = subscription["status"]
    purchase.state = "provisioning"
    from app.models import Org

    org = await session.get(Org, purchase.org_id)
    org.number_subscription_required = True
    await session.commit()  # Durable claim: a duplicate callback cannot order twice.
    purchase_id = purchase.id
    registry = request.app.state.carriers
    try:
        registry = await registry_org.prime_org_registry(
            session,
            settings,
            purchase.org_id,
            global_registry=getattr(registry, "global_registry", registry),
        )
        carrier = registry.get("telnyx")
        provider = numbers_api.as_provider(carrier)
        from app.services import telephony_access

        await telephony_access.require_telephony_allowed(session, purchase.org_id, "number")
        for index, item in enumerate(purchase.numbers):
            await telephony_access.require_telephony_allowed(session, purchase.org_id, "number")
            result = await provider.order_number(item["e164"])
            await _record_number(session, request, purchase, index, carrier, result)
        purchase.state = _settled_state(purchase.numbers)
        purchase.detail = None
        await session.commit()
        from app.services import tendlc

        await tendlc.associate_new_numbers(session, settings, purchase.org_id)
        from app.services import e911

        await e911.enable_for_purchase(session, settings, purchase)
    except Exception as error:
        # Carrier timeouts are ambiguous. Never retry a potentially accepted order or
        # charge again automatically; retain the paid cart for operator reconciliation.
        await session.rollback()
        purchase = await session.get(NumberPurchase, purchase_id)
        purchase.state = "needs_attention"
        from app.models import SecurityAlert

        session.add(
            SecurityAlert(
                kind="number_purchase",
                org_id=purchase.org_id,
                detail={"purchase_id": str(purchase.id), "error_type": type(error).__name__},
            )
        )
        purchase.detail = (
            "Payment received. A number needs provisioning assistance; "
            "support can reconcile this purchase without charging you again."
        )
    await session.commit()
    return purchase


async def _record_number(session, request, purchase, index, carrier, result):
    """Store one carrier-accepted number for this purchase, make it callable, commit."""
    from app.api.routes.numbers import persist_ordered_number
    from app.models.subscriptions import is_entitled
    from app.voice_plane import trunk_sync

    number = await persist_ordered_number(
        session,
        purchase.org_id,
        carrier,
        result,
        provisioning={"number_purchase_id": str(purchase.id), "billing": "stripe_subscription"},
    )
    await session.refresh(purchase, ["subscription_status"])
    number.is_active = number.is_active and is_entitled(purchase.subscription_status)
    # Callable now rather than at the next sweeper reconcile, up to ten minutes on.
    # ensure_number never raises; the sweeper reconcile still backstops a failure.
    if number.status == "active":
        await trunk_sync.ensure_number(
            getattr(request.app.state, "livekit", None),
            request.app.state.settings,
            carrier.name,
            number.e164,
        )
    entries = list(purchase.numbers)
    entries[index] = {"e164": result.e164, "state": number.status, "number_id": str(number.id)}
    purchase.numbers = entries
    await session.commit()
    return number


#: Entries that are no longer part of what the customer pays for.
_GONE = ("released", "refunded")


def _settled_state(entries) -> str:
    live = [n for n in entries if n.get("state") not in _GONE]
    if not live:
        return "refunded"
    if any(not n.get("number_id") for n in live):
        return "needs_attention"
    return "complete" if all(n["state"] == "active" for n in live) else "activating"


#: A provisioning run older than this is presumed dead (the process stopped mid-order).
STALE_PROVISIONING = timedelta(minutes=10)

_NEEDS_HELP = (
    "Payment received. A number needs provisioning assistance; "
    "support can reconcile this purchase without charging you again."
)


async def retry(session, request, purchase_id):
    """Operator: finish ordering the numbers of a paid purchase that stalled.

    Never orders blind. Each missing number is first looked up on the Telnyx account: a
    timed-out order may have gone through, and ordering it again would fail or double
    bill. An owned number is linked; an unowned one is ordered. A number already held by
    another workspace is never touched. A failure on one number leaves the rest done.
    """
    from app.providers import numbers as numbers_api
    from app.providers import registry_org
    from app.providers.numbers import OrderResult

    purchase = await _locked(session, purchase_id)
    if purchase.state not in ("needs_attention", "provisioning"):
        raise ConflictError(f"This purchase is {purchase.state}; there is nothing to retry.")
    updated = purchase.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if (
        purchase.state == "provisioning"
        and updated is not None
        and datetime.now(timezone.utc) - updated < STALE_PROVISIONING
    ):
        raise ConflictError("This purchase is still being set up. Try again in a few minutes.")

    settings = request.app.state.settings
    registry = request.app.state.carriers
    registry = await registry_org.prime_org_registry(
        session,
        settings,
        purchase.org_id,
        global_registry=getattr(registry, "global_registry", registry),
    )
    carrier = registry.get("telnyx")
    provider = numbers_api.as_provider(carrier)
    failures = []
    for index, item in enumerate(list(purchase.numbers)):
        if item.get("number_id") or item.get("state") in _GONE:
            continue
        e164 = item["e164"]
        try:
            held = (
                await session.execute(
                    sa.select(OrgNumber)
                    .where(OrgNumber.e164 == e164)
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            ).scalar_one_or_none()
            if held is not None:
                if held.org_id != purchase.org_id:
                    raise ConflictError("already held by another workspace")
                entries = list(purchase.numbers)
                entries[index] = {"e164": e164, "state": held.status, "number_id": str(held.id)}
                purchase.numbers = entries
                await session.commit()
                continue
            owned = await provider.lookup_owned_number(e164)
            if owned is None:
                raise FeatureUnavailableError("Telnyx could not be asked whether it owns this")
            result = (
                OrderResult(e164=e164, provider_ref="", status="active")
                if owned
                else await provider.order_number(e164)
            )
            await _record_number(session, request, purchase, index, carrier, result)
        except Exception as error:
            await session.rollback()
            purchase = await _locked(session, purchase_id)
            failures.append(f"{e164}: {getattr(error, 'message', None) or error}")
    purchase.state = _settled_state(purchase.numbers)
    purchase.detail = _NEEDS_HELP if purchase.state == "needs_attention" else None
    await session.commit()
    from app.services import tendlc

    await tendlc.associate_new_numbers(session, settings, purchase.org_id)
    from app.services import e911

    await e911.enable_for_purchase(session, settings, purchase)
    log.info("number_purchase_retried", purchase_id=str(purchase.id), failures=failures)
    return purchase, failures


async def refund_unprovisioned(session, settings, purchase_id):
    """Operator: stop billing for, and refund, every number of a paid purchase that was
    never provisioned. Numbers that were provisioned are kept and stay billed.

    The Stripe quantity drops to the numbers kept (the subscription is cancelled when none
    are), and each dropped number's first month is refunded from the latest paid invoice.
    """
    purchase = await _locked(session, purchase_id)
    if purchase.state not in ("needs_attention", "provisioning") or not purchase.subscription_id:
        raise ConflictError(f"This purchase is {purchase.state}; there is nothing to refund.")
    dropped = [
        n["e164"]
        for n in purchase.numbers
        if not n.get("number_id") and n.get("state") not in _GONE
    ]
    if not dropped:
        raise ConflictError("Every number in this purchase was provisioned.")
    kept = sum(1 for n in purchase.numbers if n.get("number_id") and n.get("state") not in _GONE)
    stripe = stripe_client._stripe(settings)
    subscription = await stripe_client._run_sync(
        stripe.Subscription.retrieve, purchase.subscription_id
    )
    key = f"refund-unprovisioned-{purchase.id}-{len(dropped)}"
    if subscription["status"] != "canceled":
        if kept:
            item = subscription["items"]["data"][0]
            await stripe_client._run_sync(
                stripe.Subscription.modify,
                purchase.subscription_id,
                items=[{"id": item["id"], "quantity": kept}],
                proration_behavior="none",
                idempotency_key=key,
            )
        else:
            await stripe_client._run_sync(
                stripe.Subscription.cancel, purchase.subscription_id, idempotency_key=key
            )
            purchase.subscription_status = "canceled"
    payments = await stripe_client._run_sync(
        stripe.InvoicePayment.list, invoice=subscription["latest_invoice"], status="paid"
    )
    intent = next(
        (
            p["payment"]["payment_intent"]
            for p in payments.get("data", [])
            if (p.get("payment") or {}).get("payment_intent")
        ),
        None,
    )
    if not intent:
        await session.commit()
        raise FeatureUnavailableError(
            "Billing was reduced, but no card payment was found to refund. Refund it in Stripe."
        )
    amount = 1500 * len(dropped)
    refund = await stripe_client._run_sync(
        stripe.Refund.create,
        payment_intent=intent,
        amount=amount,
        metadata={"kind": "number_purchase_refund", "purchase_id": str(purchase.id)},
        idempotency_key=f"{key}-refund",
    )
    purchase.numbers = [
        {**n, "state": "refunded"} if n["e164"] in dropped and not n.get("number_id") else n
        for n in purchase.numbers
    ]
    purchase.state = _settled_state(purchase.numbers)
    purchase.detail = (
        f"{len(dropped)} number(s) could not be set up and were refunded (${amount / 100:.2f})."
    )
    await session.commit()
    log.info(
        "number_purchase_refunded",
        purchase_id=str(purchase.id),
        refund_id=refund.get("id"),
        amount_cents=amount,
    )
    return purchase


async def _locked(session, purchase_id):
    return (
        await session.execute(
            sa.select(NumberPurchase)
            .where(NumberPurchase.id == purchase_id)
            .with_for_update()
            .execution_options(populate_existing=True, **{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()


async def handle_event(session, request, event):
    obj = event.get("data", {}).get("object", {})
    metadata = obj.get("metadata") or {}
    if metadata.get("kind") != "number_purchase":
        return False
    try:
        purchase_id = uuid.UUID(metadata["purchase_id"])
    except (ValueError, KeyError):
        raise ValidationFailedError("Invalid purchase metadata") from None
    purchase = (
        await session.execute(
            sa.select(NumberPurchase)
            .where(NumberPurchase.id == purchase_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if purchase is None:
        raise ValidationFailedError("Unknown number purchase")
    set_org_context(session, purchase.org_id)
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        await fulfill(session, request, purchase)
    elif event["type"].startswith("customer.subscription."):
        stripe = stripe_client._stripe(request.app.state.settings)
        latest = await stripe_client._run_sync(stripe.Subscription.retrieve, obj["id"])
        purchase.subscription_id = latest["id"]
        purchase.subscription_status = latest["status"]
        from app.models.subscriptions import is_entitled

        for number in (
            await session.execute(sa.select(OrgNumber).where(OrgNumber.org_id == purchase.org_id))
        ).scalars():
            if (number.provisioning or {}).get("number_purchase_id") == str(
                purchase.id
            ) and number.status == "active":
                number.is_active = is_entitled(purchase.subscription_status)
    await session.commit()
    return True


async def sync_released_number(session, settings, number):
    """Stop recurring rental for released numbers, using an idempotent desired quantity."""
    purchase_id = (number.provisioning or {}).get("number_purchase_id")
    if not purchase_id:
        return
    purchase = (
        await session.execute(
            sa.select(NumberPurchase)
            .where(NumberPurchase.id == uuid.UUID(purchase_id))
            .with_for_update()
        )
    ).scalar_one()
    if not purchase.subscription_id:
        return
    purchase.numbers = [
        {**entry, "state": "released"} if entry.get("number_id") == str(number.id) else entry
        for entry in purchase.numbers
    ]
    rows = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.org_id == number.org_id))
    ).scalars()
    remaining = sum(
        1
        for n in rows
        if n.status != "released"
        and (n.provisioning or {}).get("number_purchase_id") == purchase_id
    )
    stripe = stripe_client._stripe(settings)
    subscription = await stripe_client._run_sync(
        stripe.Subscription.retrieve, purchase.subscription_id
    )
    if subscription["status"] == "canceled":
        return
    if remaining:
        item = subscription["items"]["data"][0]
        await stripe_client._run_sync(
            stripe.Subscription.modify,
            purchase.subscription_id,
            items=[{"id": item["id"], "quantity": remaining}],
            proration_behavior="none",
            idempotency_key=f"release-{number.id}",
        )
    else:
        await stripe_client._run_sync(
            stripe.Subscription.cancel,
            purchase.subscription_id,
            idempotency_key=f"release-{number.id}",
        )
        purchase.subscription_status = "canceled"
    await session.commit()
