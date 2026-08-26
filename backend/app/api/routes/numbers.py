from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import phonenumbers
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.compliance import registration
from app.errors import (
    CarrierNotConfiguredError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import OrgNumber
from app.models.numbers import Campaign
from app.providers import numbers as numbers_api

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
    #: "local" | "tollfree". Decides which registration regime gates the number.
    number_type: str = "local"


class NumberOut(BaseModel):
    id: uuid.UUID
    e164: str
    carrier: str
    is_active: bool
    number_type: str = "local"
    status: str = "active"
    capabilities: dict = {}
    campaign_id: uuid.UUID | None = None
    #: "approved" | "pending" | "rejected" | "unknown". The console shows this so an
    #: operator learns a number cannot send BEFORE trying to send from it.
    registration: str = "unknown"
    registration_detail: str = ""


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
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        e164=normalized,
        carrier=carrier_name,
        number_type=payload.number_type,
    )
    ctx.session.add(number)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        # e164 is globally unique: a number belongs to exactly one org, ever.
        raise ConflictError(f"{normalized} is already registered") from exc
    return await _out(ctx.session, number)


@router.get("", response_model=list[NumberOut])
async def list_numbers(
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:read"))],
) -> list[NumberOut]:
    rows = list((await ctx.session.execute(sa.select(OrgNumber))).scalars().all())
    return [await _out(ctx.session, n) for n in rows]


_TOLLFREE_PREFIXES = frozenset({"+1800", "+1833", "+1844", "+1855", "+1866", "+1877", "+1888"})


async def _out(session, n: OrgNumber) -> NumberOut:
    state = await registration.registration_state(session, n)
    return NumberOut(
        id=n.id,
        e164=n.e164,
        carrier=n.carrier,
        is_active=n.is_active,
        number_type=n.number_type,
        status=n.status,
        capabilities=n.capabilities or {},
        campaign_id=n.campaign_id,
        registration=state.verdict,
        registration_detail=state.detail,
    )


def _carrier_or_primary(request: Request, name: str | None):
    registry = getattr(request.app.state, "carriers", None)
    if registry is None or len(registry) == 0:
        raise CarrierNotConfiguredError("No carrier is configured on this deployment")
    if name:
        carrier = registry.get(name)
        if carrier is None:
            raise CarrierNotConfiguredError(f"Carrier {name!r} is not configured")
        return carrier
    primary = registry.primary()
    if primary is None:
        raise CarrierNotConfiguredError("No primary carrier")
    return primary


class SearchOut(BaseModel):
    e164: str
    number_type: str
    region: str
    locality: str
    monthly_cost: str
    capabilities: dict


@router.get("/available", response_model=list[SearchOut])
async def search_available(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
    carrier: str | None = None,
    area_code: str = "",
    contains: str = "",
    locality: str = "",
    region: str = "",
    number_type: str = "local",
    limit: int = 20,
) -> list[SearchOut]:
    """Numbers we could order. Asks ONE carrier - whichever is named, or the primary.

    Deliberately not a fan-out: the same number is not orderable from two carriers, and
    merging results would hide which carrier a number would come from - which is exactly
    what decides its registration regime and its routing.
    """
    provider = numbers_api.as_provider(_carrier_or_primary(request, carrier))
    found = await provider.search_numbers(
        numbers_api.NumberSearch(
            area_code=area_code,
            contains=contains,
            locality=locality,
            region=region,
            number_type=number_type,
            limit=limit,
        )
    )
    return [
        SearchOut(
            e164=n.e164,
            number_type=n.number_type,
            region=n.region,
            locality=n.locality,
            monthly_cost=n.monthly_cost,
            capabilities=n.capabilities,
        )
        for n in found
    ]


class OrderIn(BaseModel):
    e164: str = Field(min_length=3, max_length=32)
    carrier: str | None = None
    campaign_id: uuid.UUID | None = None


@router.post("/order", response_model=NumberOut, status_code=201)
async def order(
    payload: OrderIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    carrier_obj = _carrier_or_primary(request, payload.carrier)
    provider = numbers_api.as_provider(carrier_obj)
    normalized = to_e164(payload.e164)

    result = await provider.order_number(normalized)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        e164=result.e164,
        carrier=carrier_obj.name,
        provider_ref=result.provider_ref or None,
        # Whatever the carrier SAID. Recording a pending order as active means inbound is
        # dropped with no trace until somebody thinks to ask why.
        status=result.status,
        capabilities=result.capabilities or {},
        number_type="tollfree" if result.e164[:5] in _TOLLFREE_PREFIXES else "local",
        campaign_id=payload.campaign_id,
    )
    ctx.session.add(number)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"{result.e164} is already registered") from exc
    return await _out(ctx.session, number)


@router.delete("/{number_id}", response_model=NumberOut)
async def release(
    number_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    """Release at the carrier, mark released here - but KEEP THE ROW (phase-4-plan DR-5).

    Threads, messages and above all the consent ledger reference this number, and the
    ledger is the evidence that somebody opted out. Deleting the row would destroy the
    record we would need to prove we honoured a STOP.
    """
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None:
        raise NotFoundError("Number not found")
    if number.status == "released":
        return await _out(ctx.session, number)

    registry = getattr(request.app.state, "carriers", None)
    carrier_obj = registry.get(number.carrier) if registry else None
    if carrier_obj is not None and isinstance(carrier_obj, numbers_api.NumberProvider):
        await carrier_obj.release_number(number.e164, number.provider_ref)

    number.status = "released"
    number.is_active = False
    number.released_at = datetime.now(timezone.utc)
    await ctx.session.commit()
    return await _out(ctx.session, number)


class AssignIn(BaseModel):
    campaign_id: uuid.UUID | None = None


@router.patch("/{number_id}/campaign", response_model=NumberOut)
async def assign_campaign(
    number_id: uuid.UUID,
    payload: AssignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None:
        raise NotFoundError("Number not found")
    if number.number_type == "tollfree" and payload.campaign_id is not None:
        raise ValidationFailedError(
            f"{number.e164} is toll-free; it is gated by toll-free verification, not by a "
            f"10DLC campaign. Assigning one would not change its ability to send."
        )
    if payload.campaign_id is not None:
        campaign = await ctx.session.get(Campaign, payload.campaign_id)
        if campaign is None:
            raise NotFoundError("Campaign not found")
    number.campaign_id = payload.campaign_id
    await ctx.session.commit()
    return await _out(ctx.session, number)
