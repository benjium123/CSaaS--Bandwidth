"""P13 platform surface: API keys, outbound webhook endpoints + deliveries, audit log
read, usage + reconciliation reads.

Permissions: API keys, webhook endpoints and the audit read reuse ``org:update`` - there
is no narrower existing key for "manage this org's platform configuration" and this
phase's allowed-files list does not include ``models/rbac.py``. Usage + reconciliation
reuse ``reports:read``, matching every other read-only metrics surface in the API.

Every mutating route here writes an audit row (``services/audit.py``) in the SAME
transaction as its own commit (DR-6).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, ValidationFailedError
from app.models import ApiKey, UsageRecord, WebhookDelivery, WebhookEndpoint
from app.services import apikeys as apikeys_svc
from app.services import audit as audit_svc
from app.services import usage as usage_svc
from app.services import webhooks_out as webhooks_out_svc

router = APIRouter(prefix="/api/v1", tags=["platform"])


def _actor(ctx: OrgContext) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(actor_user_id, actor_api_key_id) - DR-6 records whichever kind authenticated."""
    return ctx.actor_user_id, (ctx.api_key.id if ctx.api_key is not None else None)


# ==================================================================================
# API keys (DR-3)
# ==================================================================================
class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreatedOut(ApiKeyOut):
    key: str  # shown ONCE


def _key_out(row: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes or []),
        status=row.status,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )


async def _get_key(ctx: OrgContext, key_id: uuid.UUID) -> ApiKey:
    row = await ctx.session.get(ApiKey, key_id)
    if row is None:
        raise NotFoundError("API key not found")
    return row


@router.post("/api-keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    payload: ApiKeyIn, ctx: Annotated[OrgContext, Depends(require_permission("org:update"))]
) -> ApiKeyCreatedOut:
    actor_user_id, actor_api_key_id = _actor(ctx)
    row, full_key = await apikeys_svc.create(
        ctx.session,
        ctx.org.id,
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        created_by=actor_user_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
    )
    return ApiKeyCreatedOut(**_key_out(row).model_dump(), key=full_key)


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> list[ApiKeyOut]:
    rows = await apikeys_svc.list_keys(ctx.session, ctx.org.id)
    return [_key_out(r) for r in rows]


@router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_api_key(
    key_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("org:update"))]
) -> ApiKeyOut:
    row = await _get_key(ctx, key_id)
    actor_user_id, actor_api_key_id = _actor(ctx)
    row = await apikeys_svc.revoke(
        ctx.session, row, actor_user_id=actor_user_id, actor_api_key_id=actor_api_key_id
    )
    return _key_out(row)


@router.post("/api-keys/{key_id}/rotate", response_model=ApiKeyCreatedOut)
async def rotate_api_key(
    key_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("org:update"))]
) -> ApiKeyCreatedOut:
    row = await _get_key(ctx, key_id)
    actor_user_id, actor_api_key_id = _actor(ctx)
    new_row, full_key = await apikeys_svc.rotate(
        ctx.session, row, actor_user_id=actor_user_id, actor_api_key_id=actor_api_key_id
    )
    return ApiKeyCreatedOut(**_key_out(new_row).model_dump(), key=full_key)


# ==================================================================================
# Webhook endpoints + deliveries (DR-4/DR-5)
# ==================================================================================
class WebhookEndpointIn(BaseModel):
    url: str = Field(min_length=1, max_length=512)
    event_types: list[str] = Field(min_length=1)


class WebhookEndpointUpdateIn(BaseModel):
    url: str | None = None
    event_types: list[str] | None = None
    status: str | None = None


class WebhookEndpointOut(BaseModel):
    id: uuid.UUID
    url: str
    event_types: list[str]
    status: str
    failure_streak: int
    created_at: datetime


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    secret: str  # shown ONCE


def _endpoint_out(row: WebhookEndpoint) -> WebhookEndpointOut:
    return WebhookEndpointOut(
        id=row.id,
        url=row.url,
        event_types=list(row.event_types or []),
        status=row.status,
        failure_streak=row.failure_streak,
        created_at=row.created_at,
    )


