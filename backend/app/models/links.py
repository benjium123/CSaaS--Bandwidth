"""P28: tracked short links (PUBLIC_WEB_URL/l/{code}) and their clicks."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID


class ShortLink(Base, TenantScoped, TimestampMixin):
    __tablename__ = "short_links"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_short_links_code"),
        sa.Index("ix_short_links_org_message", "org_id", "message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: URL-safe, unguessable (≥ 8 chars from a 62-char alphabet); globally unique because
    #: the redirect route has no org in its path.
    code: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    target_url: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    clicks: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")


class LinkClick(Base, TenantScoped, TimestampMixin):
    __tablename__ = "link_clicks"
    __table_args__ = (sa.Index("ix_link_clicks_link_at", "short_link_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    short_link_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("short_links.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    #: sha256 of the client IP + a daily salt: enough to de-duplicate, never an identity.
    ip_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
