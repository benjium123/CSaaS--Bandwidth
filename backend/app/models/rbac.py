"""Roles, permissions and org membership.

Permissions are a code-defined catalogue stored as a JSON list on the role row. That is a
deliberate P0 simplification: there is no custom-role editor yet, so a join table would buy
nothing but migrations. Normalize when custom roles ship.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

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
    # P22: read_all bypasses orgs.contact_visibility; assign changes owner/department.
    # Owner via wildcard; admin via the comprehension below + migration 0024 backfill;
    # agents get neither (a lead's extra reach comes from department_members.is_lead).
    "contacts:read_all",
    "contacts:assign",
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

#: A role that can change who else has access, or move money. Owners hold the wildcard;
#: admins hold members:update and roles:write; agents hold none of it. This is the SINGLE
#: source of truth - orgs.py, scim.py and sso_provisioning.py each used to keep their own
#: hand-synchronised copy. Import this instead of re-declaring it.
#:
#: NOTE: services/passkey_policy.py deliberately keeps a DIFFERENT, narrower set (no
#: roles:write) for who must use a phishing-resistant passkey. That is a separate product
#: decision; do not collapse the two without deciding to widen the passkey mandate.
PRIVILEGED_PERMISSIONS = frozenset({"org:billing", "members:update", "roles:write"})


def is_privileged_permissions(perms: Iterable[str] | None) -> bool:
    """True for a permission set that can grant access to others or spend money."""
    granted = set(perms or [])
    return WILDCARD in granted or bool(granted & PRIVILEGED_PERMISSIONS)

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
        # P16: the unified inbox timeline shows calls alongside SMS. An agent who may work
        # an inbox may read its calls; P15 grants still scope WHICH numbers.
        "calls:read",
        # An employee handed a line is handed the WHOLE line - this product's premise is
        # calls and texts on one number, and an agent who could text from a line but not
        # ring back from it had half a phone. Operator decision, 18 Sept 2026.
        #
        # This grants the CAPABILITY, never the reach: resolve_access still decides which
        # numbers, and `create_call` refuses any `from` the caller lacks `can_use` on, so
        # an agent can only dial out from a line they hold a `member` grant for. A
        # `viewer` grant is still read-only.
        #
        # It also does not weaken the two gates that actually bound spend and abuse -
        # the prepaid hard gate (402) and monitoring selection both sit inside
        # voice_plane/service.py, BELOW this permission check, so they apply to every
        # caller whatever their role.
        #
        # Existing orgs are backfilled by migration 0052; SYSTEM_ROLES only seeds new ones.
        "calls:place",
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
    #: This member's last dragged order for the Lines rail - a list of inbox id strings.
    #: NULL (never customized) falls back to a computed default; an id no longer visible
    #: to this member (access revoked) is simply skipped when the list is rendered.
    inbox_order: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)

    def __repr__(self) -> str:
        return f"<OrgMembership user={self.user_id} org={self.org_id}>"
