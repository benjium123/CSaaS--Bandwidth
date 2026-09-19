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
    DepartmentMember,
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
    sla_first_response_minutes: int | None
    sla_resolution_minutes: int | None


class InboxPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=127)
    color: str | None = Field(default=None, max_length=16)
    sla_first_response_minutes: int | None = None
    sla_resolution_minutes: int | None = None
    clear_sla_first_response: bool = False
    clear_sla_resolution: bool = False


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


class InboxAssignmentDeptOut(BaseModel):
    #: One entry per ACTIVE department the user belongs to that holds a grant on the inbox.
    department_id: uuid.UUID
    department_name: str
    role: str


class InboxAssignmentOut(BaseModel):
    #: Every inbox in the org is returned, granted or not - the UI is a checklist of the
    #: org's numbers, so a missing entry would read as "this inbox does not exist".
    inbox_id: uuid.UUID
    inbox_name: str
    number_id: uuid.UUID
    e164: str
    #: "member" | "viewer" - the user's OWN direct grant on this inbox, None when there is
    #: none. Department-derived access is reported separately in via_department.
    direct_role: str | None
    via_department: list[InboxAssignmentDeptOut]


class UserAssignmentsOut(BaseModel):
    #: One entry per member of the caller's org. Members with no grants at all still appear
    #: - their `assignments` lists every inbox in the org with direct_role=None, which the
    #: UI renders as "No numbers" rather than omitting the row.
    user_id: uuid.UUID
    #: Exactly the InboxAssignmentOut objects the single-user GET returns, so the frontend
    #: can reuse one parser for both shapes.
    assignments: list[InboxAssignmentOut]


class InboxAssignmentIn(BaseModel):
    inbox_id: uuid.UUID
    #: Plain str, deliberately not a Literal/enum - see set_user_assignments for why.
    role: str


class InboxAssignmentsIn(BaseModel):
    inboxes: list[InboxAssignmentIn] = []


async def _rows_with_e164(session) -> list[tuple[Inbox, str]]:
    stmt = sa.select(Inbox, OrgNumber.e164).join(OrgNumber, OrgNumber.id == Inbox.number_id)
    return list((await session.execute(stmt)).all())


async def _get_inbox(ctx: OrgContext, inbox_id: uuid.UUID) -> Inbox:
    inbox = await ctx.session.get(Inbox, inbox_id)
    if inbox is None:
        raise NotFoundError("Inbox not found")
    return inbox


def _validate_sla_minutes(value: int | None) -> None:
    if value is not None and (value < 1 or value > 44640):
        raise ValidationFailedError("Enter a number of minutes between 1 and 44640.")


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
                sla_first_response_minutes=inbox.sla_first_response_minutes,
                sla_resolution_minutes=inbox.sla_resolution_minutes,
            )
        )
    return out


async def _require_org_member(ctx: OrgContext, user_id: uuid.UUID) -> None:
    """404 unless ``user_id`` is a member of the CALLER's org.

    Applied as the very first thing both /assignments routes do, before any read and
    before any write, so a bogus or foreign user id can neither leak the org's inbox
    list nor have grants created for it.
    """
    # OrgMembership is TenantScoped, so this select is scoped to the caller's org by the
    # listener in app/db/base.py - a membership row from ANOTHER org never comes back.
    # That is why "member of another org" and "does not exist" are deliberately one and
    # the same 404 here: telling them apart would let a caller probe cross-org membership.
    #
    # .first() rather than scalar_one_or_none(), for the reason documented on
    # _validate_grantee: this is an existence check, not a uniqueness assertion, and a
    # duplicate row is a pre-existing data anomaly this route has no business 500-ing on.
    member = (
        await ctx.session.execute(
            sa.select(OrgMembership).where(OrgMembership.user_id == user_id)
        )
    ).scalars().first()
    if member is None:
        raise NotFoundError("User not found")


