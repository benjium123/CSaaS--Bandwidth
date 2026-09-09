from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import WILDCARD, OrgMembership, Role
from app.models.rbac import validate_permissions
from app.services import audit as audit_svc

router = APIRouter(prefix="/api/v1/roles", tags=["roles"])


class RoleCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    permissions: list[str] = []
    clone_from: uuid.UUID | None = None


class RolePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=63)
    permissions: list[str] | None = None


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    permissions: list[str]
    is_system: bool
    member_count: int


async def _get_role(ctx: OrgContext, role_id: uuid.UUID) -> Role:
    role = await ctx.session.get(Role, role_id)
    if role is None:
        raise NotFoundError("Role not found")
    return role


def _assert_grantable(ctx: OrgContext, permissions: list[str]) -> None:
    if WILDCARD in permissions:
        raise ValidationFailedError(
            "The full-access permission cannot be given to a custom role"
        )
    if "org:delete" in permissions or "org:billing" in permissions:
        raise ValidationFailedError("Deleting the workspace and billing stay with the owner")
    for permission in permissions:
        if not ctx.role.grants(permission):
            raise PermissionDeniedError(
                "You cannot give a permission you do not have yourself"
            )


def _role_out(role: Role, member_count: int) -> RoleOut:
    return RoleOut(
        id=role.id,
        name=role.name,
        permissions=role.permissions or [],
        is_system=role.is_system,
        member_count=member_count,
    )


@router.get("", response_model=list[RoleOut])
async def list_roles(
    ctx: Annotated[OrgContext, Depends(require_permission("roles:read"))],
) -> list[RoleOut]:
    counts = dict(
        (
            await ctx.session.execute(
                sa.select(OrgMembership.role_id, sa.func.count()).group_by(
                    OrgMembership.role_id
                )
            )
        ).all()
    )
    roles = (
        await ctx.session.execute(
            sa.select(Role).order_by(Role.created_at.asc(), Role.id.asc())
        )
    ).scalars().all()
    return [_role_out(role, counts.get(role.id, 0)) for role in roles]


@router.post("", response_model=RoleOut, status_code=201)
async def create_role(
    payload: RoleCreateIn,
    ctx: Annotated[OrgContext, Depends(require_permission("roles:write"))],
) -> RoleOut:
    clone = None
    if payload.clone_from is not None:
        clone = await _get_role(ctx, payload.clone_from)

    if "permissions" not in payload.model_fields_set and clone is not None:
        permissions = list(clone.permissions or [])
    else:
        permissions = list(payload.permissions or [])

    permissions = validate_permissions(permissions)
    _assert_grantable(ctx, permissions)

    role = Role(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        name=payload.name.strip(),
        permissions=permissions,
        is_system=False,
    )
    ctx.session.add(role)
    try:
        await ctx.session.flush()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A role named {payload.name!r} already exists") from exc

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="role.create",
        target_type="role",
        target_id=str(role.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"name": role.name, "permissions": permissions},
    )
    await ctx.session.commit()
    return _role_out(role, 0)


@router.patch("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: uuid.UUID,
    payload: RolePatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("roles:write"))],
) -> RoleOut:
    role = await _get_role(ctx, role_id)
    if role.is_system:
        raise ConflictError("Built-in roles cannot be changed")

    updates = payload.model_dump(exclude_unset=True)
    if "permissions" in updates:
        permissions = validate_permissions(updates["permissions"] or [])
        # B1 (Opus P22 verify): keeping a permission the role ALREADY has is not a grant.
        # The UI always sends the full list (non-grantable keys render checked+disabled),
        # so checking the whole set made every save 403 for a limited role-manager.
        # Only additions are subject to "you can only give what you hold".
        added = [p for p in permissions if p not in (role.permissions or [])]
        _assert_grantable(ctx, added)
        role.permissions = permissions
    if "name" in updates:
        role.name = payload.name.strip()

    try:
        await ctx.session.flush()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A role named {payload.name!r} already exists") from exc

    count = (
        await ctx.session.execute(
            sa.select(sa.func.count())
            .select_from(OrgMembership)
            .where(OrgMembership.role_id == role.id)
        )
    ).scalar_one()

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="role.update",
        target_type="role",
        target_id=str(role.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail=updates,
    )
    await ctx.session.commit()
    return _role_out(role, count)


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("roles:write"))],
) -> Response:
    role = await _get_role(ctx, role_id)
    if role.is_system:
        raise ConflictError("Built-in roles cannot be deleted")

    count = (
        await ctx.session.execute(
            sa.select(sa.func.count())
            .select_from(OrgMembership)
            .where(OrgMembership.role_id == role.id)
        )
    ).scalar_one()
    if count > 0:
        people = "person" if count == 1 else "people"
        raise ConflictError(f"That role is still assigned to {count} {people}")

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="role.delete",
        target_type="role",
        target_id=str(role.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"name": role.name},
    )
    await ctx.session.delete(role)
    await ctx.session.commit()
    return Response(status_code=204)
