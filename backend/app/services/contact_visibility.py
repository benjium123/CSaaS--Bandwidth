# INTEGRATOR: call stamp_inbound_ownership from services/messaging.py inbound auto-create
# (messaging.py is intentionally not reproduced in full here; P22 tests call
# stamp_inbound_ownership directly).
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models import Contact, Department, DepartmentMember, Inbox, InboxGrant, OrgNumber

if TYPE_CHECKING:
    from app.auth.deps import OrgContext

POLICIES = ("everyone", "department", "owner")


@dataclass(frozen=True)
class VisibilityScope:
    policy: str  # one of POLICIES
    user_id: uuid.UUID | None  # None for API-key / machine callers
    department_ids: frozenset[uuid.UUID]
    lead_department_ids: frozenset[uuid.UUID]
    can_read_all: bool
    can_write: bool
    is_machine: bool = False


async def resolve_scope(
    session: AsyncSession,
    org,
    *,
    user_id: uuid.UUID | None,
    permissions: list[str] | None,
) -> VisibilityScope:
    policy = org.contact_visibility if org.contact_visibility in POLICIES else "everyone"
    perms = list(permissions or [])
    can_read_all = "*" in perms or "contacts:read_all" in perms
    can_write = "*" in perms or "contacts:write" in perms

    department_ids: set[uuid.UUID] = set()
    lead_department_ids: set[uuid.UUID] = set()

    if user_id is not None:
        rows = (
            await session.execute(
                sa.select(DepartmentMember.department_id, DepartmentMember.is_lead)
                .join(Department, Department.id == DepartmentMember.department_id)
                .where(
                    DepartmentMember.user_id == user_id,
                    Department.is_active.is_(True),
                )
            )
        ).all()
        for dept_id, is_lead in rows:
            department_ids.add(dept_id)
            if is_lead:
                lead_department_ids.add(dept_id)

    return VisibilityScope(
        policy=policy,
        user_id=user_id,
        department_ids=frozenset(department_ids),
        lead_department_ids=frozenset(lead_department_ids),
        can_read_all=can_read_all,
        can_write=can_write,
    )


def machine_scope(policy: str, department_id: uuid.UUID | None) -> VisibilityScope:
    return VisibilityScope(
        policy=policy if policy in POLICIES else "everyone",
        user_id=None,
        department_ids=frozenset([department_id] if department_id is not None else []),
        lead_department_ids=frozenset(),
        can_read_all=False,
        can_write=False,
        is_machine=True,
    )


def visible_contacts_filter(scope: VisibilityScope) -> sa.ColumnElement | None:
    if scope.policy == "everyone" or scope.can_read_all:
        return None
    if scope.user_id is None and not scope.is_machine:
        # Fable decision (2026-09-10): API-key callers are integrations acting for the
        # workspace, so they always see every contact regardless of policy. Pinned by
        # test_api_key_sees_all_contacts_under_owner_policy; stated in Settings copy.
        return None

    if scope.is_machine:
        clauses = []
        if scope.department_ids:
            clauses.append(Contact.department_id.in_(scope.department_ids))
        clauses.append(
            sa.and_(Contact.department_id.is_(None), Contact.owner_user_id.is_(None))
        )
        return sa.or_(*clauses)

    if scope.policy == "department":
        clauses = []
        if scope.user_id is not None:
            clauses.append(Contact.owner_user_id == scope.user_id)
        if scope.department_ids:
            clauses.append(Contact.department_id.in_(scope.department_ids))
        if scope.can_write:
            clauses.append(
                sa.and_(Contact.department_id.is_(None), Contact.owner_user_id.is_(None))
            )
        return sa.or_(*clauses) if clauses else sa.false()

    if scope.policy == "owner":
        clauses = []
        if scope.user_id is not None:
            clauses.append(Contact.owner_user_id == scope.user_id)
        if scope.lead_department_ids:
            clauses.append(Contact.department_id.in_(scope.lead_department_ids))
        if scope.can_write:
            clauses.append(
                sa.and_(Contact.department_id.is_(None), Contact.owner_user_id.is_(None))
            )
        return sa.or_(*clauses) if clauses else sa.false()

    return None


async def visible_contacts_filter_for(ctx: OrgContext) -> sa.ColumnElement | None:
    scope = await resolve_scope(
        ctx.session,
        ctx.org,
        user_id=ctx.actor_user_id,
        permissions=ctx.role.permissions or [],
    )
    return visible_contacts_filter(scope)


async def get_visible_contact(ctx: OrgContext, contact_id: uuid.UUID) -> Contact:
    stmt = sa.select(Contact).where(Contact.id == contact_id)
    predicate = await visible_contacts_filter_for(ctx)
    if predicate is not None:
        stmt = stmt.where(predicate)
    contact = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if contact is None:
        raise NotFoundError("Contact not found")
    return contact


async def default_ownership_for_creator(
    ctx: OrgContext,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    if ctx.actor_user_id is None:
        return (None, None)

    dept_id = (
        await ctx.session.execute(
            sa.select(DepartmentMember.department_id)
            .join(Department, Department.id == DepartmentMember.department_id)
            .where(
                DepartmentMember.user_id == ctx.actor_user_id,
                Department.is_active.is_(True),
            )
            .order_by(Department.name.asc(), Department.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return (ctx.actor_user_id, dept_id)


async def department_for_inbox_number(
    session: AsyncSession, our_e164: str
) -> uuid.UUID | None:
    return (
        await session.execute(
            sa.select(Department.id)
            .select_from(Department)
            .join(InboxGrant, InboxGrant.grantee_id == Department.id)
            .join(Inbox, Inbox.id == InboxGrant.inbox_id)
            .join(OrgNumber, OrgNumber.id == Inbox.number_id)
            .where(
                InboxGrant.grantee_type == "department",
                OrgNumber.e164 == our_e164,
            )
            .order_by(Department.name.asc(), Department.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def stamp_inbound_ownership(session: AsyncSession, contact, *, thread) -> None:
    if contact.owner_user_id is None:
        contact.owner_user_id = thread.assigned_user_id
    if contact.department_id is None:
        contact.department_id = await department_for_inbox_number(session, thread.our_e164)
