"""Stripe subscription webhook handling.

This module mirrors Stripe Subscription objects into the local ``subscriptions``
table and keeps the existing org->plan link (``orgs.plan_code``) in step with Stripe:
pointed at an entitled subscription's plan while one entitles the org, and cleared when
the last one stops entitling it (canceled/unpaid/incomplete_expired, or deleted). It
deliberately does not commit: the webhook route owns the transaction boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    ENTITLED_SUBSCRIPTION_STATUSES,
    SUBSCRIPTION_STATUSES,
    Org,
    Subscription,
    is_entitled,
)

log = structlog.get_logger("subscriptions")

HANDLED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)

#: Stripe statuses from which we never move a subscription back to past_due.
TERMINAL_SUBSCRIPTION_STATUSES: frozenset[str] = frozenset(
    {
        "canceled",
        "incomplete_expired",
        "unpaid",
    }
)


def _event_object(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the Stripe event object dict, or None if the payload is malformed."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    obj = data.get("object")
    return obj if isinstance(obj, dict) else None


def _metadata(obj: dict[str, Any]) -> dict[str, Any]:
    """Return Stripe metadata if it is a dict, otherwise an empty dict."""
    metadata = obj.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _coerce_str(value: Any) -> str | None:
    """Best-effort string coercion for Stripe id-like fields."""
    return value if isinstance(value, str) else None


def _coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion for Stripe unix timestamps."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Best-effort boolean coercion with an explicit fallback."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(value, int):
        return value != 0
    return default


def _parse_org_id(value: Any) -> uuid.UUID | None:
    """Parse a UUID org id without raising on attacker-controlled metadata."""
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


def _datetime_from_unix(value: Any) -> datetime | None:
    """Convert a Stripe unix timestamp to a timezone-aware UTC datetime."""
    timestamp = _coerce_int(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        log.warning("stripe_invalid_timestamp", value=value)
        return None


def _status_from_payload(
    raw_status: Any,
    existing: Subscription | None,
    *,
    event_type: str,
    stripe_subscription_id: str,
) -> str:
    """Normalise a Stripe status, preserving unknown strings and never raising.

    A new Stripe status should not crash the webhook, so we mirror the string
    exactly and just warn. If the field is missing or the wrong type on an
    existing row we keep the old status rather than guessing.
    """
    if isinstance(raw_status, str):
        if raw_status not in SUBSCRIPTION_STATUSES:
            log.warning(
                "stripe_subscription_status_unknown",
                event_type=event_type,
                stripe_subscription_id=stripe_subscription_id,
                status=raw_status,
            )
        return raw_status

    if existing is not None:
        log.warning(
            "stripe_subscription_status_missing_or_invalid",
            event_type=event_type,
            stripe_subscription_id=stripe_subscription_id,
            raw_status=raw_status,
        )
        return existing.status

    log.warning(
        "stripe_subscription_status_missing_for_new_row",
        event_type=event_type,
        stripe_subscription_id=stripe_subscription_id,
        raw_status=raw_status,
    )
    return "incomplete"


def _resolve_org_id(
    metadata_org_id: uuid.UUID | None,
    existing: Subscription | None,
    *,
    event_type: str,
    stripe_subscription_id: str,
) -> uuid.UUID | None:
    """Choose the org a webhook event belongs to.

    If we already have a row for the Stripe subscription, the row's tenant is the
    source of truth. Metadata can identify a new row, but it must not move an
    existing subscription across orgs.
    """
    if existing is not None:
        if metadata_org_id is not None and metadata_org_id != existing.org_id:
            log.warning(
                "stripe_event_org_mismatch",
                event_type=event_type,
                stripe_subscription_id=stripe_subscription_id,
                metadata_org_id=str(metadata_org_id),
                existing_org_id=str(existing.org_id),
            )
        return existing.org_id
    return metadata_org_id


async def _find_unscoped_subscription(
    session: AsyncSession, stripe_subscription_id: str
) -> Subscription | None:
    """Find a subscription by its Stripe id without tenant scoping.

    Webhook handling starts with no tenant context, so we must be able to look up
    the one unique row across all orgs before we can call set_org_context().
    """
    stmt = (
        select(Subscription)
        .where(Subscription.stripe_subscription_id == stripe_subscription_id)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _maybe_update_org_plan_linkage(
    session: AsyncSession,
    org_id: uuid.UUID,
    plan_code: str | None,
    status: str,
) -> None:
    """Point the existing org->plan link at an entitled subscription's plan.

    This reuses ``orgs.plan_code`` rather than inventing a second link. The
    anniversary anchor is only set once and never moved, so an existing
    plan_started_at is left intact for services/plans.period_for.
    """
    if not is_entitled(status) or not plan_code:
        return

    org = await session.get(Org, org_id)
    if org is None:
        log.warning(
            "subscription_org_missing_for_plan_link",
            org_id=str(org_id),
            plan_code=plan_code,
        )
        return

    org.plan_code = plan_code
    if org.plan_started_at is None:
        org.plan_started_at = datetime.now(timezone.utc)


async def _reconcile_org_plan_linkage(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Re-derive ``orgs.plan_code`` when a subscription stops entitling the org.

    Called when a subscription reaches a terminal non-entitled status (canceled,
    incomplete_expired, unpaid) or is deleted. The org must stop getting that plan's
    allowances - but ONLY if no other subscription still entitles it. If another
    entitled subscription remains, ``orgs.plan_code`` is pointed at THAT subscription's
    plan instead of being cleared, so a working subscription is never clobbered.

    ``plan_started_at`` is history (the billing anniversary anchor read by
    ``services/plans.period_for``) and is deliberately left untouched, mirroring
    ``_maybe_update_org_plan_linkage``, which only ever sets it when it is unset.
    """
    # Make the caller's status change visible below, so a subscription that has just
    # gone non-entitled is not still counted among the org's entitled subscriptions.
    await session.flush()

    org = await session.get(Org, org_id)
    if org is None:
        log.warning(
            "subscription_org_missing_for_plan_unlink",
            org_id=str(org_id),
        )
        return

    if await has_entitled_subscription(session, org_id):
        owner = await current_subscription(session, org_id)
        if owner is not None:
            org.plan_code = owner.plan_code
            return

    # Nothing entitles this org any more: stop granting the plan's allowances. Only the
    # plan link is cleared - plan_started_at stays as the historical anniversary anchor.
    org.plan_code = None


