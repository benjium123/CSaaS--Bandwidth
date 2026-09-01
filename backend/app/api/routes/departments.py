from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import Department, DepartmentMember, InboxGrant, OrgMembership
from app.services import audit as audit_svc

router = APIRouter(prefix="/api/v1/departments", tags=["departments"])


class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)


class DepartmentPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=127)
    is_active: bool | None = None


class MembersIn(BaseModel):
    user_ids: list[uuid.UUID] = []


class DepartmentOut(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    member_user_ids: list[uuid.UUID]


async def _members_of(session, department_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            sa.select(DepartmentMember.user_id).where(
                DepartmentMember.department_id == department_id
            )
        )
    ).scalars().all()
    return list(rows)


async def _out(session, d: Department) -> DepartmentOut:
    return DepartmentOut(
        id=d.id,
        name=d.name,
        is_active=d.is_active,
        member_user_ids=await _members_of(session, d.id),
    )


async def _get_department(ctx: OrgContext, department_id: uuid.UUID) -> Department:
    d = await ctx.session.get(Department, department_id)
    if d is None:
        raise NotFoundError("Department not found")
    return d


@router.get("", response_model=list[DepartmentOut])
async def list_departments(
    ctx: Annotated[OrgContext, Depends(require_permission("departments:read"))],
) -> list[DepartmentOut]:
    rows = (
        await ctx.session.execute(sa.select(Department).order_by(Department.name))
    ).scalars().all()
    return [await _out(ctx.session, d) for d in rows]


@router.post("", response_model=DepartmentOut, status_code=201)
async def create_department(
    payload: DepartmentIn,
    ctx: Annotated[OrgContext, Depends(require_permission("departments:manage"))],
) -> DepartmentOut:
    row = Department(id=uuid.uuid4(), org_id=ctx.org.id, name=payload.name.strip())
    ctx.session.add(row)
    try:
        await ctx.session.flush()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A department named {payload.name!r} already exists") from exc
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="department.create",
        target_type="department",
        target_id=str(row.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={"name": row.name},
    )
    await ctx.session.commit()
    return await _out(ctx.session, row)


@router.patch("/{department_id}", response_model=DepartmentOut)
async def patch_department(
    department_id: uuid.UUID,
    payload: DepartmentPatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("departments:manage"))],
) -> DepartmentOut:
    d = await _get_department(ctx, department_id)
    if payload.name is not None:
        d.name = payload.name.strip()
    if payload.is_active is not None:
        d.is_active = payload.is_active
    try:
        await ctx.session.flush()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A department named {payload.name!r} already exists") from exc
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="department.update",
        target_type="department",
        target_id=str(d.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail=payload.model_dump(exclude_none=True),
    )
    await ctx.session.commit()
    return await _out(ctx.session, d)


@router.delete("/{department_id}", status_code=204)
async def delete_department(
    department_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("departments:manage"))],
) -> Response:
    d = await _get_department(ctx, department_id)
    # No FK on InboxGrant.grantee_id (deliberate - see models/inboxes.py docstring): a
    # department's grants must be cleaned up here, or they'd dangle pointing at a
    # department id that no longer exists.
    await ctx.session.execute(
        sa.delete(InboxGrant).where(
            InboxGrant.grantee_type == "department", InboxGrant.grantee_id == department_id
        )
    )
    await ctx.session.execute(
        sa.delete(DepartmentMember).where(DepartmentMember.department_id == department_id)
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="department.delete",
        target_type="department",
        target_id=str(department_id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={"name": d.name},
    )
    await ctx.session.delete(d)
    await ctx.session.commit()
    return Response(status_code=204)


@router.put("/{department_id}/members", response_model=DepartmentOut)
async def set_members(
    department_id: uuid.UUID,
    payload: MembersIn,
    ctx: Annotated[OrgContext, Depends(require_permission("departments:manage"))],
) -> DepartmentOut:
    d = await _get_department(ctx, department_id)
    wanted = set(payload.user_ids)
    if wanted:
        found = {
            m.user_id
            for m in (
                await ctx.session.execute(
                    sa.select(OrgMembership).where(OrgMembership.user_id.in_(wanted))
                )
            )
            .scalars()
            .all()
        }
        missing = wanted - found
        if missing:
            raise ValidationFailedError(
                f"Not members of this organization: {sorted(str(m) for m in missing)}"
            )

    existing = list(
        (
            await ctx.session.execute(
                sa.select(DepartmentMember).where(DepartmentMember.department_id == d.id)
            )
        )
        .scalars()
        .all()
    )
    have = {row.user_id for row in existing}
    for row in existing:
        if row.user_id not in wanted:
            await ctx.session.delete(row)
    for user_id in wanted - have:
        ctx.session.add(
            DepartmentMember(
                id=uuid.uuid4(), org_id=ctx.org.id, department_id=d.id, user_id=user_id
            )
        )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="department.members_set",
        target_type="department",
        target_id=str(d.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={"user_ids": sorted(str(u) for u in wanted)},
    )
    await ctx.session.commit()
    return await _out(ctx.session, d)