async def _assignments_for_users(
    ctx: OrgContext, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[InboxAssignmentOut]]:
    """One InboxAssignmentOut list per user, for every id in ``user_ids``.

    The single-user GET, the bulk GET and the PUT response all route through this ONE
    assembler, so their element shapes cannot drift apart. It issues a FIXED number of
    queries (four at most) regardless of how many users are passed - the per-user loop
    lives in memory, never in SQL.
    """
    # SQL `IN ()` is a trap: some dialects reject an empty list outright, others expand it
    # to a predicate that is always false. Either way there is nothing to assemble.
    if not user_ids:
        return {}

    # 1. The org's inboxes joined to their numbers. Once, shared by every user below.
    rows = await _rows_with_e164(ctx.session)

    # 2. Every requested user's own direct grants in ONE query, instead of one query per
    # user. Keyed user -> inbox -> role.
    direct_rows = (
        await ctx.session.execute(
            sa.select(InboxGrant.grantee_id, InboxGrant.inbox_id, InboxGrant.role).where(
                InboxGrant.grantee_type == "user",
                InboxGrant.grantee_id.in_(user_ids),
            )
        )
    ).all()
    direct: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    for grantee_id, inbox_id, role in direct_rows:
        direct.setdefault(grantee_id, {})[inbox_id] = role

    # 3. Department membership for all requested users in ONE query. Departments that are
    # no longer active are EXCLUDED because resolve_access treats their grants as void at
    # send/dial time (see app/services/inbox_access.py) - this read must agree with the
    # runtime, or the checklist would advertise access that sending would then refuse.
    dept_member_rows = (
        await ctx.session.execute(
            sa.select(DepartmentMember.user_id, Department.id, Department.name)
            .join(Department, Department.id == DepartmentMember.department_id)
            .where(
                DepartmentMember.user_id.in_(user_ids),
                Department.is_active.is_(True),
            )
        )
    ).all()
    dept_names_by_user: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    dept_ids: set[uuid.UUID] = set()
    for member_user_id, dept_id, dept_name in dept_member_rows:
        dept_names_by_user.setdefault(member_user_id, {})[dept_id] = dept_name
        dept_ids.add(dept_id)

    # 4. Grants held by ANY of those departments, in ONE query, keyed inbox -> dept ->
    # role. Skipped entirely when no active departments were found, so an org without
    # departments never pays for the round trip.
    dept_grants: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    if dept_ids:
        dept_grant_rows = (
            await ctx.session.execute(
                sa.select(InboxGrant.inbox_id, InboxGrant.grantee_id, InboxGrant.role).where(
                    InboxGrant.grantee_type == "department",
                    InboxGrant.grantee_id.in_(list(dept_ids)),
                )
            )
        ).all()
        for inbox_id, dept_id, role in dept_grant_rows:
            dept_grants.setdefault(inbox_id, {})[dept_id] = role

    # Everything below is in-memory assembly. This is exactly where a naive per-user query
    # loop would have been, and it is deliberately not in SQL.
    result: dict[uuid.UUID, list[InboxAssignmentOut]] = {}
    for user_id in user_ids:
        user_direct = direct.get(user_id, {})
        user_depts = dept_names_by_user.get(user_id, {})
        out: list[InboxAssignmentOut] = []
        for inbox, e164 in rows:
            inbox_dept_roles = dept_grants.get(inbox.id, {})
            via_dept = [
                InboxAssignmentDeptOut(
                    department_id=dept_id,
                    department_name=dept_name,
                    role=inbox_dept_roles[dept_id],
                )
                for dept_id, dept_name in user_depts.items()
                if dept_id in inbox_dept_roles
            ]
            out.append(
                InboxAssignmentOut(
                    inbox_id=inbox.id,
                    inbox_name=inbox.name,
                    number_id=inbox.number_id,
                    e164=e164,
                    direct_role=user_direct.get(inbox.id),
                    # Sorted by name so a UI that diffs consecutive responses does not
                    # see spurious reordering.
                    via_department=sorted(
                        via_dept, key=lambda d: d.department_name
                    ),
                )
            )
        # e164 ascending, so the checklist renders in a stable order across GET and PUT.
        out.sort(key=lambda a: a.e164)
        result[user_id] = out
    return result


