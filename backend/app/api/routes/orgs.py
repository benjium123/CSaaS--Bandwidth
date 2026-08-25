from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, get_current_user, require_permission
from app.db.session import get_session
from app.models import OrgMembership, Role, User
from app.repositories import orgs as orgs_repo

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


class OrgCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    permissions: list[str]
    is_system: bool


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role_name: str


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    payload: OrgCreateIn,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrgOut:
    org = await orgs_repo.create_org_with_owner(session, name=payload.name, owner_id=user.id)
    await session.commit()
    return OrgOut(id=org.id, name=org.name, slug=org.slug)


@router.get("/current", response_model=OrgOut)
async def current_org(
    ctx: Annotated[OrgContext, Depends(require_permission("org:read"))],
) -> OrgOut:
    return OrgOut(id=ctx.org.id, name=ctx.org.name, slug=ctx.org.slug)


@router.get("/current/roles", response_model=list[RoleOut])
async def current_org_roles(
    ctx: Annotated[OrgContext, Depends(require_permission("roles:read"))],
) -> list[RoleOut]:
    """The resource the tenancy gate test reads across tenants.

    Note there is no ``.where(org_id == ...)`` here. That is deliberate: the session-level
    guard injects it. If the guard ever regresses, this endpoint leaks — which is exactly
    why the gate test asserts on it.
    """
    result = await ctx.session.execute(sa.select(Role))
    return [
        RoleOut(id=r.id, name=r.name, permissions=r.permissions or [], is_system=r.is_system)
        for r in result.scalars().all()
    ]


@router.get("/current/members", response_model=list[MemberOut])
async def current_org_members(
    ctx: Annotated[OrgContext, Depends(require_permission("members:read"))],
) -> list[MemberOut]:
    stmt = (
        sa.select(OrgMembership, User, Role)
        .join(User, User.id == OrgMembership.user_id)
        .join(Role, Role.id == OrgMembership.role_id)
    )
    rows = (await ctx.session.execute(stmt)).all()
    return [
        MemberOut(
            user_id=u.id, email=u.email, full_name=u.full_name, role_name=r.name
        )
        for _m, u, r in rows
    ]
