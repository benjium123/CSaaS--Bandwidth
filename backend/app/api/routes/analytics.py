"""P13: analytics dashboard overview + transcript search (DR-7/DR-10).

Both reuse ``reports:read`` - the existing permission key for every other read-only
metrics surface in the API.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.auth.deps import OrgContext, require_permission
from app.services import analytics as analytics_svc
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


@router.get("/analytics/overview", response_model=OverviewOut)
async def analytics_overview(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    days: int = Query(14, ge=1, le=90),
) -> OverviewOut:
    return OverviewOut(**await analytics_svc.overview(ctx.session, ctx.org.id, days))


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
    results = await search_svc.search_transcripts(ctx.session, ctx.org.id, q, limit=limit)
    return [TranscriptSearchResultOut(**r) for r in results]