async def _assignments_for_user(
    ctx: OrgContext, user_id: uuid.UUID
) -> list[InboxAssignmentOut]:
    """One InboxAssignmentOut per inbox in the org, for ``user_id``.

    Thin wrapper over _assignments_for_users so the single-user GET, the bulk GET and the
    PUT response all share ONE assembler - the checklist a caller reads and the state a
    caller gets back after writing can never drift apart.
    """
    return (await _assignments_for_users(ctx, [user_id]))[user_id]


# response_model=None is DELIBERATE: this single FastAPI operation serves TWO response
# shapes (the single-user list and the org-wide bulk list) that FastAPI cannot express as
# one union response model. Both branches return pydantic models, so FastAPI's
# jsonable_encoder serializes them exactly as before - the single-user branch's JSON is
# unchanged even though no response_model is declared.
@router.get("/assignments", response_model=None)
async def list_user_assignments(
    ctx: Annotated[OrgContext, Depends(require_permission("inboxes:admin"))],
    user_id: uuid.UUID | None = None,
) -> list[InboxAssignmentOut] | list[UserAssignmentsOut]:
    if user_id is not None:
        # Validate the target user FIRST: a foreign or bogus id is a 404 and nothing about
        # this org's inboxes is read for it.
        await _require_org_member(ctx, user_id)
        return await _assignments_for_user(ctx, user_id)

    # Bulk branch: without user_id, return one entry per member of the caller's org so the
    # Team page renders every row from ONE request instead of N+1 requests (one per
    # member).
    #
    # OrgMembership is TenantScoped, so this select is scoped to the caller's org by the
    # listener in app/db/base.py - that scoping is the ONLY thing keeping another
    # workspace's users out of this LIST response. That makes this branch leak MORE than
    # the single-user one (which 404s on a foreign id) if the scope is ever weakened, so
    # never pass allow_unscoped on this query.
    user_ids: list[uuid.UUID] = list(
        dict.fromkeys(
            (await ctx.session.execute(sa.select(OrgMembership.user_id))).scalars().all()
        )
    )

    # EVERY member appears, including members with no grants at all - their `assignments`
    # still lists every inbox in the org with direct_role=None, which the UI renders as
    # "No numbers" rather than omitting the row.
    assignments = await _assignments_for_users(ctx, user_ids)
    out = [
        UserAssignmentsOut(user_id=member_id, assignments=assignments[member_id])
        for member_id in user_ids
    ]
    # str(user_id) ascending for determinism, independent of the database's row order.
    # Each entry's `assignments` keeps its e164 ascending order from the assembler.
    out.sort(key=lambda entry: str(entry.user_id))
    return out


