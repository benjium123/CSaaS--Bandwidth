from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, get_current_user, require_permission
from app.db.session import get_session
from app.errors import ConflictError, NotFoundError
from app.models import Invite, OrgMembership, Role, User
from app.repositories import orgs as orgs_repo
from app.services import invites as invites_svc

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


# ----------------------------------------------------------------------------------
# Invitations - the only way in, once the instance has an owner
# ----------------------------------------------------------------------------------
class InviteIn(BaseModel):
    email: EmailStr
    #: "admin" or "agent". Never "owner" - see services/invites.INVITABLE_ROLES.
    role_name: str = "agent"


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role_name: str
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InviteCreatedOut(InviteOut):
    #: Shown EXACTLY ONCE. Only a hash is stored, so this cannot be recovered later -
    #: if it is lost, revoke the invite and issue a new one.
    token: str
    #: Ready-to-send link, built from PUBLIC_BASE_URL.
    accept_url: str


def _invite_out(inv: Invite) -> InviteOut:
    return InviteOut(
        id=inv.id,
        email=inv.email,
        role_name=inv.role_name,
        expires_at=inv.expires_at,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
    )


@router.get("/current/invites", response_model=list[InviteOut])
async def list_invites(
    ctx: Annotated[OrgContext, Depends(require_permission("members:read"))],
) -> list[InviteOut]:
    """Outstanding and historical invitations. Never includes a token."""
    rows = (
        await ctx.session.execute(sa.select(Invite).order_by(Invite.created_at.desc()))
    ).scalars().all()
    return [_invite_out(i) for i in rows]


@router.post("/current/invites", response_model=InviteCreatedOut, status_code=201)
async def create_invite(
    payload: InviteIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("members:invite"))],
) -> InviteCreatedOut:
    invite, raw = await invites_svc.create_invite(
        ctx.session,
        org_id=ctx.org.id,
        email=payload.email,
        role_name=payload.role_name,
        created_by=ctx.membership.user_id,
    )
    await ctx.session.commit()
    base = (getattr(request.app.state.settings, "public_base_url", "") or "").rstrip("/")
    return InviteCreatedOut(
        **_invite_out(invite).model_dump(),
        token=raw,
        accept_url=f"{base}/accept-invite?token={raw}",
    )


@router.delete("/current/invites/{invite_id}", response_model=InviteOut)
async def revoke_invite(
    invite_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("members:invite"))],
) -> InviteOut:
    invite = await ctx.session.get(Invite, invite_id)
    if invite is None:
        raise NotFoundError("Invitation not found")
    if invite.accepted_at is not None:
        raise ConflictError("That invitation has already been used")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
        await ctx.session.commit()
    return _invite_out(invite)
