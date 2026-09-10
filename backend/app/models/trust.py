"""P36 trust: per-org data keys for envelope encryption, public status incidents."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

STATUS_COMPONENTS: tuple[str, ...] = ("api", "db", "redis", "media_plane", "messaging", "voice")
STATUS_SEVERITIES: tuple[str, ...] = ("minor", "major", "critical")


class OrgDataKey(Base, TenantScoped, TimestampMixin):
    """A per-org data-encryption key, wrapped by CREDENTIALS_MASTER_KEY. `version` lets a
    rotation job re-wrap/re-encrypt while old ciphertexts still decrypt."""

    __tablename__ = "org_data_keys"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "version", name="uq_org_data_keys_org_version"),
        sa.Index("ix_org_data_keys_org_current", "org_id", "is_current"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    wrapped_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_current: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    rotated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class StatusIncident(Base, TimestampMixin):
    """Platform-wide (not tenant-scoped): shown on the public status page."""

    __tablename__ = "status_incidents"
    __table_args__ = (sa.Index("ix_status_incidents_started", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    component: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    severity: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
