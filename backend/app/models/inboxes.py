"""P15: departments and tiered inbox access.

The access model is fail-closed and three-tiered:

  * A user whose role grants ``inboxes:admin`` (owner wildcard, system admin) sees and
    manages EVERY inbox in the org.
  * Everyone else sees exactly the union of inboxes granted to them directly and inboxes
    granted to a department they belong to. No grant, no visibility — an ungranted number
    is admin-only. This mirrors the deliberate "two-gate fail-closed" stance: a missing
    row must never widen access.

An ``Inbox`` is a 1:1 wrapper around an ``OrgNumber``. It exists as its own row (rather
than columns on org_numbers) because grants, naming and future per-inbox settings hang off
it, and because numbers are never deleted while an inbox's sharing config is freely
editable. Every number gets its inbox at creation time (migration 0016 backfills existing
numbers); services must treat a missing inbox row as a bug, not a branch.

``InboxGrant.role``: "member" may read AND send/dial from the inbox's number; "viewer" is
read-only. When a user holds both (e.g. viewer directly, member via department) the more
permissive grant wins — grants only ever ADD capability.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

GRANTEE_TYPES: tuple[str, ...] = ("department", "user")
INBOX_GRANT_ROLES: tuple[str, ...] = ("member", "viewer")


class Department(Base, TenantScoped, TimestampMixin):
    __tablename__ = "departments"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_departments_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Department {self.name}>"


class DepartmentMember(Base, TenantScoped, TimestampMixin):
    __tablename__ = "department_members"
    __table_args__ = (
        sa.UniqueConstraint("department_id", "user_id", name="uq_dept_members_dept_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    department_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<DepartmentMember dept={self.department_id} user={self.user_id}>"


class Inbox(Base, TenantScoped, TimestampMixin):
    __tablename__ = "inboxes"
    __table_args__ = (
        # 1:1 with a number. The unique constraint IS the contract — services may join
        # through it without de-duplicating.
        sa.UniqueConstraint("number_id", name="uq_inboxes_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    number_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("org_numbers.id", ondelete="CASCADE"), nullable=False
    )
    #: Optional UI accent (hex string or token). Purely cosmetic.
    color: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)

    def __repr__(self) -> str:
        return f"<Inbox {self.name}>"


class InboxGrant(Base, TenantScoped, TimestampMixin):
    __tablename__ = "inbox_grants"
    __table_args__ = (
        sa.UniqueConstraint(
            "inbox_id", "grantee_type", "grantee_id", name="uq_inbox_grants_inbox_grantee"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    inbox_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("inboxes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: "department" | "user". No FK on grantee_id — it points at departments.id or
    #: users.id depending on grantee_type. Services validate existence at write time;
    #: department deletion cascades are handled by the deliberate cleanup in the service
    #: layer (see services/inbox_access.py), not the database.
    grantee_type: Mapped[str] = mapped_column(sa.String(12), nullable=False)
    grantee_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: "member" (read + send/dial) | "viewer" (read-only).
    role: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="member")

    def __repr__(self) -> str:
        return f"<InboxGrant inbox={self.inbox_id} {self.grantee_type}={self.grantee_id}>"
