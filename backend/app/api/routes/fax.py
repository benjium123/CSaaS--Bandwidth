"""Fax: send and list faxes, download documents, switch a number to fax mode, and the
Telnyx fax webhook (signature-verified)."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.auth.deps import OrgContext, require_permission
from app.db.base import set_org_context
from app.db.session import get_session
from app.errors import NotFoundError, UnauthenticatedError, ValidationFailedError
from app.models import Fax, OrgNumber
from app.services import fax as fax_svc

router = APIRouter(prefix="/api/v1", tags=["fax"])
log = structlog.get_logger("fax_routes")


def _public(f: Fax) -> dict:
    return {
        "id": str(f.id),
        "direction": f.direction,
        "status": f.status,
        "from": f.from_e164,
        "to": f.to_e164,
        "pages": f.page_count,
        "charged_micros": int(f.charged_micros or 0),
        "failure_reason": f.failure_reason,
        "has_document": bool(f.media_key),
        "document_name": f.media_name,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "completed_at": f.completed_at.isoformat() if f.completed_at else None,
    }


@router.get("/fax")
async def list_faxes(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> dict:
    set_org_context(ctx.session, ctx.org.id)
    rows = (
        await ctx.session.execute(sa.select(Fax).order_by(Fax.created_at.desc()).limit(200))
    ).scalars().all()
    numbers = (
        await ctx.session.execute(
            sa.select(OrgNumber).where(
                OrgNumber.released_at.is_(None), OrgNumber.status == "active"
            )
        )
    ).scalars().all()
    return {
        "faxes": [_public(f) for f in rows],
        "fax_numbers": [n.e164 for n in numbers if (n.provisioning or {}).get("fax_mode")],
        "numbers": [
            {"id": str(n.id), "e164": n.e164, "carrier": n.carrier,
             "fax_mode": bool((n.provisioning or {}).get("fax_mode"))}
            for n in numbers
        ],
    }


@router.post("/fax")
async def send_fax(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
) -> dict:
    form = await request.form()
    upload = form.get("file")
    to = str(form.get("to") or "").strip()
    from_ = str(form.get("from") or "").strip()
    if not isinstance(upload, UploadFile):
        raise ValidationFailedError("Attach a PDF or TIFF.")
    from app.api.routes.numbers import to_e164

    try:
        to_norm = to_e164(to)
    except Exception as exc:
        raise ValidationFailedError("Enter a valid fax number.") from exc
    data = await upload.read(fax_svc.MAX_BYTES + 1)
    ctype = (upload.content_type or "").split(";")[0].strip().lower()
    if ctype not in fax_svc.ALLOWED_TYPES:
        name = (upload.filename or "").lower()
        ctype = "application/pdf" if name.endswith(".pdf") else (
            "image/tiff" if name.endswith((".tif", ".tiff")) else ctype
        )
    user = getattr(ctx, "user", None)
    fax = await fax_svc.send(
        ctx.session,
        request.app.state.settings,
        request.app.state.media_store,
        ctx.org.id,
        from_e164=from_,
        to_e164=to_norm,
        data=data,
        content_type=ctype,
        filename=upload.filename or "fax.pdf",
        user_id=getattr(user, "id", None),
    )
    return _public(fax)


@router.get("/fax/{fax_id}/document")
async def fax_document(
    fax_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> Response:
    set_org_context(ctx.session, ctx.org.id)
    fax = await ctx.session.get(Fax, fax_id)
    if fax is None or fax.org_id != ctx.org.id or not fax.media_key:
        raise NotFoundError("Fax document not found")
    data = await request.app.state.media_store.get(fax.media_key)
    ctype = "application/pdf" if fax.media_key.endswith(".pdf") else "image/tiff"
    import re

    safe = re.sub(r"[^A-Za-z0-9._+-]", "_", fax.media_name or "fax")[:120] or "fax"
    return Response(
        data,
        media_type=ctype,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


class FaxModeIn(BaseModel):
    enabled: bool


@router.patch("/numbers/{number_id}/fax-mode")
async def number_fax_mode(
    number_id: uuid.UUID,
    payload: FaxModeIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    set_org_context(ctx.session, ctx.org.id)
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None or number.org_id != ctx.org.id or number.released_at is not None:
        raise NotFoundError("Number not found")
    await fax_svc.set_fax_mode(ctx.session, request.app.state.settings, number, payload.enabled)
    await ctx.session.commit()
    return {"id": str(number.id), "e164": number.e164, "fax_mode": payload.enabled}


@router.post("/webhooks/telnyx/fax")
async def telnyx_fax_webhook(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict:
    from app.providers.telnyx import webhooks as telnyx_webhooks

    settings = request.app.state.settings
    raw = await request.body()
    public_key = settings.telnyx_public_key.get_secret_value()
    if not public_key or not telnyx_webhooks.verify(request.headers, public_key, raw):
        raise UnauthenticatedError("Invalid webhook signature")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ValidationFailedError("Malformed webhook") from exc
    outcome = await fax_svc.handle_webhook(
        session, settings, request.app.state.media_store, body
    )
    log.info("telnyx_fax_webhook", outcome=outcome)
    return {"ok": True, "outcome": outcome}
