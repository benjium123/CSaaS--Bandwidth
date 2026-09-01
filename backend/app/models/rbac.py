"""Roles, permissions and org membership.

Permissions are a code-defined catalogue stored as a JSON list on the role row. That is a
deliberate P0 simplification: there is no custom-role editor yet, so a join table would buy
nothing but migrations. Normalize when custom roles ship.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON
from app.errors import ValidationFailedError

# --------------------------------------------------------------------------------------
# Permission catalogue. Keys are "resource:action". "*" is the wildcard (owner only).
# --------------------------------------------------------------------------------------
PERMISSIONS: tuple[str, ...] = (
    "org:read",
    "org:update",
    "org:delete",
    "org:billing",
    "members:read",
    "members:invite",
    "members:update",
    "members:remove",
    "roles:read",
    "roles:write",
    "inbox:read",
    "inbox:send",
    "inbox:manage",
    # P15: see + manage ALL inboxes and their grants, bypassing the tiered access model.
    # Deliberately separate from inbox:manage, which agents hold to work their OWN inboxes.
    "inboxes:admin",
    "departments:read",
    "departments:manage",
    "contacts:read",
    "contacts:write",
    "numbers:read",
    "numbers:manage",
    "campaigns:read",
    "campaigns:manage",
    "calls:read",
    "calls:place",
    # P12: monitor/whisper/barge on live calls. Owner via wildcard; admin via the
    # comprehension below + migration 0015 backfill for pre-P12 orgs; agents never.
    "calls:supervise",
    "reports:read",
    "settings:read",
    "settings:write",
    "compliance:read",
    "compliance:manage",
    "templates:read",
    "templates:manage",
)

WILDCARD = "*"

SYSTEM_ROLES: dict[str, list[str]] = {
    "owner": [WILDCARD],
    "admin": [p for p in PERMISSIONS if p not in ("org:delete", "org:billing")],
    # agent deliberately lacks members:read — it gives RBAC a real, tested deny path.
    # agent gains inbox:manage in P2 - agents work the inbox. The tested RBAC deny
    # path stays agent x members:read, so the P0 test is untouched.
    "agent": [
        "inbox:read",
        "inbox:send",
        "inbox:manage",
        "contacts:read",
        "contacts:write",
        # Agents see consent state and compose from templates; they cannot edit either.
        "compliance:read",
        "templates:read",
    ],
}


def validate_permissions(perms: list[str]) -> list[str]:
    """Reject unknown permission keys loudly. Typos must not silently grant nothing."""
    unknown = [p for p in perms if p != WILDCARD and p not in PERMISSIONS]
    if unknown:
        raise ValidationFailedError(f"Unknown permission keys: {', '.join(sorted(unknown))}")
    return perms


class Role(Base, TenantScoped, TimestampMixin):
    __tablename__ = "roles"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_roles_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    permissions: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    def grants(self, permission: str) -> bool:
        perms = self.permissions or []
        return WILDCARD in perms or permission in perms

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class OrgMembership(Base, TenantScoped, TimestampMixin):
    __tablename__ = "org_memberships"
    __table_args__ = (sa.UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )

    def __repr__(self) -> str:
        return f"<OrgMembership user={self.user_id} org={self.org_id}>"
