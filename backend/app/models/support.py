"""Customer support requests (Help menu -> Contact support) and platform-wide settings."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

SUPPORT_STATUSES: tuple[str, ...] = ("open", "answered", "closed")


class SupportRequest(Base, TenantScoped, TimestampMixin):
    """A question a workspace member sent from the Help menu; ops answer by email."""

    __tablename__ = "support_requests"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Reply-to address captured at send time (the user may change theirs later).
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Where in the app it was sent from (path), to help ops reproduce.
    page: Mapped[str | None] = mapped_column(sa.String(255))
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="open", server_default="open", index=True
    )
    reply: Mapped[str | None] = mapped_column(sa.Text)
    replied_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class PlatformSetting(Base, TimestampMixin):
    """Platform-wide key -> JSON value (not tenant data), edited by ops."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