async def _get_endpoint(ctx: OrgContext, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    row = await ctx.session.get(WebhookEndpoint, endpoint_id)
    if row is None:
        raise NotFoundError("Webhook endpoint not found")
    return row


@router.post("/webhook-endpoints", response_model=WebhookEndpointCreatedOut, status_code=201)
async def create_webhook_endpoint(
    payload: WebhookEndpointIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> WebhookEndpointCreatedOut:
    actor_user_id, actor_api_key_id = _actor(ctx)
    row, secret = await webhooks_out_svc.create_endpoint(
        ctx.session,
        request.app.state.settings,
        ctx.org.id,
        url=payload.url,
        event_types=payload.event_types,
        created_by=actor_user_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
    )
    return WebhookEndpointCreatedOut(**_endpoint_out(row).model_dump(), secret=secret)


@router.get("/webhook-endpoints", response_model=list[WebhookEndpointOut])
async def list_webhook_endpoints(
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> list[WebhookEndpointOut]:
    rows = await webhooks_out_svc.list_endpoints(ctx.session, ctx.org.id)
    return [_endpoint_out(r) for r in rows]


@router.patch("/webhook-endpoints/{endpoint_id}", response_model=WebhookEndpointOut)
async def update_webhook_endpoint(
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointUpdateIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> WebhookEndpointOut:
    row = await _get_endpoint(ctx, endpoint_id)
    actor_user_id, actor_api_key_id = _actor(ctx)
    row = await webhooks_out_svc.update_endpoint(
        ctx.session,
        request.app.state.settings,
        row,
        url=payload.url,
        event_types=payload.event_types,
        status=payload.status,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
    )
    return _endpoint_out(row)


@router.delete("/webhook-endpoints/{endpoint_id}", status_code=204)
async def delete_webhook_endpoint(
    endpoint_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("org:update"))]
) -> None:
    row = await _get_endpoint(ctx, endpoint_id)
    actor_user_id, actor_api_key_id = _actor(ctx)
    await webhooks_out_svc.delete_endpoint(
        ctx.session, row, actor_user_id=actor_user_id, actor_api_key_id=actor_api_key_id
    )


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    created_at: datetime


def _delivery_out(row: WebhookDelivery) -> WebhookDeliveryOut:
    return WebhookDeliveryOut(
        id=row.id,
        endpoint_id=row.endpoint_id,
        event_id=row.event_id,
        event_type=row.event_type,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        created_at=row.created_at,
    )


@router.get(
    "/webhook-endpoints/{endpoint_id}/deliveries", response_model=list[WebhookDeliveryOut]
)
async def list_deliveries(
    endpoint_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
    status: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
) -> list[WebhookDeliveryOut]:
    await _get_endpoint(ctx, endpoint_id)  # 404s on a missing/other-org endpoint
    stmt = (
        sa.select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [_delivery_out(r) for r in rows]


@router.post("/webhook-deliveries/{delivery_id}/redeliver", response_model=WebhookDeliveryOut)
async def redeliver_webhook(
    delivery_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("org:update"))]
) -> WebhookDeliveryOut:
    row = await ctx.session.get(WebhookDelivery, delivery_id)
    if row is None:
        raise NotFoundError("Webhook delivery not found")
    actor_user_id, actor_api_key_id = _actor(ctx)
    row = await webhooks_out_svc.redeliver(
        ctx.session, row, actor_user_id=actor_user_id, actor_api_key_id=actor_api_key_id
    )
    return _delivery_out(row)


# ==================================================================================
# Audit log read (DR-6)
# ==================================================================================
class AuditEntryOut(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_api_key_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str | None
    detail: dict
    created_at: datetime


class AuditListOut(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None


@router.get("/audit", response_model=AuditListOut)
async def list_audit(
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    actor_user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> AuditListOut:
    rows, next_cursor = await audit_svc.list_entries(
        ctx.session,
        ctx.org.id,
        action=action,
        target_type=target_type,
        actor_user_id=actor_user_id,
        limit=limit,
        cursor=cursor,
    )
    return AuditListOut(
        items=[
            AuditEntryOut(
                id=r.id,
                actor_user_id=r.actor_user_id,
                actor_api_key_id=r.actor_api_key_id,
                action=r.action,
                target_type=r.target_type,
                target_id=r.target_id,
                detail=r.detail,
                created_at=r.created_at,
            )
            for r in rows
        ],
        next_cursor=next_cursor,
    )


# ==================================================================================
# Usage + reconciliation (DR-2)
# ==================================================================================
class UsageRecordOut(BaseModel):
    metric: str
    period_date: date
    quantity: int
    carrier_quantity: int | None


@router.get("/usage", response_model=list[UsageRecordOut])
async def get_usage(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    start: date = Query(...),
    end: date = Query(...),
) -> list[UsageRecordOut]:
    if end < start:
        raise ValidationFailedError("end must not be before start")
    stmt = (
        sa.select(UsageRecord)
        .where(
            UsageRecord.org_id == ctx.org.id,
            UsageRecord.period_date >= start,
            UsageRecord.period_date <= end,
        )
        .order_by(UsageRecord.period_date, UsageRecord.metric)
    )
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [
        UsageRecordOut(
            metric=r.metric,
            period_date=r.period_date,
            quantity=r.quantity,
            carrier_quantity=r.carrier_quantity,
        )
        for r in rows
    ]


class ReconciliationItemOut(BaseModel):
    metric: str
    ours: int
    carrier: int | None
    delta: int | None
    within_tolerance: bool | None
    verdict: str
    #: Count of billable messages still waiting on a carrier DLR (Opus review B3) - the
    #: unreconciled remainder, reported on its own rather than folded into `verdict`.
    pending_dlrs: int


class ReconciliationOut(BaseModel):
    date: date
    items: list[ReconciliationItemOut]


@router.get("/usage/reconciliation", response_model=ReconciliationOut)
async def get_reconciliation(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    date_: date = Query(..., alias="date"),
) -> ReconciliationOut:
    items = await usage_svc.reconciliation(ctx.session, ctx.org.id, date_)
    return ReconciliationOut(date=date_, items=[ReconciliationItemOut(**i) for i in items])
