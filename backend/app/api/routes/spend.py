from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.auth.deps import OrgContext, require_permission
from app.errors import ValidationFailedError
from app.services import audit as audit_svc
from app.services import spend as spend_svc

router = APIRouter(prefix="/api/v1", tags=["spend"])


class ProviderRateIn(BaseModel):
    provider: str
    metric: str
    unit_cost_micros: int


class ProviderRatesPut(BaseModel):
    rates: list[ProviderRateIn]


def _default_range(
    start: date | None, end: date | None
) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    if start is None:
        start = today.replace(day=1)
    if end is None:
        end = today
    if start > end:
        raise ValidationFailedError("from must be on or before to")
    if (end - start).days > 366:
        raise ValidationFailedError("Date range must not exceed 366 days")
    return start, end


@router.get("/spend/summary")
async def get_spend_summary(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict:
    start, end = _default_range(start, end)
    data = await spend_svc.summary(ctx.session, ctx.org.id, start, end)
    data["total_usd"] = f"{data['total_micros'] / 1_000_000:.2f}"
    return data


@router.get("/spend/daily")
async def get_spend_daily(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    provider: str | None = None,
) -> list[dict]:
    start, end = _default_range(start, end)
    return await spend_svc.daily(ctx.session, ctx.org.id, start, end, provider=provider)


@router.get("/provider-rates")
async def get_provider_rates(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[dict]:
    return await spend_svc.effective_rates(ctx.session)


@router.put("/provider-rates")
async def put_provider_rates(
    payload: ProviderRatesPut,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> list[dict]:
    updated = await spend_svc.upsert_rates(
        ctx.session,
        [r.model_dump() for r in payload.rates],
        org_id=ctx.org.id,
    )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="provider_rates.updated",
        target_type="provider_rate",
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "rates": [
                {
                    "provider": r.provider,
                    "metric": r.metric,
                    "unit_cost_micros": r.unit_cost_micros,
                }
                for r in updated
            ]
        },
    )
    await ctx.session.commit()

    return [
        {
            "provider": r.provider,
            "metric": r.metric,
            "unit_cost_micros": r.unit_cost_micros,
            "currency": r.currency,
        }
        for r in updated
    ]


@router.post("/spend/rollup")
async def post_spend_rollup(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    day: Annotated[date, Query(description="UTC day to recompute")],
) -> dict:
    today = datetime.now(timezone.utc).date()
    if day > today:
        raise ValidationFailedError("day must not be in the future")
    if (today - day).days > 400:
        raise ValidationFailedError("day must be within the last 400 days")

    rows_written = await spend_svc.rollup_day(ctx.session, ctx.org.id, day)
    return {"day": day.isoformat(), "rows_written": rows_written}
