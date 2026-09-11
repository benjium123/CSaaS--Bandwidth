
"""P24 AI metering, pricing and prepaid credits.

This module meters fine-grained AI usage idempotently, prices it with
``services.credits``, and only charges prepaid credits when billing enforcement
is switched on. It never commits; the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import InsufficientCreditsError, ValidationFailedError
from app.models import (
    AI_USAGE_KINDS,
    AI_USAGE_METRICS,
    AI_USAGE_SOURCES,
    AgentProfile,
    AiUsageEvent,
    Call,
    CreditLedgerEntry,
    MessageThread,
    Org,
    PaymentMethod,
    PlatformEvent,
)
from app.services import credits, spend

logger = structlog.get_logger(__name__)

# These constants are estimates used ONLY to size a reserve, never to bill.
# They assume one second of a voice call consumes 1 voice second, 1 speech-to-text
# second, 15 text-to-speech characters, 8 input tokens and 4 output tokens.
TTS_CHARS_PER_SECOND = 15
LLM_TOKENS_IN_PER_SECOND = 8
LLM_TOKENS_OUT_PER_SECOND = 4
DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_STT_PROVIDER = "deepgram"
DEFAULT_TTS_PROVIDER = "elevenlabs"
DEFAULT_VOICE_PROVIDER = "livekit"

# Allowed assistant fallbacks when a workspace cannot pay for a call.
CREDIT_FALLBACKS = ("human_flow", "busy")
DEFAULT_CREDIT_FALLBACK = "busy"

# Keys accepted on a batch usage item. ``settings`` is also a keyword on
# ``record()``, but record_batch supplies it explicitly and pops it below.
RECORD_FIELDS = frozenset({
    "provider",
    "kind",
    "metric",
    "quantity",
    "source",
    "idempotency_key",
    "call_id",
    "thread_id",
    "profile_id",
    "occurred_at",
    "settings",
})

MAX_MARGIN_DAYS = 92
MAX_MARGIN_ROWS = 200_000


async def _find_existing_event(
    session: AsyncSession, org_id: uuid.UUID, idempotency_key: str
) -> AiUsageEvent | None:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(AiUsageEvent).where(
                AiUsageEvent.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()


async def record(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    provider: str,
    kind: str,
    metric: str,
    quantity: int,
    source: str,
    idempotency_key: str,
    call_id: uuid.UUID | None = None,
    thread_id: uuid.UUID | None = None,
    profile_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    settings: Any | None = None,
) -> AiUsageEvent:
    """Meter one AI usage event idempotently.

    Returns the existing row unchanged when ``idempotency_key`` is already known.
    With billing enforcement off (the default) the event is written without
    debiting credits; this is shadow metering.
    """

    set_org_context(session, org_id)

    if kind not in AI_USAGE_KINDS:
        raise ValidationFailedError("Unknown usage type.")
    if metric not in AI_USAGE_METRICS:
        raise ValidationFailedError("Unknown usage metric.")
    if source not in AI_USAGE_SOURCES:
        raise ValidationFailedError("Unknown usage source.")
    if int(quantity) < 0:
        raise ValidationFailedError("Usage quantity cannot be negative.")

    # Tenant-scoped existence checks: the bound org context makes these lookups
    # org-local, so a row from another workspace is simply not found.
    if call_id is not None:
        found_call_id = (
            await session.execute(
                sa.select(Call.id).where(Call.id == call_id).limit(1)
            )
        ).scalar_one_or_none()
        if found_call_id is None:
            raise ValidationFailedError("That call is not part of this workspace.")

    if thread_id is not None:
        found_thread_id = (
            await session.execute(
                sa.select(MessageThread.id)
                .where(MessageThread.id == thread_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if found_thread_id is None:
            raise ValidationFailedError(
                "That conversation is not part of this workspace."
            )

    if profile_id is not None:
        found_profile_id = (
            await session.execute(
                sa.select(AgentProfile.id)
                .where(AgentProfile.id == profile_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if found_profile_id is None:
            raise ValidationFailedError(
                "That assistant is not part of this workspace."
            )

    existing = await _find_existing_event(session, org_id, idempotency_key)
    if existing is not None:
        return existing

    org = (
        await session.execute(sa.select(Org).where(Org.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise ValidationFailedError("Workspace not found")

    unit_cost, _is_override, is_known = await spend.resolve_rate(
        session, provider, metric
    )
    if not is_known:
        cost_micros = 0
        logger.warning(
            "ai_usage.unpriced_provider",
            provider=provider,
            metric=metric,
            message="Unpriced AI provider metric; metering continues at zero cost",
        )
    else:
        cost_micros = int(unit_cost) * int(quantity)

    price_micros = credits.price_for(
        cost_micros=cost_micros, kind=kind, quantity=int(quantity), org=org
    )

    event = AiUsageEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call_id,
        thread_id=thread_id,
        profile_id=profile_id,
        provider=provider,
        kind=kind,
        metric=metric,
        quantity=int(quantity),
        cost_micros=int(cost_micros),
        price_micros=int(price_micros),
        source=source,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        idempotency_key=idempotency_key,
    )

    try:
        # The unique constraint on idempotency_key is the real guard under
        # concurrency. A savepoint lets us recover without losing the caller's
        # outer transaction.
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        existing = await _find_existing_event(session, org_id, idempotency_key)
        if existing is not None:
            return existing
        raise

    enforce = bool(
        settings is not None and getattr(settings, "ai_billing_enforce", False)
    )
    if enforce and int(price_micros) > 0:
        await credits.charge_usage(
            session,
            org_id,
            int(price_micros),
            reference=f"usage:{event.id}",
        )

    return event


async def record_batch(
    session: AsyncSession,
    org_id: uuid.UUID,
    items: list[dict],
    *,
    settings: Any | None = None,
) -> tuple[int, int]:
    """Record a worker batch. Returns ``(accepted, duplicates)``.

    Duplicates are items whose idempotency key already existed before this batch.
    The caller commits once after the whole batch succeeds.
    """

    set_org_context(session, org_id)
    accepted = 0
    duplicates = 0
    required_fields = frozenset({"provider", "kind", "metric", "source", "quantity"})

    for item in items:
        unknown = set(item) - RECORD_FIELDS
        if unknown:
            raise ValidationFailedError(
                "That usage event has a field we do not recognise."
            )

        missing = required_fields - set(item)
        if missing:
            raise ValidationFailedError(
                "That usage event is missing a required field."
            )

        idempotency_key = item.get("idempotency_key")
        if not idempotency_key:
            raise ValidationFailedError(
                "Each usage event must include its deduplication key."
            )

        existing = await _find_existing_event(session, org_id, idempotency_key)
        if existing is not None:
            duplicates += 1
            continue

        fields = {k: v for k, v in item.items() if k in RECORD_FIELDS}
        # settings is supplied by the caller, not the event item.
        fields.pop("settings", None)
        await record(session, org_id, **fields, settings=settings)
        accepted += 1

    return accepted, duplicates


async def estimate_call_price(
    session: AsyncSession,
    org: Org,
    profile: Any,
    *,
    settings: Any | None = None,
) -> int:
    """Estimate the worst-case customer price of one assistant call.

    This is a reserve-sizing estimate only, never a billable amount.
    """

    set_org_context(session, org.id)

    max_call_seconds = int(getattr(profile, "max_call_seconds", None) or 0)
    if max_call_seconds <= 0:
        max_call_seconds = 900

    llm_provider = getattr(profile, "llm_provider", None) or DEFAULT_LLM_PROVIDER
    stt_provider = getattr(profile, "stt_provider", None) or DEFAULT_STT_PROVIDER
    tts_provider = getattr(profile, "tts_provider", None) or DEFAULT_TTS_PROVIDER
    voice_provider = getattr(profile, "voice_provider", None) or DEFAULT_VOICE_PROVIDER

    per_second_cost = 0
    components = (
        (voice_provider, "ai_voice_seconds", 1),
        (stt_provider, "stt_seconds", 1),
        (tts_provider, "tts_characters", TTS_CHARS_PER_SECOND),
        (llm_provider, "llm_tokens_in", LLM_TOKENS_IN_PER_SECOND),
        (llm_provider, "llm_tokens_out", LLM_TOKENS_OUT_PER_SECOND),
    )
    for provider, metric, units_per_second in components:
        unit_cost, _is_override, is_known = await spend.resolve_rate(
            session, provider, metric
        )
        if is_known:
            per_second_cost += int(unit_cost) * units_per_second

    total_cost = max(per_second_cost * max_call_seconds, 0)
    price_micros = credits.price_for(
        cost_micros=total_cost,
        kind="voice",
        quantity=max_call_seconds,
        org=org,
    )
    return max(int(price_micros), 0)


# IMPORTANT: this key rides in the auto-recharge JSON only because adding a column
# is Fable's call; it should move to its own column when Fable next opens the schema.
def credit_fallback(org) -> str:
    """Return what an assistant does when a workspace cannot pay for the call."""
    raw = getattr(org, "credit_auto_recharge", None)
    if isinstance(raw, dict):
        fallback = raw.get("fallback")
        if isinstance(fallback, str) and fallback in CREDIT_FALLBACKS:
            return fallback
    return DEFAULT_CREDIT_FALLBACK


def call_reserve_reference(call_id) -> str:
    """Return the canonical ledger reference for one call's reserve and release.

    Every reserve and release for a call MUST go through this one function. The
    (org, entry_type, reference) uniqueness in the ledger is what makes a replayed
    reserve a no-op; a hand-built string anywhere else would silently break
    double-spend protection.
    """
    return f"call:{call_id}"


async def reserve_for_call(
    session: AsyncSession,
    org: Org,
    profile: Any,
    call: Call,
    *,
    settings: Any | None = None,
) -> dict:
    """Reserve prepaid credits for the estimated worst-case cost of one call.

    Idempotent by construction: ``credits.reserve`` returns the existing row for a
    repeated reference, so a worker that re-fetches its config mid-call cannot
    double-hold. With billing enforcement off this is shadow mode and never takes
    a call away from a customer.
    """
    reference = call_reserve_reference(call.id)
    enforce = settings is not None and bool(
        getattr(settings, "ai_billing_enforce", False)
    )

    if not enforce:
        return {
            "ok": True,
            "reserved_micros": 0,
            "reference": reference,
            "fallback": None,
            "shadow": True,
        }

    estimate = await estimate_call_price(session, org, profile, settings=settings)
    try:
        await credits.reserve(session, org.id, estimate, reference=reference)
    except InsufficientCreditsError:
        logger.warning(
            "ai_usage.reserve_for_call_insufficient_credits",
            org_id=str(org.id),
            estimate_micros=estimate,
        )
        return {
            "ok": False,
            "reserved_micros": 0,
            "reference": reference,
            "fallback": credit_fallback(org),
            "message": "Add credits to keep your assistant answering.",
        }

    return {
        "ok": True,
        "reserved_micros": estimate,
        "reference": reference,
        "fallback": None,
    }


async def release_for_call(
    session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID
) -> bool:
    """Release the reserve held for a call. True when a reserve was actually given back.

    Idempotent: ``credits.release`` is a no-op the second time, and never raises
    for a call that never reserved.
    """
    reference = call_reserve_reference(call_id)
    set_org_context(session, org_id)

    # credits.release is idempotent by (org, entry_type, reference), so a SECOND
    # release returns the SAME already-written release row - not None. Testing the
    # return value alone would therefore report "we gave credits back" on every
    # replay. Look for an existing release row first (a read; services/credits.py
    # remains the only writer) and report True only for the release that actually
    # moved money.
    already_released = (
        await session.execute(
            sa.select(CreditLedgerEntry.id)
            .where(
                CreditLedgerEntry.org_id == org_id,
                CreditLedgerEntry.entry_type == "release",
                CreditLedgerEntry.reference == reference,
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    released = await credits.release(session, org_id, reference=reference)
    return released is not None and already_released is None


async def pause_campaigns_for_empty_balance(
    session: AsyncSession, org_id: uuid.UUID
) -> int:
    """Pause an org's running/scheduled SMS campaigns when its prepaid balance is empty."""
    # Imported lazily: app.services.outbound imports a lot; keeping these out of
    # module scope avoids a cycle between ai_usage and outbound.
    from app.models import OutboundCampaign
    from app.services import audit

    set_org_context(session, org_id)
    campaigns = (
        await session.execute(
            # Every channel, not only sms: a voice campaign burns AI credits too.
            sa.select(OutboundCampaign).where(
                OutboundCampaign.status.in_(("running", "scheduled")),
            )
        )
    ).scalars().all()

    paused = 0
    for campaign in campaigns:
        campaign.status = "paused"
        audit.record(
            session,
            org_id,
            action="campaign.paused",
            target_type="outbound_campaign",
            target_id=str(campaign.id),
            detail={"reason": "no_credits"},
        )
        paused += 1

    return paused


