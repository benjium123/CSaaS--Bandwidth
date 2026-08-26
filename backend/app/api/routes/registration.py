"""10DLC brands and campaigns, and toll-free verification.

`compliance:manage` throughout. Registration decides what an org is legally permitted to
send, so it sits with compliance rather than with numbers - an agent who can order a number
should not be able to declare a use case on the company's behalf.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import OrgNumber
from app.models.numbers import Brand, Campaign, TollFreeVerification
from app.services import registration as reg

router = APIRouter(prefix="/api/v1/registration", tags=["registration"])


# ----------------------------------------------------------------------------------
# Brands
# ----------------------------------------------------------------------------------
class BrandIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    ein: str | None = Field(default=None, max_length=32)
    entity_type: str = "PRIVATE_PROFIT"
    vertical: str | None = None
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str = "US"


class BrandOut(BaseModel):
    id: uuid.UUID
    name: str
    entity_type: str
    status: str
    carrier_refs: dict
    last_error: str | None
    #: What still has to be filled in before this can be submitted. Surfaced on READ so the
    #: console can show the gap without the user having to fail a submission to discover it.
    missing_for_submission: list[str]


def _brand_out(b: Brand) -> BrandOut:
    missing = [f for f in reg.REQUIRED_BRAND_FIELDS if not getattr(b, f, None)]
    if b.entity_type != "SOLE_PROPRIETOR" and not b.ein:
        missing.append("ein")
    return BrandOut(
        id=b.id,
        name=b.name,
        entity_type=b.entity_type,
        status=b.status,
        carrier_refs=b.carrier_refs or {},
        last_error=b.last_error,
        missing_for_submission=missing,
    )


@router.get("/brands", response_model=list[BrandOut])
async def list_brands(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[BrandOut]:
    rows = (await ctx.session.execute(sa.select(Brand).order_by(Brand.name))).scalars().all()
    return [_brand_out(b) for b in rows]


@router.post("/brands", response_model=BrandOut, status_code=201)
async def create_brand(
    payload: BrandIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    brand = Brand(id=uuid.uuid4(), org_id=ctx.org.id, **payload.model_dump())
    ctx.session.add(brand)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A brand named {payload.name!r} already exists") from exc
    return _brand_out(brand)


@router.post("/brands/{brand_id}/submit", response_model=BrandOut)
async def submit_brand(
    brand_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    brand = await reg.submit_brand(ctx.session, brand_id)
    await ctx.session.commit()
    return _brand_out(brand)


# ----------------------------------------------------------------------------------
# Campaigns
# ----------------------------------------------------------------------------------
class CampaignIn(BaseModel):
    brand_id: uuid.UUID
    name: str = Field(min_length=1, max_length=127)
    use_case: str = "MIXED"
    description: str | None = None
    opt_in_process: str | None = None
    sample_messages: list[str] = []
    help_message: str | None = None
    opt_out_message: str | None = None


class CampaignOut(BaseModel):
    id: uuid.UUID
    brand_id: uuid.UUID
    name: str
    use_case: str
    status: str
    carrier_refs: dict
    last_error: str | None
    number_count: int
    missing_for_submission: list[str]


async def _campaign_out(session, c: Campaign) -> CampaignOut:
    missing = [f for f in reg.REQUIRED_CAMPAIGN_FIELDS if not getattr(c, f, None)]
    if not (c.sample_messages or []):
        missing.append("sample_messages")
    if not c.opt_out_message:
        missing.append("opt_out_message")
    return CampaignOut(
        id=c.id,
        brand_id=c.brand_id,
        name=c.name,
        use_case=c.use_case,
        status=c.status,
        carrier_refs=c.carrier_refs or {},
        last_error=c.last_error,
        number_count=await reg.numbers_on_campaign(session, c.id),
        missing_for_submission=missing,
    )


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[CampaignOut]:
    rows = (
        await ctx.session.execute(sa.select(Campaign).order_by(Campaign.name))
    ).scalars().all()
    return [await _campaign_out(ctx.session, c) for c in rows]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    payload: CampaignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    brand = await ctx.session.get(Brand, payload.brand_id)
    if brand is None:
        raise NotFoundError("Brand not found")
    campaign = Campaign(id=uuid.uuid4(), org_id=ctx.org.id, **payload.model_dump())
    ctx.session.add(campaign)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A campaign named {payload.name!r} already exists") from exc
    return await _campaign_out(ctx.session, campaign)


@router.post("/campaigns/{campaign_id}/submit", response_model=CampaignOut)
async def submit_campaign(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    campaign = await reg.submit_campaign(ctx.session, campaign_id)
    await ctx.session.commit()
    return await _campaign_out(ctx.session, campaign)


class StatusIn(BaseModel):
    status: str
    error: str | None = None


@router.post("/campaigns/{campaign_id}/status", response_model=CampaignOut)
async def set_campaign_status(
    campaign_id: uuid.UUID,
    payload: StatusIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    """Record a registrar decision.

    Monotonic: a stale `submitted` arriving after `approved` is ignored, not applied.
    Carriers retry unordered, and demoting an approved campaign would stop every number on
    it from sending until somebody noticed.
    """
    campaign = await ctx.session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    reg.advance_status(campaign, payload.status, error=payload.error)
    await ctx.session.commit()
    return await _campaign_out(ctx.session, campaign)


@router.post("/brands/{brand_id}/status", response_model=BrandOut)
async def set_brand_status(
    brand_id: uuid.UUID,
    payload: StatusIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    brand = await ctx.session.get(Brand, brand_id)
    if brand is None:
        raise NotFoundError("Brand not found")
    reg.advance_status(brand, payload.status, error=payload.error)
    await ctx.session.commit()
    return _brand_out(brand)


# ----------------------------------------------------------------------------------
# Toll-free verification
# ----------------------------------------------------------------------------------
class TfvIn(BaseModel):
    number_id: uuid.UUID
    business_name: str = Field(min_length=1, max_length=255)
    use_case: str = "MIXED"
    use_case_summary: str | None = None
    opt_in_process: str | None = None
    opt_in_screenshot_url: str | None = None
    message_volume: int | None = None
    contact_email: str | None = None


class TfvOut(BaseModel):
    id: uuid.UUID
    number_id: uuid.UUID
    business_name: str
    status: str
    last_error: str | None


def _tfv_out(t: TollFreeVerification) -> TfvOut:
    return TfvOut(
        id=t.id,
        number_id=t.number_id,
        business_name=t.business_name,
        status=t.status,
        last_error=t.last_error,
    )


@router.get("/tollfree", response_model=list[TfvOut])
async def list_tfv(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[TfvOut]:
    rows = (await ctx.session.execute(sa.select(TollFreeVerification))).scalars().all()
    return [_tfv_out(t) for t in rows]


@router.post("/tollfree", response_model=TfvOut, status_code=201)
async def create_tfv(
    payload: TfvIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    number = await ctx.session.get(OrgNumber, payload.number_id)
    if number is None:
        raise NotFoundError("Number not found")
    if number.number_type != "tollfree":
        # Verifying a long code would produce an "approved" that means nothing, on a number
        # the 10DLC gate is separately refusing.
        raise ValidationFailedError(
            f"{number.e164} is a {number.number_type} number; toll-free verification "
            f"applies only to toll-free numbers. Register it under a 10DLC campaign instead."
        )
    tfv = TollFreeVerification(id=uuid.uuid4(), org_id=ctx.org.id, **payload.model_dump())
    ctx.session.add(tfv)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError("This number already has a verification on file") from exc
    return _tfv_out(tfv)


@router.post("/tollfree/{tfv_id}/submit", response_model=TfvOut)
async def submit_tfv(
    tfv_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    tfv = await reg.submit_tollfree(ctx.session, tfv_id)
    await ctx.session.commit()
    return _tfv_out(tfv)


@router.post("/tollfree/{tfv_id}/status", response_model=TfvOut)
async def set_tfv_status(
    tfv_id: uuid.UUID,
    payload: StatusIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    tfv = await ctx.session.get(TollFreeVerification, tfv_id)
    if tfv is None:
        raise NotFoundError("Toll-free verification not found")
    reg.advance_status(tfv, payload.status, error=payload.error)
    await ctx.session.commit()
    return _tfv_out(tfv)
