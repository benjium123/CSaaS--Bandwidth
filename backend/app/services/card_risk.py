"""P44c: stolen-card checks on a card being saved, and the reaction to fraud evidence.

Saving a card is what arms auto-recharge, so it is the moment to be strict:

  card_country_mismatch  the card was issued outside the workspace's country (US/GB)
  too_many_cards         more than MAX_NEW_CARDS_30D cards added in 30 days - card testing
  shared card            the same card is saved on ANOTHER workspace: allowed, but an
                         operator alert is opened (multi-accounting, or a stolen card
                         reused after a ban)

``react_to_fraud`` is what an early fraud warning or a chargeback does: take the money
back out of the balance, ban the card, pause calling and texting, and open an alert. It
pauses (operator-reversible), it never suspends - a disputed charge is sometimes a
customer's own bank being confused.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import PermissionDeniedError

log = structlog.get_logger("card_risk")

MAX_NEW_CARDS_30D = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def open_alert(
    session: AsyncSession, org_id: uuid.UUID | None, kind: str, detail: dict
) -> None:
    from app.models import SecurityAlert

    session.add(SecurityAlert(id=uuid.uuid4(), kind=kind[:32], org_id=org_id, detail=detail))


#: P44c purchase velocity: card testing shows up as many checkouts in a short time.
MAX_CHECKOUTS_PER_HOUR = 3
_CHECKOUT_ACTIONS = ("billing.topup_started", "billing.bundle_checkout_started")


async def check_checkout(
    session: AsyncSession, settings, org_id: uuid.UUID, amount_micros: int  # noqa: ANN001
) -> None:
    """Refuse a new top-up/bundle checkout: more than MAX_CHECKOUTS_PER_HOUR started in the
    last hour, or (for a workspace younger than FRAUD_NEW_ACCOUNT_DAYS) more than
    FRAUD_NEW_ACCOUNT_DAILY_PURCHASE_MICROS started today, this one included."""
    from app.models import AuditLogEntry
    from app.services import exposure

    set_org_context(session, org_id)
    now = _now()
    rows = (
        await session.execute(
            sa.select(AuditLogEntry.created_at, AuditLogEntry.detail).where(
                AuditLogEntry.org_id == org_id,
                AuditLogEntry.action.in_(_CHECKOUT_ACTIONS),
                AuditLogEntry.created_at >= now - timedelta(days=1),
            )
        )
    ).all()

    def _aware(value):  # noqa: ANN001, ANN202
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    last_hour = [r for r in rows if _aware(r[0]) >= now - timedelta(hours=1)]
    if len(last_hour) >= MAX_CHECKOUTS_PER_HOUR:
        raise PermissionDeniedError(
            "Too many payments were started in the last hour. Try again later.",
            code="checkout_rate_limited",
        )
    if await exposure.is_new(session, settings, org_id):
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = sum(
            int((r[1] or {}).get("amount_micros") or (r[1] or {}).get("paid_micros") or 0)
            for r in rows
            if _aware(r[0]) >= midnight
        )
        cap = int(getattr(settings, "fraud_new_account_daily_purchase_micros", 100_000_000))
        if today + int(amount_micros) > cap:
            raise PermissionDeniedError(
                f"New accounts can add up to ${cap // 1_000_000} a day for their first "
                "30 days. Try a smaller amount or wait until tomorrow.",
                code="new_account_purchase_limit",
            )
    set_org_context(session, org_id)


async def check_new_card(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    fingerprint: str | None,
    card_country: str | None,
) -> None:
    """Raise PermissionDeniedError when this card must not be saved on this workspace."""
    from app.models import PaymentMethod
    from app.services import phone_region

    home = (await phone_region.for_org(session, org_id)).upper()
    country = (card_country or "").upper()
    if country and country != home:
        await open_alert(
            session, org_id, "card_country_mismatch", {"card_country": country, "home": home}
        )
        raise PermissionDeniedError(
            "This card was issued outside your business's country. Use a card issued in "
            "the same country as your business.",
            code="card_country_mismatch",
        )

    set_org_context(session, org_id)
    recent = (
        await session.execute(
            sa.select(sa.func.count(PaymentMethod.id)).where(
                PaymentMethod.org_id == org_id,
                PaymentMethod.created_at >= _now() - timedelta(days=30),
            )
        )
    ).scalar_one()
    if int(recent) >= MAX_NEW_CARDS_30D:
        await open_alert(session, org_id, "too_many_cards", {"cards_30d": int(recent)})
        raise PermissionDeniedError(
            "Too many cards were added to this account recently. Contact support to add "
            "another.",
            code="too_many_cards",
        )

    if fingerprint:
        # JUSTIFIED allow_unscoped: looking for the same card on OTHER workspaces.
        other = (
            await session.execute(
                sa.select(PaymentMethod.org_id)
                .where(
                    PaymentMethod.card_fingerprint == fingerprint,
                    PaymentMethod.org_id != org_id,
                )
                .limit(1)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if other is not None:
            await open_alert(
                session, org_id, "shared_card", {"also_on_org": str(other)}
            )
        set_org_context(session, org_id)


async def react_to_fraud(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    source: str,
    reference: str,
    amount_micros: int,
    fingerprint: str | None,
    intent_id: str,
) -> None:
    """Freeze the disputed money, ban the card, pause the workspace, alert ops.

    The hold is keyed on the PAYMENT (``intent_id``), not on the warning or dispute id: a
    payment can get both an early fraud warning and a chargeback, and a replayed webhook
    arrives too. Either way its credit is taken out once."""
    from app.models import CreditLedgerEntry
    from app.services import ban_list, credits, monitor_score

    set_org_context(session, org_id)
    ref = f"fraud:{intent_id}"[:120]
    already = (
        await session.execute(
            sa.select(CreditLedgerEntry.id).where(
                CreditLedgerEntry.org_id == org_id, CreditLedgerEntry.reference == ref
            )
        )
    ).first()
    if already is None and amount_micros > 0:
        await credits.adjust(
            session,
            org_id,
            -int(amount_micros),
            reference=ref,
            note=f"Held: {source} on a card payment",
            created_by=None,
        )
    if fingerprint:
        try:
            await ban_list.add(
                session,
                kind="card_fingerprint",
                value=fingerprint,
                reason=f"{source} {reference}"[:200],
                source_org_id=org_id,
            )
        except Exception:  # noqa: BLE001 - an existing ban is fine; never block the pause
            log.warning("card_risk.ban_failed", org_id=str(org_id))
    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id)
    if state.level != "paused":
        monitor_score.enter_pause(state, reason=f"{source}: {reference}"[:255])
    await open_alert(
        session,
        org_id,
        source,
        {"reference": reference, "amount_micros": int(amount_micros)},
    )


#: Stripe events this module owns (routes/webhooks.py dispatches them here).
HANDLED_EVENT_TYPES = frozenset(
    {"radar.early_fraud_warning.created", "charge.dispute.created", "charge.dispute.closed"}
)


def _org_from_metadata(metadata: dict | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str((metadata or {}).get("org_id")))
    except (TypeError, ValueError):
        return None


async def handle_stripe_event(session: AsyncSession, settings, event: dict) -> None:  # noqa: ANN001
    """React to fraud evidence on one of our payments. The caller commits.

    radar.early_fraud_warning.created  the card issuer reports the payment as fraud. We
        ask ops to refund it by hand (efw_refund_needed alert; a refund heads off the chargeback),
        take the credit it bought back out, ban the card and pause the workspace.
    charge.dispute.created             a chargeback: the money is already gone, so the
        credit it bought is held, the card banned, the workspace paused.
    charge.dispute.closed (won)        the held credit is given back; the pause stays for
        an operator to lift.
    """
    from app.services import credits, stripe_client

    etype = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    intent_id = obj.get("payment_intent")
    if not intent_id:
        log.warning("card_risk.event_without_intent", event_type=etype)
        return
    intent = await stripe_client.retrieve_intent_with_charge(settings, str(intent_id))
    metadata = intent.get("metadata") or {}
    org_id = _org_from_metadata(metadata)
    if org_id is None:
        log.warning("card_risk.event_unknown_org", event_type=etype, intent=str(intent_id))
        await open_alert(session, None, "stripe_fraud_unmatched", {"intent": str(intent_id)})
        return
    charge = intent.get("latest_charge") or {}
    card = ((charge.get("payment_method_details") or {}).get("card")) or {}
    fingerprint = card.get("fingerprint")
    # Only a credit top-up bought balance one-for-one; anything else (bundles, numbers)
    # is stopped by the pause. Hold what the ledger shows was actually credited for this
    # payment - a payment refunded at once by the risk check was never credited.
    credited = 0
    if metadata.get("kind") == "credit_topup":
        from app.models import CreditLedgerEntry

        set_org_context(session, org_id)
        credited = int(
            (
                await session.execute(
                    sa.select(sa.func.coalesce(sa.func.sum(CreditLedgerEntry.amount_micros), 0))
                    .where(
                        CreditLedgerEntry.org_id == org_id,
                        CreditLedgerEntry.entry_type == "topup",
                        CreditLedgerEntry.reference == str(intent_id),
                    )
                )
            ).scalar_one()
        )
    already_refunded = int(charge.get("amount_refunded") or 0) >= int(
        charge.get("amount") or intent.get("amount_received") or 1
    )

    if etype == "radar.early_fraud_warning.created":
        # Refunds are an operator decision, made by hand in Stripe: ask for one, never
        # send it from here. The credit stays held and the workspace paused meanwhile.
        if not already_refunded:
            await open_alert(
                session,
                org_id,
                "efw_refund_needed",
                {"intent": str(intent_id), "warning": str(obj.get("id") or "")},
            )
        await react_to_fraud(
            session,
            org_id,
            source="early_fraud_warning",
            reference=str(obj.get("id") or intent_id),
            amount_micros=credited,
            fingerprint=fingerprint,
            intent_id=str(intent_id),
        )
        return

    dispute_id = str(obj.get("id") or intent_id)
    if etype == "charge.dispute.created":
        await react_to_fraud(
            session,
            org_id,
            source="chargeback",
            reference=dispute_id,
            amount_micros=credited,
            fingerprint=fingerprint,
            intent_id=str(intent_id),
        )
        return

    if etype == "charge.dispute.closed" and obj.get("status") == "won" and credited:
        from app.models import CreditLedgerEntry

        if int(charge.get("amount_refunded") or 0) > 0:
            # Refunded after an early fraud warning: the money went back to the card, so
            # the held credit stays held even though the dispute was won.
            return
        set_org_context(session, org_id)
        ref = f"fraud-release:{intent_id}"[:120]

        async def _has(reference: str) -> bool:
            return (
                await session.execute(
                    sa.select(CreditLedgerEntry.id).where(
                        CreditLedgerEntry.org_id == org_id,
                        CreditLedgerEntry.reference == reference,
                    )
                )
            ).first() is not None

        # Give back only what was actually held, and only once.
        if await _has(f"fraud:{intent_id}"[:120]) and not await _has(ref):
            await credits.adjust(
                session,
                org_id,
                credited,
                reference=ref,
                note="Chargeback won: held credit returned",
                created_by=None,
            )
        await open_alert(session, org_id, "chargeback_won", {"dispute": dispute_id})
