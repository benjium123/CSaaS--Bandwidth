"""P27 contacts pro: saved views, retention policy, subject-erasure requests."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

ERASURE_STATUSES: tuple[str, ...] = ("pending", "done", "failed")


class SavedView(Base, TenantScoped, TimestampMixin):
    """A named filter set on the Contacts page. user_id NULL = shared with the workspace."""

    __tablename__ = "saved_views"
    __table_args__ = (sa.Index("ix_saved_views_org_user", "org_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    filters: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    sort: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class RetentionPolicy(Base, TenantScoped, TimestampMixin):
    """One row per org. NULL = keep forever. Defaults: messages forever, recordings 90 d,
    transcripts 365 d, imports 30 d. The sweeper purges past the window (closes D4)."""

    __tablename__ = "retention_policies"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_retention_policies_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    messages_days: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    recordings_days: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, default=90, server_default="90"
    )
    transcripts_days: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, default=365, server_default="365"
    )
    imports_days: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, default=30, server_default="30"
    )


class ErasureRequest(Base, TenantScoped, TimestampMixin):
    """"Erase this person": anonymise contact fields, message bodies and transcripts, delete
    recordings; compliance ledger rows (opt-outs, consent) are kept - the law requires the
    opt-out to survive the erasure."""

    __tablename__ = "erasure_requests"
    __table_args__ = (sa.Index("ix_erasure_requests_org_status", "org_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending", server_default="pending"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    summary: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
