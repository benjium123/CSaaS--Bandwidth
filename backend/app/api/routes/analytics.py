"""P13: analytics dashboard overview + transcript search (DR-7/DR-10).

Both reuse ``reports:read`` - the existing permission key for every other read-only
metrics surface in the API.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.auth.deps import OrgContext, require_permission, requires_feature
from app.errors import PermissionDeniedError, ValidationFailedError
from app.models import User
from app.services import analytics as analytics_svc
from app.services import delivery_digest as delivery_digest_svc
from app.services import inbox_access as inbox_access_svc
from app.services import mailer
from app.services import messaging_health as messaging_health_svc
from app.services import search as search_svc

router = APIRouter(prefix="/api/v1", tags=["analytics"])


# ==================================================================================
# Overview (DR-10). Real response models (Opus review item 7) - a bare `dict` return
# type leaves the generated frontend types as `unknown`.
# ==================================================================================
class OverviewRangeOut(BaseModel):
    start: str
    end: str
    days: int


class MessagesSeriesPointOut(BaseModel):
    date: str
    inbound: int
    outbound: int
    delivery_rate: float | None


class CallsSeriesPointOut(BaseModel):
    date: str
    calls: int
    avg_duration_seconds: float | None


class CampaignProgressPointOut(BaseModel):
    status: str
    count: int


class AiSeriesPointOut(BaseModel):
    date: str
    turns: int
    handoffs: int


class OverviewOut(BaseModel):
    range: OverviewRangeOut
    messages: list[MessagesSeriesPointOut]
    calls: list[CallsSeriesPointOut]
    campaigns: list[CampaignProgressPointOut]
    ai: list[AiSeriesPointOut]
    spend_usd_month_to_date: float


class AssistantAnalyticsOut(BaseModel):
    calls: int
    minutes: float
    answer_rate: float | None
    handoff_rate: float | None
    booked: int
    avg_duration_seconds: float | None
    cost_micros: None
    range: OverviewRangeOut


@router.get(
    "/analytics/overview",
    response_model=OverviewOut,
    dependencies=[Depends(requires_feature("analytics"))],
)
async def analytics_overview(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    days: int = Query(14, ge=1, le=90),
) -> OverviewOut:
    return OverviewOut(**await analytics_svc.overview(ctx.session, ctx.org.id, days))


@router.get(
    "/analytics/assistant",
    response_model=AssistantAnalyticsOut,
    dependencies=[Depends(requires_feature("analytics"))],
)
async def analytics_assistant(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    from_: str = Query("", alias="from"),
    to: str = Query("", alias="to"),
    profile_id: uuid.UUID | None = Query(None),
) -> AssistantAnalyticsOut:
    if not from_ and not to:
        start, end = analytics_svc._day_bounds(30)
    else:
        if not from_ or not to:
            raise ValidationFailedError("Give the dates as YYYY-MM-DD.")
        try:
            start = datetime.strptime(from_, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end = datetime.strptime(to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValidationFailedError("Give the dates as YYYY-MM-DD.") from exc
        end = end + timedelta(days=1)
        if end <= start:
            raise ValidationFailedError("The end date must be after the start date.")

    summary = await analytics_svc.assistant_summary(
        ctx.session, ctx.org.id, start=start, end=end, profile_id=profile_id
    )
    range_out = OverviewRangeOut(
        start=start.date().isoformat(),
        end=(end - timedelta(days=1)).date().isoformat(),
        days=(end - start).days,
    )
    return AssistantAnalyticsOut(**summary, range=range_out)


# ==================================================================================
# Transcript search (DR-7)
# ==================================================================================
class TranscriptSegmentOut(BaseModel):
    role: str
    text: str
    at_ms: int
    matched: bool


class TranscriptSearchResultOut(BaseModel):
    call_id: str
    contact_e164: str
    started_at: str
    segments: list[TranscriptSegmentOut]


@router.get(
    "/search/transcripts",
    response_model=list[TranscriptSearchResultOut],
    dependencies=[Depends(requires_feature("analytics"))],
)
async def search_transcripts(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
) -> list[TranscriptSearchResultOut]:
    # P15 (5.3): scope to numbers this caller may view - unfiltered, this returned
    # transcript content across every inbox in the org.
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    allowed_e164s = None if access.is_admin else (access.member_e164s | access.viewer_e164s)
    results = await search_svc.search_transcripts(
        ctx.session, ctx.org.id, q, limit=limit, allowed_e164s=allowed_e164s
    )
    return [TranscriptSearchResultOut(**r) for r in results]


# ==================================================================================
# P41 messaging health. Same ``reports:read`` key as the overview above - this is one
# more read-only metrics surface, not a new kind of permission.
# ==================================================================================
class MessagingHealthOut(BaseModel):
    window_start: date
    window_end: date
    volume: int
    delivery_rate: float | None
    spam_block_rate: float | None
    opt_out_rate: float | None
    failed_by_class: dict[str, int]
    level: str
    reasons: list[str]
    thresholds: dict[str, float]


@router.get(
    "/analytics/health",
    response_model=MessagingHealthOut,
    dependencies=[Depends(requires_feature("analytics"))],
)
async def analytics_messaging_health(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    days: int = Query(7, ge=1, le=90),
) -> MessagingHealthOut:
    return MessagingHealthOut(
        **await messaging_health_svc.health(ctx.session, ctx.org.id, days=days)
    )


# ==================================================================================
# D2 daily delivery digest. Reading the config is a read-only surface (settings:read);
# changing it changes what we send, so it sits behind settings:write. Same analytics
# feature gate as the health route above.
# ==================================================================================
class DeliveryDigestOut(BaseModel):
    enabled: bool
    hour: int
    tz: str
    recipients: list[str]
    default_recipients: list[str]
    last_sent_at: str | None


class DeliveryDigestIn(BaseModel):
    enabled: bool
    hour: int
    tz: str
    recipients: list[str] = []


async def _delivery_digest_out(ctx: OrgContext) -> DeliveryDigestOut:
    row = await delivery_digest_svc.get_schedule(ctx.session, ctx.org.id)
    view = delivery_digest_svc.schedule_view(row)
    defaults = await delivery_digest_svc.default_recipients(ctx.session, ctx.org.id)
    return DeliveryDigestOut(**view, default_recipients=defaults)


@router.get(
    "/analytics/delivery-digest",
    response_model=DeliveryDigestOut,
    dependencies=[Depends(requires_feature("analytics"))],
)
async def get_delivery_digest(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> DeliveryDigestOut:
    return await _delivery_digest_out(ctx)


@router.put(
    "/analytics/delivery-digest",
    response_model=DeliveryDigestOut,
    dependencies=[Depends(requires_feature("analytics"))],
)
async def put_delivery_digest(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    body: DeliveryDigestIn,
) -> DeliveryDigestOut:
    await delivery_digest_svc.save_schedule(
        ctx.session,
        ctx.org.id,
        enabled=body.enabled,
        hour=body.hour,
        tz=body.tz,
        recipients=body.recipients,
        user_id=ctx.actor_user_id,
    )
    return await _delivery_digest_out(ctx)


@router.post(
    "/analytics/delivery-digest/test",
    dependencies=[Depends(requires_feature("analytics"))],
)
async def post_delivery_digest_test(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> dict:
    # An API key has no inbox of its own to send to, and a key must never mail a person.
    if ctx.actor_user_id is None:
        raise PermissionDeniedError("This action needs a signed-in person, not an API key")

    today = datetime.now(timezone.utc).date()
    day: date | None = None
    digest: dict | None = None
    for offset in range(7):
        candidate = today - timedelta(days=offset)
        built = await delivery_digest_svc.build(ctx.session, ctx.org.id, candidate)
        if built is not None:
            day = candidate
            digest = built
            break
    if day is None or digest is None:
        return {"sent": False, "day": None}

    user = await ctx.session.get(User, ctx.actor_user_id)
    if user is None or not user.email:
        return {"sent": False, "day": None}

    settings = request.app.state.settings
    subject, body = delivery_digest_svc.render(
        digest,
        app_name=settings.app_name,
        day=day,
        base_url=(getattr(settings, "public_web_url", "") or ""),
    )
    # A test send never touches last_sent_at: the real digest is still owed for the day.
    sent = await mailer.send(settings, [user.email], subject, body)
    return {"sent": bool(sent), "day": day.isoformat()}
