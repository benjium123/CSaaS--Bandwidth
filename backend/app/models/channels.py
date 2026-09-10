"""P35 channels: email / WhatsApp connections and embeddable web-chat widgets."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

THREAD_CHANNELS: tuple[str, ...] = ("sms", "call", "email", "whatsapp", "webchat")
CHANNEL_KINDS: tuple[str, ...] = ("email", "whatsapp")
CHANNEL_PROVIDERS: dict[str, tuple[str, ...]] = {
    "email": ("smtp_imap", "postmark"),
    "whatsapp": ("meta", "twilio"),
}


class ChannelAccount(Base, TenantScoped, TimestampMixin):
    __tablename__ = "channel_accounts"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "kind", "label", name="uq_channel_accounts_org_kind_label"),
        sa.Index("ix_channel_accounts_org_kind", "org_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    label: Mapped[str] = mapped_column(
        sa.String(127), nullable=False, default="", server_default=""
    )
    credentials_encrypted: Mapped[str] = mapped_column(sa.Text, nullable=False)
    config: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="unverified", server_default="unverified"
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_probe_detail: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)


class WebchatWidget(Base, TenantScoped, TimestampMixin):
    __tablename__ = "webchat_widgets"
    __table_args__ = (
        sa.UniqueConstraint("key", name="uq_webchat_widgets_key"),
        sa.Index("ix_webchat_widgets_org", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: Public, unguessable key embedded in the script tag; org resolves from it.
    key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    name: Mapped[str] = mapped_column(
        sa.String(63), nullable=False, default="Website chat", server_default="Website chat"
    )
    allowed_origins: Mapped[list] = mapped_column(
        PortableJSON(), nullable=False, default=list, server_default="[]"
    )
    theme: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    inbox_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("inboxes.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
