from __future__ import annotations

import re
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError
from app.models import Org, OrgMembership, Role
from app.models.rbac import SYSTEM_ROLES, validate_permissions

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return (slug or "org")[:63]


async def get_org_by_slug(session: AsyncSession, slug: str) -> Org | None:
    result = await session.execute(sa.select(Org).where(Org.slug == slug))
    return result.scalar_one_or_none()


async def create_org_with_owner(session: AsyncSession, *, name: str, owner_id: uuid.UUID) -> Org:
    """Create an org, seed its system roles, and make the creator its owner.

    JUSTIFIED allow_unscoped: this is the bootstrap. The org does not exist yet, so there
    is no org context to bind until we have created it — and the roles/membership we insert
    here are the very first rows that establish that context. The flag is cleared before
    returning so nothing downstream inherits an unguarded session.
    """
    base = slugify(name)
    slug = base
    n = 1
    while await get_org_by_slug(session, slug) is not None:
        n += 1
        slug = f"{base[:58]}-{n}"
        if n > 1000:  # pragma: no cover - pathological
            raise ConflictError("Could not allocate a unique org slug")

    org = Org(id=uuid.uuid4(), name=name.strip(), slug=slug)
    session.add(org)
    await session.flush()

    session.info[ALLOW_UNSCOPED_KEY] = True
    try:
        roles: dict[str, Role] = {}
        for role_name, perms in SYSTEM_ROLES.items():
            role = Role(
                id=uuid.uuid4(),
                org_id=org.id,
                name=role_name,
                permissions=validate_permissions(list(perms)),
                is_system=True,
            )
            session.add(role)
            roles[role_name] = role
        await session.flush()

        session.add(
            OrgMembership(
                id=uuid.uuid4(),
                org_id=org.id,
                user_id=owner_id,
                role_id=roles["owner"].id,
            )
        )
        await session.flush()
    finally:
        session.info[ALLOW_UNSCOPED_KEY] = False

    set_org_context(session, org.id)
    return org


async def get_membership(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Org, OrgMembership, Role] | None:
    """Resolve (org, membership, role) for a user.

    JUSTIFIED allow_unscoped: this IS the check that establishes tenant context. It cannot
    itself require the context it is about to set. It is explicitly constrained to a single
    (org_id, user_id) pair, so it can never return another tenant's rows.
    """
    stmt = (
        sa.select(Org, OrgMembership, Role)
        .join(OrgMembership, OrgMembership.org_id == Org.id)
        .join(Role, Role.id == OrgMembership.role_id)
        .where(
            Org.id == org_id,
            OrgMembership.user_id == user_id,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return (row[0], row[1], row[2])


async def list_memberships_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[tuple]:
    """All orgs a user belongs to. Same justification as get_membership: constrained to
    this one user, and it is what powers the org picker before any org is chosen."""
    stmt = (
        sa.select(Org, Role)
        .join(OrgMembership, OrgMembership.org_id == Org.id)
        .join(Role, Role.id == OrgMembership.role_id)
        .where(OrgMembership.user_id == user_id)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return [(r[0], r[1]) for r in (await session.execute(stmt)).all()]
