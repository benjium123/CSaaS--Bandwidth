from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, ValidationFailedError
from app.models import (
    GRANTEE_TYPES,
    INBOX_GRANT_ROLES,
    Department,
    Inbox,
    InboxGrant,
    OrgMembership,
    OrgNumber,
)
from app.services import audit as audit_svc
from app.services import inbox_access as inbox_access_svc

router = APIRouter(prefix="/api/v1/inboxes", tags=["inboxes"])


class InboxOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str | None
    e164: str
    number_id: uuid.UUID
    #: "admin" | "member" | "viewer" - the caller's own relationship to this inbox.
    my_role: str


class InboxPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=127)
    color: str | None = Field(default=None, max_length=16)


class GrantIn(BaseModel):
    grantee_type: str
    grantee_id: uuid.UUID
    role: str = "member"


class GrantsIn(BaseModel):
    grants: list[GrantIn] = []


class GrantOut(BaseModel):
    id: uuid.UUID
    grantee_type: str
    grantee_id: uuid.UUID
    role: str


def _grant_out(g: InboxGrant) -> GrantOut:
    return GrantOut(id=g.id, grantee_type=g.grantee_type, grantee_id=g.grantee_id, role=g.role)


async def _rows_with_e164(session) -> list[tuple[Inbox, str]]:
    stmt = sa.select(Inbox, OrgNumber.e164).join(OrgNumber, OrgNumber.id == Inbox.number_id)
    return list((await session.execute(stmt)).all())


async def _get_inbox(ctx: OrgContext, inbox_id: uuid.UUID) -> Inbox:
    inbox = await ctx.session.get(Inbox, inbox_id)
    if inbox is None:
        raise NotFoundError("Inbox not found")
    return inbox


@router.get("", response_model=list[InboxOut])
async def list_inboxes(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> list[InboxOut]:
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    out: list[InboxOut] = []
    for inbox, e164 in await _rows_with_e164(ctx.session):
        if access.is_admin:
            my_role = "admin"
        elif e164 in access.member_e164s:
            my_role = "member"
        elif e164 in access.viewer_e164s:
            my_role = "viewer"
        else:
            continue
        out.append(
            InboxOut(
                id=inbox.id,
                name=inbox.name,
                color=inbox.color,
                e164=e164,
                number_id=inbox.number_id,
                my_role=my_role,
            )
        )
    return out


@router.patch("/{inbox_id}", response_model=InboxOut)
async def patch_inbox(
    inbox_id: uuid.UUID,
    payload: InboxPatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inboxes:admin"))],
) -> InboxOut:
    inbox = await _get_inbox(ctx, inbox_id)
    if payload.name is not None:
        inbox.name = payload.name.strip()
    if payload.color is not None:
        inbox.color = payload.color
    await ctx.session.commit()
    number = await ctx.session.get(OrgNumber, inbox.number_id)
    return InboxOut(
        id=inbox.id,
        name=inbox.name,
        color=inbox.color,
        e164=number.e164 if number is not None else "",
        number_id=inbox.number_id,
        # inboxes:admin is required to reach this route at all.
        my_role="admin",
    )


@router.get("/{inbox_id}/grants", response_model=list[GrantOut])
async def get_grants(
    inbox_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inboxes:admin"))],
) -> list[GrantOut]:
    await _get_inbox(ctx, inbox_id)
    rows = (
        await ctx.session.execute(sa.select(InboxGrant).where(InboxGrant.inbox_id == inbox_id))
    ).scalars().all()
    return [_grant_out(g) for g in rows]


async def _validate_grantee(ctx: OrgContext, grantee_type: str, grantee_id: uuid.UUID) -> None:
    if grantee_type == "user":
        # .first() rather than scalar_one_or_none(): this is an existence check, not a
        # uniqueness assertion - a MultipleResultsFound here would be a pre-existing data
        # anomaly unrelated to this request, and this route has no business raising a 500
        # for it.
        exists = (
            await ctx.session.execute(
                sa.select(OrgMembership).where(OrgMembership.user_id == grantee_id)
            )
        ).scalars().first()
        if exists is None:
            raise ValidationFailedError(f"{grantee_id} is not a member of this organization")
    else:
        exists = await ctx.session.get(Department, grantee_id)
        if exists is None:
            raise NotFoundError("Department not found")


@router.put("/{inbox_id}/grants", response_model=list[GrantOut])
async def set_grants(
    inbox_id: uuid.UUID,
    payload: GrantsIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inboxes:admin"))],
) -> list[GrantOut]:
    inbox = await _get_inbox(ctx, inbox_id)

    # De-duplicate by (grantee_type, grantee_id), keeping the more permissive role - a
    # caller submitting the same grantee twice (e.g. once as viewer, once as member) must
    # not trip uq_inbox_grants_inbox_grantee, and "member" always wins per the model's own
    # conflict rule (grants only ever ADD capability).
    deduped: dict[tuple[str, uuid.UUID], str] = {}
    for g in payload.grants:
        if g.grantee_type not in GRANTEE_TYPES:
            raise ValidationFailedError(f"grantee_type must be one of {GRANTEE_TYPES}")
        if g.role not in INBOX_GRANT_ROLES:
            raise ValidationFailedError(f"role must be one of {INBOX_GRANT_ROLES}")
        await _validate_grantee(ctx, g.grantee_type, g.grantee_id)
        key = (g.grantee_type, g.grantee_id)
        if deduped.get(key) == "member":
            continue
        deduped[key] = g.role

    existing = list(
        (
            await ctx.session.execute(
                sa.select(InboxGrant).where(InboxGrant.inbox_id == inbox.id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await ctx.session.delete(row)
    # Flush the deletes before inserting the replacement rows - otherwise a grant kept
    # across the replace (same inbox/grantee_type/grantee_id) would collide with itself
    # on uq_inbox_grants_inbox_grantee before the old row is actually gone.
    await ctx.session.flush()

    created: list[InboxGrant] = []
    for (grantee_type, grantee_id), role in deduped.items():
        row = InboxGrant(
            id=uuid.uuid4(),
            org_id=ctx.org.id,
            inbox_id=inbox.id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            role=role,
        )
        ctx.session.add(row)
        created.append(row)

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="inbox.grants_set",
        target_type="inbox",
        target_id=str(inbox.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "grants": [
                {"grantee_type": gt, "grantee_id": str(gid), "role": role}
                for (gt, gid), role in deduped.items()
            ]
        },
    )
    await ctx.session.commit()
    return [_grant_out(g) for g in created]