async def credits_tick(
    session: AsyncSession,
    *,
    settings: Any | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Sweeper pass for prepaid credits: stale-reserve release, low-balance warnings and
    pausing SMS campaigns when an org has no credits.

    Owns its own per-org transaction; one failed org never poisons the rest.
    """
    totals = {
        "orgs": 0,
        "reserves_released": 0,
        "warnings": 0,
        "campaigns_paused": 0,
    }

    # JUSTIFIED: a sweeper pass legitimately spans every tenant. CreditLedgerEntry is
    # tenant-scoped, so the DISTINCT org scan needs the explicit unscoped option.
    org_ids = (
        await session.execute(
            sa.select(sa.distinct(CreditLedgerEntry.org_id)).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalars().all()

    for org_id in org_ids:
        try:
            set_org_context(session, org_id)

            released = await credits.release_stale_reserves(
                session, org_id, now=now
            )

            # check_balance_warnings takes the Org ROW (not an id) and takes no
            # settings; it returns {"recorded", "level", "balance_micros"} or None.
            org_row = await session.get(Org, org_id)
            warning = (
                await check_balance_warnings(session, org_row)
                if org_row is not None
                else None
            )
            level = warning["level"] if warning else None

            if org_row is not None and level is not None:
                # Try the saved card BEFORE stopping the machine: a workspace with
                # auto-recharge on should be topped up, not paused.
                await maybe_auto_recharge(session, org_row, settings=settings)

            # Only where a prepaid gate is actually on: an org in shadow mode must never
            # have its campaigns stopped by a balance nobody is charging against.
            gated = org_row is not None and (
                bool(getattr(settings, "ai_billing_enforce", False))
                or bool(getattr(org_row, "telephony_prepaid", False))
            )
            if level == "empty" and gated:
                totals["campaigns_paused"] += (
                    await pause_campaigns_for_empty_balance(session, org_id)
                )

            await session.commit()

            totals["orgs"] += 1
            totals["reserves_released"] += released
            if level is not None:
                totals["warnings"] += 1
        except Exception:
            await session.rollback()
            logger.exception(
                "ai_usage.credits_tick_org_failed",
                org_id=str(org_id),
            )

    return totals


async def check_balance_warnings(session, org) -> dict | None:
    """Record one warning per low-balance cycle.

    This shares the ``billing.low_balance`` PlatformEvent type with
    auto-recharge because the event catalogue is Fable's to extend; ``kind``
    separates the two uses.
    """
    balance = await credits.balance(session, org.id)

    last_topup = (
        await session.execute(
            sa.select(CreditLedgerEntry)
            .where(
                CreditLedgerEntry.org_id == org.id,
                CreditLedgerEntry.entry_type == "topup",
            )
            .order_by(CreditLedgerEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_topup_micros = int(last_topup.amount_micros) if last_topup is not None else 0
    level = credits.warning_level(balance, last_topup_micros)
    if level in (None, "none"):
        return None

    last_topup_reference = last_topup.reference if last_topup is not None else "none"
    dedupe_key = f"balancewarning:{org.id}:{last_topup_reference}:{level}"

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    previous_events = (
        await session.execute(
            sa.select(PlatformEvent)
            .where(
                PlatformEvent.org_id == org.id,
                PlatformEvent.event_type == "billing.low_balance",
                PlatformEvent.created_at >= cutoff,
            )
            .order_by(PlatformEvent.created_at.desc())
            # 200 keeps our warning dedupe row in the window even when many
            # auto-recharge markers share this event type.
            .limit(200)
        )
    ).scalars().all()

    for event in previous_events:
        payload = getattr(event, "payload", {}) or {}
        # Auto-recharge writes markers with the same event type. Those must not
        # count as warning dedupe rows, or a run of recharge activity could let
        # us warn the customer twice for the same low-balance cycle.
        if payload.get("kind") == "auto_recharge":
            continue
        if payload.get("dedupe_key") == dedupe_key:
            return None

    marker = PlatformEvent(
        org_id=org.id,
        event_type="billing.low_balance",
        payload={
            "kind": "warning",
            "dedupe_key": dedupe_key,
            "balance_micros": balance,
            "level": level,
        },
    )
    session.add(marker)
    await session.flush()

    return {"recorded": True, "level": level, "balance_micros": balance}


async def maybe_auto_recharge(session, org, *, settings) -> dict | None:
    """Charge a workspace once per low-balance crossing using its saved card."""
    auto = getattr(org, "credit_auto_recharge", None)
    if not isinstance(auto, dict):
        return None
    if not auto.get("enabled"):
        return None

    threshold = auto.get("threshold_micros")
    amount = auto.get("amount_micros")
    payment_method_id = auto.get("payment_method_id")

    if not isinstance(threshold, int) or not isinstance(amount, int):
        return None
    if threshold <= 0 or amount <= 0:
        return None
    if amount < 5_000_000:
        return None

    # Import lazily to keep the module graph clean and to avoid requiring the
    # optional stripe dependency just to import ai_usage.
    from app.services import stripe_client

    if not stripe_client.is_configured(settings):
        return None

    balance = await credits.balance(session, org.id)
    if balance >= threshold:
        return None

    last_topup_reference = (
        await session.execute(
            sa.select(CreditLedgerEntry.reference)
            .where(
                CreditLedgerEntry.org_id == org.id,
                CreditLedgerEntry.entry_type == "topup",
            )
            .order_by(CreditLedgerEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    dedupe_key = (
        f"autorecharge:{org.id}:{last_topup_reference or 'none'}:{threshold}"
    )

    # Same portable Python-side dedupe check check_balance_warnings uses: load
    # recent billing.low_balance events for this org and compare payloads in
    # Python. Both features share this event type because the event catalogue is
    # Fable's to extend; ``kind`` is what separates them here.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    previous_events = (
        await session.execute(
            sa.select(PlatformEvent)
            .where(
                PlatformEvent.org_id == org.id,
                PlatformEvent.event_type == "billing.low_balance",
                PlatformEvent.created_at >= cutoff,
            )
            .order_by(PlatformEvent.created_at.desc())
            .limit(50)
        )
    ).scalars().all()

    for event in previous_events:
        payload = getattr(event, "payload", {}) or {}
        # Only auto_recharge markers dedupe an auto-recharge attempt. Warning
        # markers share the event type but must be ignored here.
        if payload.get("kind") != "auto_recharge":
            continue
        if payload.get("dedupe_key") == dedupe_key:
            return None

    # Write a marker before calling Stripe and flush it. On a crash mid-charge, the
    # same dedupe_key will prevent a second attempt on the next sweep.
    marker = PlatformEvent(
        org_id=org.id,
        event_type="billing.low_balance",
        payload={
            "kind": "auto_recharge",
            "dedupe_key": dedupe_key,
            "threshold_micros": threshold,
            "amount_micros": amount,
            "payment_method_id": str(payment_method_id),
        },
    )
    session.add(marker)
    await session.flush()

    try:
        pm_uuid = uuid.UUID(str(payment_method_id))
    except (TypeError, ValueError):
        return None

    pm = (
        await session.execute(
            sa.select(PaymentMethod).where(
                PaymentMethod.org_id == org.id,
                PaymentMethod.id == pm_uuid,
            )
        )
    ).scalar_one_or_none()

    if pm is None:
        logger.warning(
            "auto_recharge_payment_method_missing",
            org_id=str(org.id),
            payment_method_id=str(payment_method_id),
        )
        return None

    result = await stripe_client.charge_off_session(
        settings,
        org=org,
        amount_micros=amount,
        payment_method_id=pm.stripe_payment_method_id,
        customer_id=pm.stripe_customer_id,
        idempotency_key=dedupe_key,
    )

    if result.get("status") == "succeeded":
        # Do NOT credit here. The Stripe webhook is the one place a top-up becomes
        # credit, and it is idempotent on the payment intent id. Crediting here too
        # would double-credit the workspace.
        return {
            "charged": True,
            "intent_id": result.get("id", ""),
            "amount_micros": amount,
        }

    reason = result.get("reason") or "We could not charge this card."
    logger.warning(
        "auto_recharge_failed",
        org_id=str(org.id),
        reason=reason,
    )
    return {"charged": False, "reason": reason}


async def usage_summary(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Customer-facing AI usage grouped by metric. Never exposes cost."""

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(
                AiUsageEvent.metric,
                sa.func.coalesce(sa.func.sum(AiUsageEvent.quantity), 0).label(
                    "quantity"
                ),
                sa.func.coalesce(
                    sa.func.sum(AiUsageEvent.price_micros), 0
                ).label("price_micros"),
            )
            .where(
                AiUsageEvent.occurred_at >= start,
                AiUsageEvent.occurred_at < end,
            )
            .group_by(AiUsageEvent.metric)
            .order_by(AiUsageEvent.metric)
        )
    ).all()

    return [
        {
            "metric": row.metric,
            "quantity": int(row.quantity),
            "price_micros": int(row.price_micros),
        }
        for row in rows
    ]


