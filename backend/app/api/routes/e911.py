"""P44e: 911 service addresses and binding them to numbers."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, require_permission
from app.db.base import set_org_context
from app.errors import NotFoundError
from app.models import EmergencyAddress, OrgNumber
from app.services import audit as audit_svc
from app.services import e911 as e911_svc

router = APIRouter(prefix="/api/v1/e911", tags=["e911"])


class AddressIn(BaseModel):
    label: str = Field(default="", max_length=64)
    caller_name: str = Field(min_length=2, max_length=128)
    line1: str = Field(min_length=3, max_length=128)
    line2: str = Field(default="", max_length=128)
    city: str = Field(min_length=2, max_length=64)
    state: str = Field(min_length=2, max_length=32)
    postal_code: str = Field(min_length=3, max_length=16)
    country: str = Field(default="US", min_length=2, max_length=2)


class AssignIn(BaseModel):
    address_id: uuid.UUID


def _address(a: EmergencyAddress) -> dict:
    return {
        "id": str(a.id),
        "label": a.label,
        "caller_name": a.caller_name,
        "line1": a.line1,
        "line2": a.line2,
        "city": a.city,
        "state": a.state,
        "postal_code": a.postal_code,
        "country": a.country,
        "status": a.status,
        "last_error": a.last_error,
    }


def _number(n: OrgNumber) -> dict:
    return {
        "id": str(n.id),
        "e164": n.e164,
        "emergency_address_id": str(n.emergency_address_id) if n.emergency_address_id else None,
        "e911_status": n.e911_status,
        "e911_error": n.e911_error,
    }


def _refused(exc: e911_svc.CarrierAddressRefused) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "address_not_verified",
                "message": str(exc),
                "suggestions": exc.suggestions,
            }
        },
    )


@router.get("/addresses")
async def list_addresses(
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> dict:
    set_org_context(ctx.session, ctx.org.id)
    rows = (
        await ctx.session.execute(
            sa.select(EmergencyAddress).order_by(EmergencyAddress.created_at)
        )
    ).scalars().all()
    numbers = (
        await ctx.session.execute(
            sa.select(OrgNumber).where(OrgNumber.is_active.is_(True)).order_by(OrgNumber.e164)
        )
    ).scalars().all()
    return {"addresses": [_address(a) for a in rows], "numbers": [_number(n) for n in numbers]}


@router.post("/addresses", status_code=201)
async def create_address(
    payload: AddressIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
):
    registry = getattr(request.app.state, "carriers", None)
    try:
        address = await e911_svc.create_address(
            ctx.session, registry, ctx.org.id, payload.model_dump(), user_id=ctx.actor_user_id
        )
    except e911_svc.CarrierAddressRefused as exc:
        await ctx.session.rollback()
        return _refused(exc)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="e911.address_created",
        target_type="emergency_address",
        target_id=str(address.id),
        actor_user_id=ctx.actor_user_id,
        detail={"city": address.city, "state": address.state},
    )
    await ctx.session.commit()
    return _address(address)


@router.post("/numbers/{number_id}/address")
async def assign_address(
    number_id: uuid.UUID,
    payload: AssignIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
):
    set_org_context(ctx.session, ctx.org.id)
    number = await ctx.session.get(OrgNumber, number_id)
    address = await ctx.session.get(EmergencyAddress, payload.address_id)
    if number is None or address is None:
        raise NotFoundError("Number or address not found")
    registry = getattr(request.app.state, "carriers", None)
    try:
        await e911_svc.assign(ctx.session, registry, number, address)
    except e911_svc.CarrierAddressRefused as exc:
        await ctx.session.commit()  # keep the address marked invalid
        return _refused(exc)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="e911.number_assigned",
        target_type="number",
        target_id=str(number.id),
        actor_user_id=ctx.actor_user_id,
        detail={"address_id": str(address.id), "status": number.e911_status},
    )
    await ctx.session.commit()
    return _number(number)