@router.put("/assignments", response_model=list[InboxAssignmentOut])
async def set_user_assignments(
    ctx: Annotated[OrgContext, Depends(require_permission("inboxes:admin"))],
    user_id: uuid.UUID,
    payload: InboxAssignmentsIn,
) -> list[InboxAssignmentOut]:
    # First thing the route does - see _require_org_member.
    await _require_org_member(ctx, user_id)

    # Validate the WHOLE body before any delete or insert, so a rejected request writes
    # nothing at all. `role` is a plain str on purpose: the app has no
    # RequestValidationError handler, so a pydantic Literal/enum rejection would return
    # FastAPI's default {"detail": ...} body instead of the app's {"error": {...}}
    # envelope - an invalid role has to come back through ValidationFailedError like
    # every other validation failure.
    #
    # De-duplicate by inbox, keeping the more permissive role: "member" always wins per
    # the model's own conflict rule (grants only ever ADD capability), and naming the
    # same inbox twice must not trip uq_inbox_grants_inbox_grantee. A duplicate is not an
    # error.
    deduped: dict[uuid.UUID, str] = {}
    for item in payload.inboxes:
        if item.role not in INBOX_GRANT_ROLES:
            raise ValidationFailedError(f"role must be one of {INBOX_GRANT_ROLES}")
        # TenantScoped, so a foreign inbox id simply resolves to nothing here - this
        # doubles as the cross-org guard for the write below.
        await _get_inbox(ctx, item.inbox_id)
        if deduped.get(item.inbox_id) == "member":
            continue
        deduped[item.inbox_id] = item.role

    existing = list(
        (
            await ctx.session.execute(
                sa.select(InboxGrant).where(
                    # ONLY this user's own direct grants. The grantee_type filter is load
                    # bearing: without it this replace would also delete every DEPARTMENT
                    # grant on those inboxes, silently stripping access from a whole
                    # team. Department grants must always survive this PUT untouched.
                    InboxGrant.grantee_type == "user",
                    InboxGrant.grantee_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await ctx.session.delete(row)
    # Flush the deletes before inserting the replacements - otherwise a grant kept across
    # the replace (same inbox/grantee) would collide with itself on
    # uq_inbox_grants_inbox_grantee before the old row is actually gone.
    await ctx.session.flush()

    for inbox_id, role in deduped.items():
        ctx.session.add(
            InboxGrant(
                id=uuid.uuid4(),
                org_id=ctx.org.id,
                inbox_id=inbox_id,
                grantee_type="user",
                grantee_id=user_id,
                role=role,
            )
        )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="inbox.user_assignments_set",
        target_type="user",
        target_id=str(user_id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={"inboxes": [{"inbox_id": str(i), "role": r} for i, r in deduped.items()]},
    )
    # ONE transaction: a single commit at the very end, after every validation has passed.
    await ctx.session.commit()
    return await _assignments_for_user(ctx, user_id)


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

    old_first = inbox.sla_first_response_minutes
    old_resolution = inbox.sla_resolution_minutes

    new_first = old_first
    if payload.sla_first_response_minutes is not None:
        _validate_sla_minutes(payload.sla_first_response_minutes)
        new_first = payload.sla_first_response_minutes
    elif payload.clear_sla_first_response:
        new_first = None

    new_resolution = old_resolution
    if payload.sla_resolution_minutes is not None:
        _validate_sla_minutes(payload.sla_resolution_minutes)
        new_resolution = payload.sla_resolution_minutes
    elif payload.clear_sla_resolution:
        new_resolution = None

    if new_first != old_first or new_resolution != old_resolution:
        inbox.sla_first_response_minutes = new_first
        inbox.sla_resolution_minutes = new_resolution
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            action="inbox.sla_set",
            target_type="inbox",
            target_id=str(inbox.id),
            actor_user_id=ctx.actor_user_id,
            actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
            detail={
                "first_response_minutes": new_first,
                "resolution_minutes": new_resolution,
            },
        )

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
        sla_first_response_minutes=inbox.sla_first_response_minutes,
        sla_resolution_minutes=inbox.sla_resolution_minutes,
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
        dept = await ctx.session.get(Department, grantee_id)
        if dept is None:
            # 5.17: both grantee-not-found paths now raise the SAME error type - a
            # caller submitting a bad grantee id got a 422 for "user" and a 404 for
            # "department", an inconsistency with no reason behind it.
            raise ValidationFailedError(f"{grantee_id} is not a department in this organization")
        if not dept.is_active:
            # 5.17: a grant to a deactivated department was previously accepted outright
            # - resolve_access already treats a deactivated department's grants as void
            # (module docstring), so this grant would sit in the table doing nothing,
            # silently misleading whoever set it up.
            raise ValidationFailedError(
                f"Department {grantee_id} is deactivated and cannot receive new grants"
            )


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
