"""Stripe-paid Telnyx number carts. Payment is verified server-side before ordering."""

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models import KycProfile, NumberPurchase, OrgNumber
from app.services import stripe_client


def public(purchase):
    return {
        "id": str(purchase.id),
        "state": purchase.state,
        "numbers": purchase.numbers,
        "monthly_total_cents": 1500 * sum(n.get("state") != "released" for n in purchase.numbers),
        "detail": purchase.detail,
        "checkout_url": purchase.checkout_url if purchase.state == "checkout" else None,
    }


async def create(session, settings, org_id, numbers):
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
    from app.api.routes.numbers import persist_ordered_number
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
            number = await persist_ordered_number(
                session,
                purchase.org_id,
                carrier,
                result,
                provisioning={
                    "number_purchase_id": str(purchase.id),
                    "billing": "stripe_subscription",
                },
            )
            await session.refresh(purchase, ["subscription_status"])
            from app.models.subscriptions import is_entitled

            number.is_active = number.is_active and is_entitled(purchase.subscription_status)
            entries = list(purchase.numbers)
            entries[index] = {
                "e164": item["e164"],
                "state": number.status,
                "number_id": str(number.id),
            }
            purchase.numbers = entries
            await session.commit()
        purchase.state = (
            "complete" if all(n["state"] == "active" for n in purchase.numbers) else "activating"
        )
        purchase.detail = None
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
