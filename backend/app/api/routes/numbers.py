from __future__ import annotations

import uuid
from typing import Annotated

import phonenumbers
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, ValidationFailedError
from app.models import OrgNumber

router = APIRouter(prefix="/api/v1/numbers", tags=["numbers"])


def to_e164(raw: str, region: str = "US") -> str:
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException as exc:
        raise ValidationFailedError(f"{raw!r} is not a parseable phone number") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValidationFailedError(f"{raw!r} is not a valid phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class NumberIn(BaseModel):
    e164: str = Field(min_length=3, max_length=32)
    #: Which carrier actually hosts this DID. Defaults to the deployment's primary rather
    #: than a hard-coded "bandwidth": a number recorded against a carrier that does not
    #: host it is unroutable, and the error surfaces far from the mistake.
    carrier: str | None = None


class NumberOut(BaseModel):
    id: uuid.UUID
    e164: str
    carrier: str
    is_active: bool


@router.post("", response_model=NumberOut, status_code=201)
async def add_number(
    payload: NumberIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    """Minimal seed endpoint. P4 owns real search/order/port; here numbers are entered by
    hand so P1 can send from something."""
    normalized = to_e164(payload.e164)
    registry = getattr(request.app.state, "carriers", None)
    carrier_name = payload.carrier or (registry.primary_name if registry else "") or "bandwidth"
    number = OrgNumber(
        id=uuid.uuid4(), org_id=ctx.org.id, e164=normalized, carrier=carrier_name
    )
    ctx.session.add(number)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        # e164 is globally unique: a number belongs to exactly one org, ever.
        raise ConflictError(f"{normalized} is already registered") from exc
    return NumberOut(
        id=number.id, e164=number.e164, carrier=number.carrier, is_active=number.is_active
    )


@router.get("", response_model=list[NumberOut])
async def list_numbers(
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:read"))],
) -> list[NumberOut]:
    rows = (await ctx.session.execute(sa.select(OrgNumber))).scalars().all()
    return [
        NumberOut(id=n.id, e164=n.e164, carrier=n.carrier, is_active=n.is_active) for n in rows
    ]
