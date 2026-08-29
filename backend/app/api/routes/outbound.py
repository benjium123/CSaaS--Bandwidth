"""P11 REST surface: contact-list upload/import, SMS + dial campaigns.

RBAC reuses the existing ``campaigns:read`` / ``campaigns:manage`` permission keys
(``app.models.rbac.PERMISSIONS``) - they were declared but never wired to a route until
now, and are a better fit for "outbound campaign" than adding a new key would be (this
phase's allowed-files list does not include ``models/rbac.py``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, get_current_user, require_permission
from app.db.session import get_sessionmaker
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import (
    CAMPAIGN_CHANNELS,
    DIALER_MODES,
    ContactList,
    ContactListRow,
    DialAttempt,
    OutboundCampaign,
    OutboundSend,
    User,
)
from app.services import audit as audit_svc
from app.services import dialer as dialer_svc
from app.services import list_import as list_import_svc
from app.services import outbound as outbound_svc

router = APIRouter(prefix="/api/v1/outbound", tags=["outbound"])


def _store(request: Request):
    store = getattr(request.app.state, "media_store", None)
    if store is None:  # pragma: no cover - lifespan always sets it
        raise ValidationFailedError("Storage is not configured")
    return store


def _import_key(org_id: uuid.UUID, list_id: uuid.UUID) -> str:
    return f"org/{org_id}/imports/{list_id}/source"


# --------------------------------------------------------------------------------------
# Lists
# --------------------------------------------------------------------------------------
class ListPreviewOut(BaseModel):
    list_id: uuid.UUID
    name: str
    headers: list[str]
    preview_rows: list[dict[str, str]]
    suggested_mapping: dict[str, str]
    row_count: int


class ListOut(BaseModel):
    id: uuid.UUID
    name: str
    source_filename: str
    status: str
    total_rows: int
    accepted_count: int
    invalid_count: int
    duplicate_count: int
    dnc_count: int
    error: str | None
    created_at: datetime


def _list_out(lst: ContactList) -> ListOut:
    return ListOut(
        id=lst.id,
        name=lst.name,
        source_filename=lst.source_filename,
        status=lst.status,
        total_rows=lst.total_rows,
        accepted_count=lst.accepted_count,
        invalid_count=lst.invalid_count,
        duplicate_count=lst.duplicate_count,
        dnc_count=lst.dnc_count,
        error=lst.error,
        created_at=lst.created_at,
    )


@router.post("/lists", response_model=ListPreviewOut, status_code=201)
async def upload_list(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
    user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
) -> ListPreviewOut:
    """Step 1 (DR-8): parse for a preview + suggested mapping, and create the list row.
    The raw bytes are stashed in the object store so /commit can re-parse the SAME file."""
    filename = file.filename or "upload.csv"
    lower_filename = filename.lower()
    if not (lower_filename.endswith(".csv") or lower_filename.endswith(".xlsx")):
        # Before reading a single byte of the body - an unbounded upload of a format we
        # will refuse anyway must not cost anything.
        raise ValidationFailedError("Only .csv and .xlsx files are supported")

    data = await file.read()
    if not data:
        raise ValidationFailedError("The uploaded file is empty")
    if len(data) > list_import_svc.MAX_LIST_BYTES:
        raise ValidationFailedError(
            f"File is too large; the limit is {list_import_svc.MAX_LIST_BYTES // 1_000_000} MB"
        )

    parsed_preview = list_import_svc.preview(filename, data)
    if parsed_preview["row_count"] > list_import_svc.MAX_LIST_ROWS:
        raise ValidationFailedError(
            f"List has too many rows; the limit is {list_import_svc.MAX_LIST_ROWS}"
        )

    list_name = (name or filename.rsplit(".", 1)[0] or filename).strip()[:127] or filename
    row = ContactList(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        name=list_name,
        source_filename=filename[:255],
        status="importing",
        created_by=user.id,
    )
    ctx.session.add(row)
    await ctx.session.commit()

    store = _store(request)
    await store.put(
        _import_key(ctx.org.id, row.id), data, file.content_type or "application/octet-stream"
    )

    return ListPreviewOut(
        list_id=row.id,
        name=row.name,
        headers=parsed_preview["headers"],
        preview_rows=parsed_preview["preview_rows"],
        suggested_mapping=parsed_preview["suggested_mapping"],
        row_count=parsed_preview["row_count"],
    )


class CommitIn(BaseModel):
    mapping: dict[str, str]


@router.post("/lists/{list_id}/commit", response_model=ListOut, status_code=202)
async def commit_list(
    list_id: uuid.UUID,
    payload: CommitIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
) -> ListOut:
    """Step 2 (DR-8): confirm the column mapping and kick off the background import."""
    lst = await ctx.session.get(ContactList, list_id)
    if lst is None:
        raise NotFoundError("List not found")
    if lst.status != "importing":
        raise ConflictError(f"List is already {lst.status!r}")
    if "phone" not in payload.mapping:
        raise ValidationFailedError("mapping must include 'phone'")

    store = _store(request)
    try:
        data = await store.get(_import_key(ctx.org.id, lst.id))
    except KeyError as exc:
        raise NotFoundError("The uploaded file has expired; upload it again") from exc

    list_import_svc.spawn_import(
        get_sessionmaker(),
        list_id=lst.id,
        org_id=ctx.org.id,
        filename=lst.source_filename,
        data=data,
        mapping=payload.mapping,
    )
    return _list_out(lst)


@router.get("/lists", response_model=list[ListOut])
async def list_lists(
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:read"))],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ListOut]:
    stmt = (
        sa.select(ContactList).order_by(ContactList.created_at.desc()).limit(limit).offset(offset)
    )
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [_list_out(r) for r in rows]


@router.get("/lists/{list_id}", response_model=ListOut)
async def get_list(
    list_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:read"))],
) -> ListOut:
    lst = await ctx.session.get(ContactList, list_id)
    if lst is None:
        raise NotFoundError("List not found")
    return _list_out(lst)


class ListRowOut(BaseModel):
    id: uuid.UUID
    row_number: int
    e164: str | None
    status: str
    reason: str | None
    fields: dict
    contact_id: uuid.UUID | None


@router.get("/lists/{list_id}/rows", response_model=list[ListRowOut])
async def list_rows(
    list_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:read"))],
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ListRowOut]:
    """The per-row import report the P11 gate demands, filterable by outcome status."""
    lst = await ctx.session.get(ContactList, list_id)
    if lst is None:
        raise NotFoundError("List not found")
    stmt = (
        sa.select(ContactListRow)
        .where(ContactListRow.list_id == list_id)
        .order_by(ContactListRow.row_number.asc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(ContactListRow.status == status)
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [
        ListRowOut(
            id=r.id,
            row_number=r.row_number,
            e164=r.e164,
            status=r.status,
            reason=r.reason,
            fields=r.fields,
            contact_id=r.contact_id,
        )
        for r in rows
    ]


# --------------------------------------------------------------------------------------
# Campaigns (sms + voice)
# --------------------------------------------------------------------------------------
class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    channel: str = "sms"
    list_id: uuid.UUID
    body: str | None = None
    from_numbers: list[str] = []
    rate_per_minute: int = Field(default=6, ge=1, le=600)
    daily_cap: int = Field(default=200, ge=1)
    respect_warmup: bool = True
    start_at: datetime | None = None
    dialer_mode: str | None = None
    parallel_lines: int = Field(default=1, ge=1, le=20)
    local_presence: bool = False
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_backoff_minutes: int = Field(default=240, ge=1)


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    channel: str
    list_id: uuid.UUID
    status: str
    body: str | None
    from_numbers: list[str]
    rate_per_minute: int
    daily_cap: int
    respect_warmup: bool
    start_at: datetime | None
    dialer_mode: str | None
    parallel_lines: int
    local_presence: bool
    max_attempts: int
    retry_backoff_minutes: int
    created_at: datetime


def _campaign_out(c: OutboundCampaign) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        channel=c.channel,
        list_id=c.list_id,
        status=c.status,
        body=c.body,
        from_numbers=list(c.from_numbers or []),
        rate_per_minute=c.rate_per_minute,
        daily_cap=c.daily_cap,
        respect_warmup=c.respect_warmup,
        start_at=c.start_at,
        dialer_mode=c.dialer_mode,
        parallel_lines=c.parallel_lines,
        local_presence=c.local_presence,
        max_attempts=c.max_attempts,
        retry_backoff_minutes=c.retry_backoff_minutes,
        created_at=c.created_at,
    )


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    payload: CampaignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
    user: Annotated[User, Depends(get_current_user)],
) -> CampaignOut:
    if payload.channel not in CAMPAIGN_CHANNELS:
        raise ValidationFailedError(f"channel must be one of: {', '.join(CAMPAIGN_CHANNELS)}")
    contact_list = await ctx.session.get(ContactList, payload.list_id)
    if contact_list is None:
        raise NotFoundError("List not found")
    if payload.channel == "voice" and payload.dialer_mode not in DIALER_MODES:
        raise ValidationFailedError(f"dialer_mode must be one of: {', '.join(DIALER_MODES)}")

    campaign = await outbound_svc.create_campaign(
        ctx.session,
        ctx.org.id,
        name=payload.name.strip(),
        channel=payload.channel,
        list_id=payload.list_id,
        body=payload.body,
        from_numbers=list(payload.from_numbers),
        rate_per_minute=payload.rate_per_minute,
        daily_cap=payload.daily_cap,
        respect_warmup=payload.respect_warmup,
        start_at=payload.start_at,
        dialer_mode=payload.dialer_mode,
        parallel_lines=payload.parallel_lines,
        local_presence=payload.local_presence,
        max_attempts=payload.max_attempts,
        retry_backoff_minutes=payload.retry_backoff_minutes,
        created_by=user.id,
    )
    return _campaign_out(campaign)


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:read"))],
    channel: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[CampaignOut]:
    stmt = (
        sa.select(OutboundCampaign)
        .order_by(OutboundCampaign.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if channel:
        stmt = stmt.where(OutboundCampaign.channel == channel)
    if status:
        stmt = stmt.where(OutboundCampaign.status == status)
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [_campaign_out(r) for r in rows]


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:read"))],
) -> CampaignOut:
    campaign = await ctx.session.get(OutboundCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    return _campaign_out(campaign)


@router.post("/campaigns/{campaign_id}/start", response_model=CampaignOut)
async def start_campaign(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
) -> CampaignOut:
    campaign = await ctx.session.get(OutboundCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    if campaign.channel == "voice":
        campaign = await dialer_svc.start_dial_campaign(ctx.session, campaign)
    else:
        campaign = await outbound_svc.start_campaign(ctx.session, campaign)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        action="campaign.started",
        target_type="outbound_campaign",
        target_id=str(campaign.id),
        detail={"name": campaign.name, "channel": campaign.channel},
    )
    await ctx.session.commit()
    return _campaign_out(campaign)


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
) -> CampaignOut:
    campaign = await ctx.session.get(OutboundCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    campaign = await outbound_svc.pause_campaign(ctx.session, campaign)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        action="campaign.paused",
        target_type="outbound_campaign",
        target_id=str(campaign.id),
        detail={"name": campaign.name, "channel": campaign.channel},
    )
    await ctx.session.commit()
    return _campaign_out(campaign)


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
) -> CampaignOut:
    campaign = await ctx.session.get(OutboundCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    campaign = await outbound_svc.cancel_campaign(ctx.session, campaign)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        action="campaign.cancelled",
        target_type="outbound_campaign",
        target_id=str(campaign.id),
        detail={"name": campaign.name, "channel": campaign.channel},
    )
    await ctx.session.commit()
    return _campaign_out(campaign)


class DialAttemptOut(BaseModel):
    id: uuid.UUID
    e164: str
    status: str
    disposition: str | None
    amd_verdict: str | None
    attempts: int
    next_attempt_at: datetime | None


def _dial_attempt_out(row: DialAttempt) -> DialAttemptOut:
    return DialAttemptOut(
        id=row.id,
        e164=row.e164,
        status=row.status,
        disposition=row.disposition,
        amd_verdict=row.amd_verdict,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
    )


@router.post("/campaigns/{campaign_id}/dial-next", response_model=DialAttemptOut | None)
async def dial_next(
    campaign_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:manage"))],
) -> DialAttemptOut | None:
    """Preview mode's manual trigger (DR-10): claims and dials exactly one due row.
    Returns null when nothing was due."""
    campaign = await ctx.session.get(OutboundCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    settings = request.app.state.settings
    api = getattr(request.app.state, "livekit", None)
    bus = getattr(request.app.state, "event_bus", None)
    row = await dialer_svc.dial_next(ctx.session, api, settings, bus, campaign)
    return _dial_attempt_out(row) if row is not None else None


class ProgressOut(BaseModel):
    campaign_id: uuid.UUID
    status: str
    counts: dict[str, int]
    total: int


@router.get("/campaigns/{campaign_id}/progress", response_model=ProgressOut)
async def campaign_progress(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("campaigns:read"))],
) -> ProgressOut:
    campaign = await ctx.session.get(OutboundCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    model = DialAttempt if campaign.channel == "voice" else OutboundSend
    stmt = (
        sa.select(model.status, sa.func.count())
        .where(model.campaign_id == campaign_id)
        .group_by(model.status)
    )
    counts = dict((await ctx.session.execute(stmt)).all())
    return ProgressOut(
        campaign_id=campaign.id, status=campaign.status, counts=counts, total=sum(counts.values())
    )
