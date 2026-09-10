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

import hmac
import uuid
from datetime import date, datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, require_permission
from app.db.base import set_org_context
from app.db.session import get_session
from app.errors import (
    FeatureUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models import ApiKey, Org, UsageRecord, WebhookDelivery, WebhookEndpoint
from app.models.billing import DEFAULT_AI_MARKUP_BPS
from app.services import apikeys as apikeys_svc
from app.services import ai_usage
from app.services import audit as audit_svc
from app.services import credits
from app.services import spend as spend_svc
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
        # C2: an API-key-authenticated caller has no actor_user_id - the service needs
        # this key's own scopes to gate the new key's scopes against.
        actor_key_scopes=list(ctx.api_key.scopes or []) if ctx.api_key is not None else None,
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


async def require_platform_operator(
    request: Request,
    x_platform_ops_token: Annotated[str | None, Header(alias="X-Platform-Ops-Token")] = None,
) -> None:
    configured = request.app.state.settings.platform_ops_token.get_secret_value().strip()
    if not configured:
        raise FeatureUnavailableError(
            "Platform operator token is not configured; status callbacks are disabled"
        )
    # C6: constant-time compare - a naive != leaks timing information an attacker can
    # use to recover the token byte-by-byte.
    if not x_platform_ops_token or not hmac.compare_digest(x_platform_ops_token, configured):
        raise PermissionDeniedError("Invalid platform operator token")


class PlatformBillingPatch(BaseModel):
    ai_markup_bps: int | None = None
    ai_platform_fee_per_minute_micros: int | None = None


class PlatformAdjustmentIn(BaseModel):
    amount_micros: int
    note: str
    entry_type: str


class PlatformRateIn(BaseModel):
    provider: str
    metric: str
    unit_cost_micros: int


class PlatformRatesPut(BaseModel):
    rates: list[PlatformRateIn]


def _platform_billing_shape(org, *, balance_micros: int, reserved_micros: int) -> dict:
    return {
        "balance_micros": balance_micros,
        "reserved_micros": reserved_micros,
        "ai_markup_bps": (
            org.ai_markup_bps
            if org.ai_markup_bps is not None
            else DEFAULT_AI_MARKUP_BPS
        ),
        "ai_platform_fee_per_minute_micros": org.ai_platform_fee_per_minute_micros,
        "ai_key_mode": org.ai_key_mode,
    }


@router.get("/platform/billing/orgs/{org_id}")
async def get_platform_billing_org(
    org_id: uuid.UUID,
    _ops: Annotated[None, Depends(require_platform_operator)],
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = await session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Org not found")

    set_org_context(session, org_id)

    balance = await credits.balance(session, org_id)
    reserved = await credits.outstanding_reserves(session, org_id)
    return _platform_billing_shape(
        org,
        balance_micros=balance,
        reserved_micros=reserved,
    )


@router.patch("/platform/billing/orgs/{org_id}")
async def patch_platform_billing_org(
    org_id: uuid.UUID,
    payload: PlatformBillingPatch,
    _ops: Annotated[None, Depends(require_platform_operator)],
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = await session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Org not found")

    set_org_context(session, org_id)

    if payload.ai_markup_bps is not None:
        if payload.ai_markup_bps < 0 or payload.ai_markup_bps > 100_000:
            raise ValidationFailedError("Markup must be between 0 and 1000 percent.")
        org.ai_markup_bps = payload.ai_markup_bps

    if payload.ai_platform_fee_per_minute_micros is not None:
        if payload.ai_platform_fee_per_minute_micros < 0:
            raise ValidationFailedError("Fee must not be negative.")
        org.ai_platform_fee_per_minute_micros = payload.ai_platform_fee_per_minute_micros

    audit_svc.record(
        session,
        org_id,
        action="platform.billing_updated",
        target_type="org",
        target_id=str(org_id),
        detail={
            "ai_markup_bps": payload.ai_markup_bps,
            "ai_platform_fee_per_minute_micros": payload.ai_platform_fee_per_minute_micros,
        },
    )
    await session.commit()

    balance = await credits.balance(session, org_id)
    reserved = await credits.outstanding_reserves(session, org_id)
    return _platform_billing_shape(
        org,
        balance_micros=balance,
        reserved_micros=reserved,
    )


@router.post("/platform/billing/orgs/{org_id}/adjustments", status_code=201)
async def post_platform_billing_adjustment(
    org_id: uuid.UUID,
    payload: PlatformAdjustmentIn,
    _ops: Annotated[None, Depends(require_platform_operator)],
    session: AsyncSession = Depends(get_session),
) -> dict:
    org = await session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Org not found")

    set_org_context(session, org_id)

    note = payload.note.strip()
    if not note:
        raise ValidationFailedError("Say why this adjustment is being made.")
    if payload.entry_type not in ("adjustment", "refund"):
        raise ValidationFailedError("Entry type must be adjustment or refund.")

    entry = await credits.adjust(
        session,
        org_id,
        payload.amount_micros,
        reference=f"ops:{uuid.uuid4()}",
        note=note,
        created_by=None,
        entry_type=payload.entry_type,
    )

    entry_id = str(entry.id)
    amount_micros = int(entry.amount_micros)
    balance_after_micros = int(entry.balance_after_micros)

    audit_svc.record(
        session,
        org_id,
        action="platform.billing_adjustment_created",
        target_type="credit_ledger",
        target_id=entry_id,
        detail={
            "amount_micros": amount_micros,
            "entry_type": payload.entry_type,
            "note": note,
        },
    )
    await session.commit()

    return {
        "id": entry_id,
        "amount_micros": amount_micros,
        "balance_after_micros": balance_after_micros,
    }


@router.put("/platform/billing/rates")
async def put_platform_billing_rates(
    payload: PlatformRatesPut,
    org_id: Annotated[uuid.UUID, Query(description="Target org for these rate rows")],
    _ops: Annotated[None, Depends(require_platform_operator)],
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    set_org_context(session, org_id)

    # A true platform-wide default row is Fable's schema call. Today operators write
    # per-org overrides, so this route requires an org_id query parameter.
    updated = await spend_svc.upsert_rates(
        session,
        [r.model_dump() for r in payload.rates],
        org_id=org_id,
    )

    result = [
        {
            "provider": r.provider,
            "metric": r.metric,
            "unit_cost_micros": int(r.unit_cost_micros),
            "currency": getattr(r, "currency", "USD"),
        }
        for r in updated
    ]

    audit_svc.record(
        session,
        org_id,
        action="platform.billing_rates_updated",
        target_type="org",
        target_id=str(org_id),
        detail={
            "rates": [
                {
                    "provider": r.provider,
                    "metric": r.metric,
                    "unit_cost_micros": r.unit_cost_micros,
                }
                for r in payload.rates
            ]
        },
    )
    await session.commit()

    return result


@router.get("/platform/billing/margin")
async def get_platform_billing_margin(
    org_id: Annotated[uuid.UUID | None, Query()] = None,
    start_date: Annotated[date, Query(alias="from")] = None,
    end_date: Annotated[date, Query(alias="to")] = None,
    _ops: Annotated[None, Depends(require_platform_operator)] = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    if start_date is None or end_date is None:
        raise ValidationFailedError("from and to are required")
    if start_date > end_date:
        raise ValidationFailedError("from must be on or before to")

    if org_id is not None:
        set_org_context(session, org_id)

    start_dt = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=timezone.utc,
    )
    end_dt = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        23,
        59,
        59,
        999999,
        tzinfo=timezone.utc,
    )

    # Operator-only surface: cost may appear here.
    return await ai_usage.margin_report(
        session,
        org_id=org_id,
        start=start_dt,
        end=end_dt,
    )