async def current_subscription(session: AsyncSession, org_id: uuid.UUID) -> Subscription | None:
    """Return the org's current subscription, preferring an entitled row."""
    set_org_context(session, org_id)

    # Entitled rows sort first, then the newest row wins deterministically.
    entitled_first = sa.case(
        (Subscription.status.in_(ENTITLED_SUBSCRIPTION_STATUSES), 0),
        else_=1,
    )
    stmt = select(Subscription).order_by(entitled_first, Subscription.created_at.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def has_entitled_subscription(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Return True exactly when the org has at least one entitled subscription."""
    set_org_context(session, org_id)
    from app.models import NumberPurchase

    if (
        await session.execute(
            select(NumberPurchase.id)
            .where(
                NumberPurchase.org_id == org_id,
                NumberPurchase.subscription_status.in_(ENTITLED_SUBSCRIPTION_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None:
        return True

    stmt = (
        select(Subscription.id)
        .where(Subscription.status.in_(ENTITLED_SUBSCRIPTION_STATUSES))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def handle_event(session: AsyncSession, event: dict) -> None:
    """Handle one Stripe event, never committing and never raising."""
    event_type = event.get("type")
    if event_type not in HANDLED_EVENT_TYPES:
        return

    obj = _event_object(event)
    if obj is None:
        log.warning("stripe_event_missing_object", event_type=event_type)
        return

    if event_type == "checkout.session.completed":
        await _handle_checkout_session_completed(session, obj)
    elif event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
    }:
        await _handle_subscription_upsert(session, obj, event_type=event_type)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(session, obj)
    elif event_type == "invoice.payment_failed":
        await _handle_invoice_payment_failed(session, obj)


async def _handle_checkout_session_completed(
    session: AsyncSession, checkout_session: dict[str, Any]
) -> None:
    """Create or update a row from a Checkout Session in subscription mode."""
    if checkout_session.get("mode") != "subscription":
        log.debug("ignoring_non_subscription_checkout_session")
        return

    stripe_subscription_id = _coerce_str(checkout_session.get("subscription"))
    if not stripe_subscription_id:
        log.warning("checkout_session_missing_subscription_id")
        return

    existing = await _find_unscoped_subscription(session, stripe_subscription_id)

    metadata = _metadata(checkout_session)
    metadata_org_id = _parse_org_id(metadata.get("org_id"))
    metadata_plan_code = _coerce_str(metadata.get("plan_code"))

    org_id = _resolve_org_id(
        metadata_org_id,
        existing,
        event_type="checkout.session.completed",
        stripe_subscription_id=stripe_subscription_id,
    )
    plan_code = metadata_plan_code or (existing.plan_code if existing else None)

    if org_id is None:
        log.warning(
            "checkout_session_unattributable",
            stripe_subscription_id=stripe_subscription_id,
        )
        return
    if plan_code is None:
        log.warning(
            "checkout_session_missing_plan_code",
            stripe_subscription_id=stripe_subscription_id,
        )
        return

    payment_status = checkout_session.get("payment_status")
    status = (
        "active"
        if checkout_session.get("status") == "complete"
        and payment_status in ("paid", "no_payment_required")
        else "incomplete"
    )

    set_org_context(session, org_id)

    if existing is None:
        subscription = Subscription(
            org_id=org_id,
            plan_code=plan_code,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=_coerce_str(checkout_session.get("customer")),
            status=status,
            current_period_end=None,
            cancel_at_period_end=False,
        )
        session.add(subscription)
    else:
        existing.plan_code = plan_code
        existing.stripe_customer_id = (
            _coerce_str(checkout_session.get("customer")) or existing.stripe_customer_id
        )
        existing.status = status

    await _maybe_update_org_plan_linkage(session, org_id, plan_code, status)


async def _handle_subscription_upsert(
    session: AsyncSession,
    subscription_object: dict[str, Any],
    *,
    event_type: str,
) -> None:
    """Upsert from a Stripe Subscription object."""
    stripe_subscription_id = _coerce_str(subscription_object.get("id"))
    if not stripe_subscription_id:
        log.warning(
            "subscription_event_missing_id",
            event_type=event_type,
        )
        return

    existing = await _find_unscoped_subscription(session, stripe_subscription_id)

    metadata = _metadata(subscription_object)
    metadata_org_id = _parse_org_id(metadata.get("org_id"))
    metadata_plan_code = _coerce_str(metadata.get("plan_code"))

    org_id = _resolve_org_id(
        metadata_org_id,
        existing,
        event_type=event_type,
        stripe_subscription_id=stripe_subscription_id,
    )
    plan_code = metadata_plan_code or (existing.plan_code if existing else None)

    if org_id is None:
        log.warning(
            "subscription_event_unattributable",
            event_type=event_type,
            stripe_subscription_id=stripe_subscription_id,
        )
        return
    if plan_code is None:
        log.warning(
            "subscription_event_missing_plan_code",
            event_type=event_type,
            stripe_subscription_id=stripe_subscription_id,
        )
        return

    status = _status_from_payload(
        subscription_object.get("status"),
        existing,
        event_type=event_type,
        stripe_subscription_id=stripe_subscription_id,
    )
    customer_id = _coerce_str(subscription_object.get("customer"))
    cancel_at_period_end = _coerce_bool(
        subscription_object.get("cancel_at_period_end"),
        default=existing.cancel_at_period_end if existing else False,
    )

    current_period_end = _datetime_from_unix(subscription_object.get("current_period_end"))
    if current_period_end is None and existing is not None:
        # A missing/invalid timestamp should not erase what Stripe already sent us.
        current_period_end = existing.current_period_end

    set_org_context(session, org_id)

    if existing is None:
        subscription = Subscription(
            org_id=org_id,
            plan_code=plan_code,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=customer_id,
            status=status,
            current_period_end=current_period_end,
            cancel_at_period_end=cancel_at_period_end,
        )
        session.add(subscription)
    else:
        existing.plan_code = plan_code
        existing.stripe_customer_id = customer_id or existing.stripe_customer_id
        existing.status = status
        existing.current_period_end = current_period_end
        existing.cancel_at_period_end = cancel_at_period_end

    if status in TERMINAL_SUBSCRIPTION_STATUSES:
        # Entitlement has ended: the org only keeps a plan if another subscribed and
        # entitled subscription still owns one.
        await _reconcile_org_plan_linkage(session, org_id)
    else:
        await _maybe_update_org_plan_linkage(session, org_id, plan_code, status)


async def _handle_subscription_deleted(
    session: AsyncSession, subscription_object: dict[str, Any]
) -> None:
    """Mark the matching local subscription as canceled."""
    stripe_subscription_id = _coerce_str(subscription_object.get("id"))
    if not stripe_subscription_id:
        log.warning("subscription_deleted_missing_id")
        return

    existing = await _find_unscoped_subscription(session, stripe_subscription_id)
    if existing is None:
        log.warning(
            "subscription_deleted_unknown_subscription",
            stripe_subscription_id=stripe_subscription_id,
        )
        return

    set_org_context(session, existing.org_id)
    existing.status = "canceled"
    # Deletion always ends entitlement, so the org's plan link must be re-derived: cleared
    # unless another entitled subscription still owns a plan.
    await _reconcile_org_plan_linkage(session, existing.org_id)


async def _handle_invoice_payment_failed(session: AsyncSession, invoice: dict[str, Any]) -> None:
    """Move a non-terminal subscription to past_due.

    This never creates a row, because the subscription should already exist from
    checkout or a prior customer.subscription.* event.
    """
    subscription_value = invoice.get("subscription")
    if isinstance(subscription_value, str):
        stripe_subscription_id = subscription_value
    elif isinstance(subscription_value, dict):
        stripe_subscription_id = _coerce_str(subscription_value.get("id")) or ""
    else:
        stripe_subscription_id = ""

    if not stripe_subscription_id:
        log.warning("invoice_payment_failed_missing_subscription_id")
        return

    existing = await _find_unscoped_subscription(session, stripe_subscription_id)
    if existing is None:
        log.warning(
            "invoice_payment_failed_unknown_subscription",
            stripe_subscription_id=stripe_subscription_id,
        )
        return

    if existing.status in TERMINAL_SUBSCRIPTION_STATUSES:
        log.debug(
            "invoice_payment_failed_terminal_subscription",
            stripe_subscription_id=stripe_subscription_id,
            status=existing.status,
        )
        return

    set_org_context(session, existing.org_id)
    existing.status = "past_due"
    await _maybe_update_org_plan_linkage(
        session,
        existing.org_id,
        existing.plan_code,
        "past_due",
    )
