"""P34: connected integrations (HubSpot, Salesforce, Zapier, generic) and their sync log."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

INTEGRATION_KINDS: tuple[str, ...] = ("hubspot", "salesforce", "zapier", "generic_webhook")
INTEGRATION_STATUSES: tuple[str, ...] = ("unverified", "active", "failed", "disabled")
SYNC_DIRECTIONS: tuple[str, ...] = ("pull", "push", "both")


class Integration(Base, TenantScoped, TimestampMixin):
    __tablename__ = "integrations"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "kind", "label", name="uq_integrations_org_kind_label"),
        sa.Index("ix_integrations_org_kind", "org_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    label: Mapped[str] = mapped_column(
        sa.String(127), nullable=False, default="", server_default=""
    )
    credentials_encrypted: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    config: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="unverified", server_default="unverified"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class IntegrationSyncLog(Base, TenantScoped, TimestampMixin):
    __tablename__ = "integration_sync_log"
    __table_args__ = (
        sa.Index("ix_integration_sync_log_integration", "integration_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    counts: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    error: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