async def usage_for_call(
    session: AsyncSession,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
) -> dict:
    """Customer-facing AI usage for one call. Never exposes cost."""

    set_org_context(session, org_id)
    events = (
        await session.execute(
            sa.select(AiUsageEvent)
            .where(AiUsageEvent.call_id == call_id)
            .order_by(AiUsageEvent.occurred_at)
        )
    ).scalars().all()

    total_price = sum(int(event.price_micros) for event in events)
    return {
        "call_id": str(call_id),
        "events": [
            {
                "provider": event.provider,
                "kind": event.kind,
                "metric": event.metric,
                "quantity": int(event.quantity),
                "price_micros": int(event.price_micros),
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in events
        ],
        "price_micros": int(total_price),
    }


def _utc_day(value: datetime) -> date:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.date()


async def margin_report(
    session: AsyncSession,
    org_id: uuid.UUID | None = None,
    *,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Platform-ops-only AI margin by UTC day."""

    if end - start > timedelta(days=MAX_MARGIN_DAYS):
        raise ValidationFailedError("Choose a shorter date range.")

    stmt = (
        sa.select(
            AiUsageEvent.occurred_at,
            AiUsageEvent.cost_micros,
            AiUsageEvent.price_micros,
        )
        .where(
            AiUsageEvent.occurred_at >= start,
            AiUsageEvent.occurred_at < end,
        )
        .order_by(AiUsageEvent.occurred_at)
        .limit(MAX_MARGIN_ROWS + 1)
    )

    if org_id is not None:
        set_org_context(session, org_id)
    else:
        # JUSTIFIED: platform-ops margin report spans every org. The route enforces
        # platform-operator auth before calling this service.
        stmt = stmt.execution_options(**{ALLOW_UNSCOPED_KEY: True})

    rows = (await session.execute(stmt)).all()

    # A SQL-side rollup grouped by UTC day is the right fix once this report is
    # used at scale. The explicit limit keeps a broad request from unbounded
    # Python work.
    if len(rows) > MAX_MARGIN_ROWS:
        logger.warning(
            "ai_usage.margin_report_truncated",
            start=start.isoformat(),
            end=end.isoformat(),
            max_rows=MAX_MARGIN_ROWS,
        )
        rows = rows[:MAX_MARGIN_ROWS]

    by_day: dict[date, dict[str, int]] = {}
    for occurred_at, cost_micros, price_micros in rows:
        day = _utc_day(occurred_at)
        bucket = by_day.setdefault(
            day, {"cost_micros": 0, "price_micros": 0}
        )
        bucket["cost_micros"] += int(cost_micros)
        bucket["price_micros"] += int(price_micros)

    return [
        {
            "day": day.isoformat(),
            "cost_micros": values["cost_micros"],
            "price_micros": values["price_micros"],
            "margin_micros": values["price_micros"] - values["cost_micros"],
        }
        for day, values in sorted(by_day.items())
    ]
