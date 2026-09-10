"""P13: analytics dashboard overview + transcript search (DR-7/DR-10).

Both reuse ``reports:read`` - the existing permission key for every other read-only
metrics surface in the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.auth.deps import OrgContext, require_permission
from app.errors import ValidationFailedError
from app.services import analytics as analytics_svc
from app.services import inbox_access as inbox_access_svc
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


@router.get("/analytics/overview", response_model=OverviewOut)
async def analytics_overview(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    days: int = Query(14, ge=1, le=90),
) -> OverviewOut:
    return OverviewOut(**await analytics_svc.overview(ctx.session, ctx.org.id, days))


@router.get("/analytics/assistant", response_model=AssistantAnalyticsOut)
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


@router.get("/search/transcripts", response_model=list[TranscriptSearchResultOut])
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
