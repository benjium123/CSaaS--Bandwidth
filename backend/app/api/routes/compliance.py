from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.compliance import service as svc
from app.errors import ValidationFailedError
from app.models import FEDERAL_WINDOW_END, FEDERAL_WINDOW_START, ConsentEvent, DncEntry

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

MAX_SCRUB = 500


class NumberIn(BaseModel):
    e164: str
    reason: str | None = None


class ScrubIn(BaseModel):
    numbers: list[str] = Field(min_length=1, max_length=MAX_SCRUB)


class SettingsIn(BaseModel):
    window_start: str | None = None
    window_end: str | None = None
    help_contact: str | None = None
    optout_text: str | None = None
    optin_text: str | None = None
    help_text: str | None = None
    quiet_hours_enforced: bool | None = None


class ConsentOut(BaseModel):
    id: uuid.UUID
    contact_e164: str
    channel: str
    event: str
    source: str
    keyword_matched: str | None
    created_at: datetime


def _clamp_window(value: str, floor: str, is_start: bool) -> str:
    """An org may NARROW the federal window; it may never widen it."""
    try:
        hh, _, mm = value.partition(":")
        hours, minutes = int(hh), int(mm)
        fh, _, fm = floor.partition(":")
        floor_h, floor_m = int(fh), int(fm)
    except (ValueError, AttributeError) as exc:
        raise ValidationFailedError("Window times must be HH:MM") from exc

    if is_start and (hours, minutes) < (floor_h, floor_m):
        return floor
    if not is_start and (hours, minutes) > (floor_h, floor_m):
        return floor
    return f"{hours:02d}:{minutes:02d}"


@router.get("/consent", response_model=list[ConsentOut])
async def list_consent(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
    contact: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[ConsentOut]:
    stmt = sa.select(ConsentEvent).order_by(ConsentEvent.created_at.desc()).limit(limit)
    if contact:
        stmt = stmt.where(ConsentEvent.contact_e164 == to_e164(contact))
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [
        ConsentOut(
            id=r.id,
            contact_e164=r.contact_e164,
            channel=r.channel,
            event=r.event,
            source=r.source,
            keyword_matched=r.keyword_matched,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/optout", status_code=201)
async def opt_out(
    payload: NumberIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> dict:
    e164 = to_e164(payload.e164)
    await svc.record_consent(
        ctx.session,
        ctx.org.id,
        contact_e164=e164,
        event="opt_out",
        source="manual",
        actor_user_id=ctx.actor_user_id,
        details={"reason": payload.reason} if payload.reason else {},
    )
    await ctx.session.commit()
    return {"contact_e164": e164, "opted_out": True}


@router.post("/optin", status_code=201)
async def opt_in(
    payload: NumberIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> dict:
    """Refused (409) when the standing opt-out came from a keyword.

    Only the consumer's own START reverses their own STOP.
    """
    e164 = to_e164(payload.e164)
    await svc.manual_opt_in(ctx.session, ctx.org.id, e164, ctx.actor_user_id)
    await ctx.session.commit()
    return {"contact_e164": e164, "opted_out": False}


@router.get("/dnc")
async def list_dnc(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[dict]:
    rows = (
        await ctx.session.execute(sa.select(DncEntry).order_by(DncEntry.created_at.desc()))
    ).scalars().all()
    return [
        {"id": r.id, "e164": r.e164, "source": r.source, "reason": r.reason} for r in rows
    ]


@router.post("/dnc", status_code=201)
async def add_dnc(
    payload: NumberIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> dict:
    e164 = to_e164(payload.e164)
    entry = await svc.add_dnc(
        ctx.session,
        ctx.org.id,
        e164,
        reason=payload.reason,
        actor_user_id=ctx.actor_user_id,
    )
    await ctx.session.commit()
    return {"id": entry.id, "e164": entry.e164}


@router.delete("/dnc/{e164}", status_code=204)
async def remove_dnc(
    e164: str,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> None:
    await svc.remove_dnc(ctx.session, ctx.org.id, to_e164(e164), ctx.actor_user_id)
    await ctx.session.commit()


@router.post("/scrub")
async def scrub(
    payload: ScrubIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> dict:
    """Check numbers against opt-out state and the internal DNC list.

    ``federal_checked`` is ALWAYS false and every result carries
    ``federal_dnc:unchecked`` - we hold no registry subscription and never pretend to.
    This is also P11's import-time scrub.
    """
    results = await svc.scrub(ctx.session, ctx.org.id, payload.numbers)
    return {
        "federal_dnc_checked": False,
        "results": [
            {
                "e164": r.e164,
                "ok": r.ok,
                "reasons": r.reasons,
                "federal_checked": r.federal_checked,
            }
            for r in results
        ],
    }


@router.get("/settings")
async def get_settings(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> dict:
    s = await svc.get_settings(ctx.session, ctx.org.id)
    await ctx.session.commit()
    return {
        "window_start": s.window_start,
        "window_end": s.window_end,
        "help_contact": s.help_contact,
        "optout_text": s.optout_text,
        "optin_text": s.optin_text,
        "help_text": s.help_text,
        "quiet_hours_enforced": s.quiet_hours_enforced,
        "federal_window": {"start": FEDERAL_WINDOW_START, "end": FEDERAL_WINDOW_END},
    }


@router.patch("/settings")
async def patch_settings(
    payload: SettingsIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> dict:
    s = await svc.get_settings(ctx.session, ctx.org.id)
    if payload.window_start is not None:
        s.window_start = _clamp_window(payload.window_start, FEDERAL_WINDOW_START, True)
    if payload.window_end is not None:
        s.window_end = _clamp_window(payload.window_end, FEDERAL_WINDOW_END, False)
    for field in ("help_contact", "optout_text", "optin_text", "help_text"):
        value = getattr(payload, field)
        if value is not None:
            setattr(s, field, value)
    if payload.quiet_hours_enforced is not None:
        s.quiet_hours_enforced = payload.quiet_hours_enforced
    await ctx.session.commit()
    return {
        "window_start": s.window_start,
        "window_end": s.window_end,
        "quiet_hours_enforced": s.quiet_hours_enforced,
    }
