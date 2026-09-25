"""Fax (billing v2, B7): one row per fax sent or received, and every carrier event seen.

A fax's document lives in the object store under ``media_key`` (Telnyx media URLs expire in
minutes, so an inbound fax is downloaded the moment it arrives). Money: an outbound fax holds
pages x price at send, is charged the carrier's page count on delivery, released on failure;
an inbound fax is charged on receipt.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

FAX_STATUSES = ("queued", "sending", "delivered", "failed", "receiving", "received")


class Fax(Base, TenantScoped, TimestampMixin):
    __tablename__ = "faxes"
    __table_args__ = (sa.Index("ix_faxes_org_created", "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="queued")
    from_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    to_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    provider_fax_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    media_key: Mapped[str | None] = mapped_column(sa.String(255))
    media_name: Mapped[str | None] = mapped_column(sa.String(255))
    failure_reason: Mapped[str | None] = mapped_column(sa.String(255))
    charged_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class FaxEvent(Base):
    """Every fax webhook, deduplicated on the carrier's event id."""

    __tablename__ = "fax_events"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    provider_fax_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    payload: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
