"""P30 reports: scheduled report emails and per-org email delivery settings."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

REPORT_KINDS: tuple[str, ...] = ("team", "inbox_sla", "campaigns", "assistant")
REPORT_CADENCES: tuple[str, ...] = ("daily", "weekly", "monthly")
EMAIL_PROVIDERS: tuple[str, ...] = ("smtp", "postmark")


class ReportSchedule(Base, TenantScoped, TimestampMixin):
    __tablename__ = "report_schedules"
    __table_args__ = (sa.Index("ix_report_schedules_org", "org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    report: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    params: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    cadence: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    recipients: Mapped[list] = mapped_column(
        PortableJSON(), nullable=False, default=list, server_default="[]"
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class OrgEmailSettings(Base, TenantScoped, TimestampMixin):
    """One row per org. Credentials Fernet-encrypted (services/credentials.py); probe = one
    real send to the from address."""

    __tablename__ = "org_email_settings"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_org_email_settings_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_address: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="unverified", server_default="unverified"
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_probe_detail: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
