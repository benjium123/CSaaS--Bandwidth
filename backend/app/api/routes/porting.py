"""P44f: port numbers in, watch port-outs, lock numbers. Customer routes + operator review.

P1: the workspace can fix a request the carrier refused (PATCH) or stop one that has not
reached its date (cancel). Customer responses never name our carrier, never carry the
port-out PIN and never carry a storage key.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from app.auth.deps import (
    OperatorContext,
    OrgContext,
    require_operator_permission,
    require_permission,
    requires_feature,
)
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import NotFoundError, ValidationFailedError
from app.models import OrgNumber, PortRequest
from app.services import audit as audit_svc
from app.services import porting as porting_svc

router = APIRouter(prefix="/api/v1/ports", tags=["porting"])
ops_router = APIRouter(prefix="/api/v1/ops/ports", tags=["ops-porting"])

Reader = Annotated[OperatorContext, Depends(require_operator_permission("ops:read"))]
Kyc = Annotated[OperatorContext, Depends(require_operator_permission("ops:kyc"))]

#: The only fields a workspace may see on its own request. NEVER the PIN (secret_enc) and
#: never a media key.
EDITABLE_FIELDS = (
    "authorized_name",
    "business_name",
    "account_number",
    "billing_number",
    "service_address",
)

_CARRIER_NAMES = re.compile(r"telnyx|signalwire", re.IGNORECASE)


def _no_carrier(text: str) -> str:
    """A workspace never learns which of OUR carriers we use."""
    return _CARRIER_NAMES.sub("the carrier", text or "")


def _for_customer(port: dict) -> dict:
    """Strip the carrier and every operator-only word before a workspace sees this."""
    port.pop("carrier", None)
    events = []
    for event in port.get("events") or []:
        text = str(event.get("text") or "")
        if text.startswith("Approved - file it in"):
            # An operator instruction for the losing carrier's dashboard, not for the
            # customer: say what happened instead of where to click.
            text = "Approved - filing with the carrier"
        else:
            text = _no_carrier(text)
        events.append({**event, "text": text})
    port["events"] = events
    if port.get("last_error"):
        port["last_error"] = _no_carrier(str(port["last_error"]))
    return port


def _public(p: PortRequest) -> dict:
    details = dict(p.details or {})
    return {
        "id": str(p.id),
        "org_id": str(p.org_id),
        "direction": p.direction,
        "carrier": p.carrier,
        "numbers": p.numbers or [],
        "status": p.status,
        "foc_date": p.foc_date,
        "last_error": p.last_error,
        "customer_reason": details.get("customer_reason"),
        "authorized_name": details.get("authorized_name"),
        "business_name": details.get("business_name"),
        "account_number": details.get("account_number"),
        "billing_number": details.get("billing_number"),
        "service_address": details.get("service_address"),
        "can_edit": p.status in porting_svc.EDITABLE,
        "can_cancel": p.status in porting_svc.CANCELLABLE,
        "manual": bool(details.get("manual")),
        "events": p.events or [],
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def _org_port(session, org_id: uuid.UUID, port_id: uuid.UUID) -> PortRequest:  # noqa: ANN001
    """The workspace's own port request - another org's is a 404, not a leak."""
    set_org_context(session, org_id)
    port = (
        await session.execute(sa.select(PortRequest).where(PortRequest.id == port_id))
    ).scalar_one_or_none()
    if port is None:
        raise NotFoundError("Port request not found")
    return port


class CheckIn(BaseModel):
    numbers: list[str] = Field(min_length=1, max_length=porting_svc.MAX_NUMBERS)


@router.post("/check", dependencies=[Depends(requires_feature("porting"))])
async def check(
    payload: CheckIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    numbers = porting_svc.normalize_numbers(payload.numbers)
    results = await porting_svc.portability_check(
        getattr(request.app.state, "carriers", None), numbers
    )
    return {"results": results}


@router.get("")
async def list_ports(
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    set_org_context(ctx.session, ctx.org.id)
    rows = (
        await ctx.session.execute(sa.select(PortRequest).order_by(PortRequest.created_at.desc()))
    ).scalars().all()
    return {"ports": [_for_customer(_public(p)) for p in rows]}


async def _doc(form, name: str) -> tuple[bytes, str]:  # noqa: ANN001
    upload = form.get(name)
    if not isinstance(upload, UploadFile):
        raise ValidationFailedError(f"Attach the {name.upper()} (PDF, PNG or JPEG)")
    data = await upload.read(porting_svc.MAX_DOC_BYTES + 1)
    ctype = (upload.content_type or "").split(";")[0].strip().lower()
    return data, ctype


async def _optional_doc(form, name: str) -> tuple[bytes, str] | None:  # noqa: ANN001
    """A replacement document, or None when the customer did not send one."""
    upload = form.get(name)
    if not isinstance(upload, UploadFile):
        return None
    data = await upload.read(porting_svc.MAX_DOC_BYTES + 1)
    ctype = (upload.content_type or "").split(";")[0].strip().lower()
    return data, ctype


_PORT_FIELDS = (
    "authorized_name",
    "business_name",
    "account_number",
    "pin",
    "billing_number",
    "service_street",
    "service_extended",
    "service_city",
    "service_state",
    "service_zip",
)


def _fields(form) -> dict:  # noqa: ANN001
    return {k: str(form.get(k) or "") for k in _PORT_FIELDS}


@router.post("", status_code=201, dependencies=[Depends(requires_feature("porting"))])
async def create_port(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    form = await request.form()
    raw_numbers = str(form.get("numbers") or "").replace("\n", ",").split(",")
    port = await porting_svc.create_port_in(
        ctx.session,
        request.app.state.settings,
        request.app.state.media_store,
        ctx.org.id,
        user_id=ctx.actor_user_id,
        carrier=str(form.get("carrier") or "telnyx").strip().lower(),
        numbers=raw_numbers,
        form=_fields(form),
        loa=await _doc(form, "loa"),
        invoice=await _doc(form, "invoice"),
        registry=getattr(request.app.state, "carriers", None),
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="porting.requested",
        target_type="port_request",
        target_id=str(port.id),
        actor_user_id=ctx.actor_user_id,
        detail={"numbers": port.numbers},
    )
    await ctx.session.commit()
    return _for_customer(_public(port))


@router.patch("/{port_id}", dependencies=[Depends(requires_feature("porting"))])
async def update_port(
    port_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    """P1: fix a request that was rejected or that the carrier asked about, and resubmit it.

    The numbers are not editable here - cancel and start a new request for those.
    """
    port = await _org_port(ctx.session, ctx.org.id, port_id)
    form = await request.form()
    port = await porting_svc.update_port_in(
        ctx.session,
        request.app.state.settings,
        request.app.state.media_store,
        getattr(request.app.state, "carriers", None),
        port,
        form=_fields(form),
        loa=await _optional_doc(form, "loa"),
        invoice=await _optional_doc(form, "invoice"),
        user_id=ctx.actor_user_id,
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="porting.updated",
        target_type="port_request",
        target_id=str(port.id),
        actor_user_id=ctx.actor_user_id,
        detail={"status": port.status},
    )
    await ctx.session.commit()
    return _for_customer(_public(port))


@router.post("/{port_id}/cancel", dependencies=[Depends(requires_feature("porting"))])
async def cancel_port(
    port_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    """P1: stop a transfer that has not reached its date yet."""
    port = await _org_port(ctx.session, ctx.org.id, port_id)
    port = await porting_svc.cancel_port_in(
        ctx.session,
        getattr(request.app.state, "carriers", None),
        port,
        user_id=ctx.actor_user_id,
        settings=request.app.state.settings,
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="porting.cancelled",
        target_type="port_request",
        target_id=str(port.id),
        actor_user_id=ctx.actor_user_id,
        detail={"numbers": port.numbers},
    )
    await ctx.session.commit()
    return _for_customer(_public(port))


class LockIn(BaseModel):
    locked: bool


@router.post("/lock/{number_id}")
async def lock_number(
    number_id: uuid.UUID,
    payload: LockIn,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    set_org_context(ctx.session, ctx.org.id)
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None:
        raise NotFoundError("Number not found")
    number.port_locked = payload.locked
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="number.port_locked" if payload.locked else "number.port_unlocked",
        target_type="number",
        target_id=str(number.id),
        actor_user_id=ctx.actor_user_id,
        detail={"e164": number.e164},
    )
    await ctx.session.commit()
    return {"id": str(number.id), "port_locked": number.port_locked}


# ---------------------------------------------------------------- operator review


@ops_router.get("")
async def ops_list(op: Reader, status: str | None = None) -> dict:
    stmt = sa.select(PortRequest).order_by(PortRequest.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(PortRequest.status == status)
    rows = (
        await op.session.execute(stmt.execution_options(**{ALLOW_UNSCOPED_KEY: True}))
    ).scalars().all()
    return {"ports": [_public(p) for p in rows]}


async def _port(op: OperatorContext, port_id: uuid.UUID) -> PortRequest:
    port = (
        await op.session.execute(
            sa.select(PortRequest)
            .where(PortRequest.id == port_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if port is None:
        raise NotFoundError("Port request not found")
    set_org_context(op.session, port.org_id)
    return port


@ops_router.get("/{port_id}/documents/{kind}")
async def ops_document(port_id: uuid.UUID, kind: str, request: Request, op: Reader) -> Response:
    port = await _port(op, port_id)
    key = {"loa": port.loa_media_key, "invoice": port.invoice_media_key}.get(kind)
    if not key:
        raise NotFoundError("Document not found")
    data = await request.app.state.media_store.get(key)
    ctype = {v: k for k, v in porting_svc.ALLOWED_DOC_TYPES.items()}.get(
        key.rsplit(".", 1)[-1], "application/octet-stream"
    )
    return Response(content=data, media_type=ctype)


@ops_router.post("/{port_id}/approve")
async def ops_approve(port_id: uuid.UUID, request: Request, op: Kyc) -> dict:
    port = await _port(op, port_id)
    port = await porting_svc.approve(
        op.session,
        request.app.state.settings,
        request.app.state.media_store,
        getattr(request.app.state, "carriers", None),
        port,
        op.user.id,
    )
    return _public(port)


class RejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)


@ops_router.post("/{port_id}/reject")
async def ops_reject(port_id: uuid.UUID, payload: RejectIn, request: Request, op: Kyc) -> dict:
    port = await _port(op, port_id)
    return _public(
        await porting_svc.reject(
            op.session,
            port,
            op.user.id,
            payload.reason,
            settings=request.app.state.settings,
        )
    )


class StatusIn(BaseModel):
    status: str
    foc_date: str | None = None
    note: str = Field(default="", max_length=200)


@ops_router.post("/{port_id}/status")
async def ops_status(port_id: uuid.UUID, payload: StatusIn, request: Request, op: Kyc) -> dict:
    """Manual status for ports filed by hand (SignalWire has no porting API)."""
    port = await _port(op, port_id)
    port = await porting_svc.set_manual_status(
        op.session,
        getattr(request.app.state, "carriers", None),
        port,
        payload.status,
        foc_date=payload.foc_date,
        note=payload.note,
        settings=request.app.state.settings,
    )
    return _public(port)
