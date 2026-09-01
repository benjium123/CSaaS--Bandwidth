"""P15: the single source of truth for who may see and use which inbox.

Three tiers, fail-closed (see ``app/models/inboxes.py`` for the full access-model
docstring - this module is the runtime resolver for that contract):

  * A caller whose role grants ``inboxes:admin`` (or the wildcard ``"*"``) is admin - it
    sees and may use EVERY inbox in the org, no grant lookup needed.
  * An API-key-authenticated caller (no human ``actor_user_id``) is scoped by its own
    explicit permission list, not by per-user department/inbox grants (P13 DR-3) - it is
    treated as admin FOR THIS GATE ONLY, preserving the pre-P15 behaviour those callers
    already had. Tiered access is a per-human concept.
  * Everyone else sees exactly the union of grants made directly to them and grants made
    to a department they belong to. No grant, no access. When both a direct and a
    department path exist for the same number, "member" always wins over "viewer" -
    grants only ever ADD capability, never take it away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Department, DepartmentMember, Inbox, InboxGrant, OrgNumber
from app.models.rbac import WILDCARD


@dataclass(frozen=True)
class InboxAccess:
    is_admin: bool
    #: May read AND send/dial from the number.
    member_e164s: frozenset[str]
    #: Read-only. A number here is never also in ``member_e164s`` - see ``resolve_access``.
    viewer_e164s: frozenset[str]

    def can_view(self, e164: str) -> bool:
        return self.is_admin or e164 in self.member_e164s or e164 in self.viewer_e164s

    def can_use(self, e164: str) -> bool:
        return self.is_admin or e164 in self.member_e164s


_FULL_ACCESS = InboxAccess(is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset())


async def resolve_access(
    session: AsyncSession,
    actor_user_id: uuid.UUID | None,
    permissions: list[str],
) -> InboxAccess:
    """Resolve one caller's access for the CURRENT org context on ``session``.

    ``actor_user_id`` is ``ctx.actor_user_id`` - ``None`` for an API-key caller, which
    bypasses the tier entirely (see module docstring). ``permissions`` is the caller's
    role's permission list (``ctx.role.permissions``); the wildcard and ``inboxes:admin``
    both short-circuit to full access.

    Runs under whatever org context is already bound to ``session`` - every query here
    touches TenantScoped models, so the session-level guard (app/db/base.py) scopes every
    row to that org automatically. Callers must have already called
    ``set_org_context`` (routes get this for free via ``OrgContext``).
    """
    if actor_user_id is None or WILDCARD in permissions or "inboxes:admin" in permissions:
        return _FULL_ACCESS

    dept_ids = list(
        (
            await session.execute(
                sa.select(DepartmentMember.department_id)
                .join(Department, Department.id == DepartmentMember.department_id)
                .where(
                    DepartmentMember.user_id == actor_user_id,
                    # A deactivated department grants nothing - its members lose access
                    # the moment it's turned off, no membership row needs to change.
                    Department.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    conditions = [
        sa.and_(InboxGrant.grantee_type == "user", InboxGrant.grantee_id == actor_user_id)
    ]
    if dept_ids:
        conditions.append(
            sa.and_(
                InboxGrant.grantee_type == "department",
                InboxGrant.grantee_id.in_(dept_ids),
            )
        )

    rows = (
        await session.execute(
            sa.select(InboxGrant.role, OrgNumber.e164)
            .join(Inbox, Inbox.id == InboxGrant.inbox_id)
            .join(OrgNumber, OrgNumber.id == Inbox.number_id)
            .where(sa.or_(*conditions))
        )
    ).all()

    member: set[str] = set()
    viewer: set[str] = set()
    for role, e164 in rows:
        if role == "member":
            member.add(e164)
        else:
            viewer.add(e164)
    # member beats viewer on conflict - grants only ever ADD capability.
    viewer -= member

    return InboxAccess(
        is_admin=False, member_e164s=frozenset(member), viewer_e164s=frozenset(viewer)
    )
