"""P33 agencies: white-label branding per org (custom domain, logo, colour, app name)."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

DOMAIN_STATUSES: tuple[str, ...] = ("pending", "active", "failed")


class OrgBranding(Base, TenantScoped, TimestampMixin):
    __tablename__ = "org_branding"
    __table_args__ = (
        sa.UniqueConstraint("org_id", name="uq_org_branding_org"),
        sa.UniqueConstraint("custom_domain", name="uq_org_branding_domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    logo_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    primary_color: Mapped[str | None] = mapped_column(sa.String(7), nullable=True)
    app_name: Mapped[str | None] = mapped_column(sa.String(63), nullable=True)
    support_email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(sa.String(253), nullable=True)
    #: pending (CNAME not yet seen) | active (CNAME verified; TLS is an operator runbook step
    #: on the shared nginx) | failed
    domain_status: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="pending", server_default="pending"
    )
    domain_checked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
