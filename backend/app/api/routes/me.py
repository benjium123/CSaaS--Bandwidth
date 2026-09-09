"""Capabilities endpoint for the current caller and org.

This powers nav gating and the first-run onboarding checklist. The backend remains the
authority for every permission check - the response here is a UI hint, never an
enforcement point.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.deps import OrgContext, get_current_org
from app.compliance import registration
from app.models import PERMISSIONS, OrgMembership, OrgNumber, ProviderAccount

router = APIRouter(prefix="/api/v1/me", tags=["me"])


class OrgSummaryOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    has_provider: bool
    has_number: bool
    member_count: int
    registration_state: str


class CapabilitiesOut(BaseModel):
    permissions: list[str]
    org: OrgSummaryOut


@router.get("/capabilities", response_model=CapabilitiesOut)
async def capabilities(
    ctx: Annotated[OrgContext, Depends(get_current_org)],
) -> CapabilitiesOut:
    # Deliberately NOT require_permission("org:read/settings:read"): the agent role holds
    # neither, and this endpoint exists to tell a caller what it itself may do.
    permissions: list[str]
    role_permissions = ctx.role.permissions or []
    if "*" in role_permissions:
        permissions = sorted(PERMISSIONS)
    else:
        permissions = sorted(set(role_permissions))

    provider_row = (
        await ctx.session.execute(
            sa.select(ProviderAccount.id)
            .where(ProviderAccount.status.in_(("active", "unverified")))
            .limit(1)
        )
    ).scalar_one_or_none()
    has_provider = provider_row is not None

    number_row = (
        await ctx.session.execute(
            sa.select(OrgNumber.id).where(OrgNumber.status == "active").limit(1)
        )
    ).scalar_one_or_none()
    has_number = number_row is not None

    # The tenant guard attaches its criteria to the ENTITIES in a statement, so a bare
    # `func.count()` over `select_from(OrgMembership)` would count every org's members.
    # Counting a MAPPED COLUMN keeps the guard's org filter attached.
    member_count = (
        await ctx.session.execute(sa.select(sa.func.count(OrgMembership.id)))
    ).scalar_one()

    active_numbers = (
        await ctx.session.execute(
            sa.select(OrgNumber).where(OrgNumber.status == "active").limit(50)
        )
    ).scalars().all()

    # Scan at most the first 50 active numbers; the precedence is pending > rejected >
    # approved, with any unknown falling through to "unknown".
    registration_state = "none"
    if active_numbers:
        any_pending = False
        any_rejected = False
        all_approved = True
        for number in active_numbers:
            state = await registration.registration_state(ctx.session, number)
            if state.verdict == "pending":
                any_pending = True
                break
            if state.verdict == "rejected":
                any_rejected = True
            if state.verdict != "approved":
                all_approved = False

        if any_pending:
            registration_state = "pending"
        elif any_rejected:
            registration_state = "rejected"
        elif all_approved:
            registration_state = "approved"
        else:
            registration_state = "unknown"

    return CapabilitiesOut(
        permissions=permissions,
        org=OrgSummaryOut(
            id=ctx.org.id,
            name=ctx.org.name,
            slug=ctx.org.slug,
            has_provider=has_provider,
            has_number=has_number,
            member_count=int(member_count),
            registration_state=registration_state,
        ),
    )
