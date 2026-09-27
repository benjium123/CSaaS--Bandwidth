"""Live captions: the agents/live_captions.py worker transcribes a call sentence by sentence
and shows it in the softphone while the call is on.

It prefers the self-hosted stt worker. When the box is too busy it overflows to Deepgram, but
only while the org is under its monthly overflow cap; Deepgram seconds are recorded as OUR
cost (ai_usage source "platform", never charged to the customer)."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog

from app.db.base import set_org_context

log = structlog.get_logger("live_captions")

#: ai_usage idempotency key prefix for overflow seconds; the monthly cap sums these.
USAGE_KEY_PREFIX = "live-stt:"


def enabled(settings) -> bool:  # noqa: ANN001
    return bool(getattr(settings, "live_captions_agent_name", ""))


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def overflow_seconds_this_month(session, org_id, now: datetime | None = None) -> int:  # noqa: ANN001
    from app.models import AiUsageEvent

    now = now or datetime.now(timezone.utc)
    set_org_context(session, org_id)
    total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(AiUsageEvent.quantity), 0)).where(
                AiUsageEvent.provider == "deepgram",
                AiUsageEvent.idempotency_key.startswith(USAGE_KEY_PREFIX),
                AiUsageEvent.occurred_at >= _month_start(now),
            )
        )
    ).scalar_one()
    return int(total or 0)


async def overflow_allowed(session, settings, org_id, now: datetime | None = None) -> bool:  # noqa: ANN001
    if not settings.deepgram_api_key.get_secret_value().strip():
        return False
    cap = int(getattr(settings, "live_overflow_max_minutes", 0)) * 60
    if cap <= 0:
        return False
    return await overflow_seconds_this_month(session, org_id, now) < cap


async def dispatch(session, api, settings, call) -> bool:  # noqa: ANN001
    """Put the live-captions worker into this call's room. Never raises."""
    extra = call.extra or {}
    if api is None or not enabled(settings):
        return False
    if extra.get("via") != "livekit" or not extra.get("room") or extra.get("live_captions"):
        return False
    overflow = "deepgram" if await overflow_allowed(session, settings, call.org_id) else "none"
    metadata = json.dumps(
        {"call_id": str(call.id), "org_id": str(call.org_id), "overflow": overflow}
    )
    try:
        await api.create_agent_dispatch(
            room=extra["room"], agent_name=settings.live_captions_agent_name, metadata=metadata
        )
    except Exception:  # noqa: BLE001 - captions must never break a call
        log.exception("live_captions_dispatch_failed", call_id=str(call.id))
        return False
    set_org_context(session, call.org_id)
    call.extra = {**(call.extra or {}), "live_captions": True}
    return True


async def meter_overflow(session, call, seconds: int) -> None:  # noqa: ANN001
    """Record the Deepgram seconds one call's captions used, once per call."""
    from app.services import ai_usage

    if seconds <= 0:
        return
    try:
        await ai_usage.record(
            session,
            call.org_id,
            provider="deepgram",
            kind="stt",
            metric="stt_seconds",
            quantity=max(1, math.ceil(seconds)),
            source="platform",
            idempotency_key=f"{USAGE_KEY_PREFIX}{call.id}",
        )
    except Exception:  # noqa: BLE001 - metering must never break the call teardown
        log.warning("live_captions_meter_failed", call_id=str(call.id))
