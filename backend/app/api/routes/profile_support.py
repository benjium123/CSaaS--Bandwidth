"""The person's own profile (name + 911 address) and the Help menu (contacts + requests)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, get_current_org, get_current_user
from app.models import User
from app.services import audit as audit_svc

router = APIRouter(prefix="/api/v1", tags=["profile", "support"])


class ProfileIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)


class EmergencyAddressIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    street_address: str = Field(min_length=1, max_length=255)
    extended_address: str | None = Field(default=None, max_length=255)
    locality: str = Field(min_length=1, max_length=127)
    administrative_area: str = Field(min_length=2, max_length=32)
    postal_code: str = Field(min_length=3, max_length=16)
    country_code: str = Field(default="US", min_length=2, max_length=2)


@router.get("/me/profile")
async def get_profile(
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from app.services import profile

    return await profile.profile(ctx.session, ctx.org.id, user)


@router.put("/me/profile")
async def update_profile(
    payload: ProfileIn,
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from app.services import profile

    profile.set_name(user, payload.full_name)
    await ctx.session.commit()
    return await profile.profile(ctx.session, ctx.org.id, user)


@router.put("/me/emergency-address")
async def set_my_emergency_address(
    payload: EmergencyAddressIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Validate and register MY 911 address, then apply it to every number assigned to me."""
    from app.services import e911, profile

    result = await profile.set_emergency_address(
        ctx.session, request.app.state.settings, ctx.org.id, user, payload.model_dump()
    )
    address = await profile.my_address(ctx.session, ctx.org.id, user.id)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="e911.profile_address_set",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        detail={"label": e911.one_line(address) if address else None, **result},
    )
    await ctx.session.commit()
    return {**(await profile.profile(ctx.session, ctx.org.id, user)), **result}


class SupportRequestIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    page: str | None = Field(default=None, max_length=255)


@router.get("/support/contacts")
async def support_contacts(ctx: Annotated[OrgContext, Depends(get_current_org)]) -> dict:
    from app.services import support

    return await support.contacts(ctx.session)


@router.post("/support/requests", status_code=201)
async def create_support_request(
    payload: SupportRequestIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from app.services import support

    row = await support.create_request(
        ctx.session, request.app.state.settings, ctx.org.id, user,
        subject=payload.subject, body=payload.body, page=payload.page,
    )
    return support.to_dict(row)


@router.get("/support/requests")
async def my_support_requests(
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from app.services import support

    return {"requests": await support.my_requests(ctx.session, ctx.org.id, user.id)}
