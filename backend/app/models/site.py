"""Public website: sales enquiries and the chat assistant's handoffs to a person.

Not tenant-scoped: visitors have no workspace. Only platform operators read these rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID

SITE_CHAT_STATUSES: tuple[str, ...] = ("waiting", "active", "closed")
SITE_CHAT_ROLES: tuple[str, ...] = ("visitor", "assistant", "agent", "system")


class SiteLead(Base, TimestampMixin):
    """A "Talk to sales" form submission."""

    __tablename__ = "site_leads"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(254), nullable=False)
    phone: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    company: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    team_size: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    numbers_needed: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    switching_from: Mapped[str | None] = mapped_column(sa.String(60), nullable=True)
    message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    sms_consent: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    plan: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    page: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="new", server_default="new", index=True
    )


class SiteChat(Base, TimestampMixin):
    """A website chat a visitor asked to hand to a person. The visitor holds a bearer token;
    only its SHA-256 is stored."""

    __tablename__ = "site_chats"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="waiting", server_default="waiting", index=True
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(254), nullable=False)
    phone: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    sms_consent: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    reason: Mapped[str | None] = mapped_column(sa.String(60), nullable=True)
    page: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, index=True
    )
    #: 0095: set when a signed-in customer started the chat from the console (NULL for an
    #: anonymous website visitor). Taken from the authenticated session, never the client.
    org_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The operator who owns the chat; NULL = every operator is alerted.
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    #: Last time an operator opened the chat; unread = last_visitor_at is newer.
    agent_read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_visitor_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    #: 0096: NULL = no assistant, "active" = the assistant answers alone, "assist" = it answers
    #: while the team is alerted (a person asked for out of hours), "handoff" = it passed the
    #: chat to the team, "off" = an operator took over (routes/site.py, services/support_agent.py).
    ai_state: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)


class SiteChatMessage(Base):
    __tablename__ = "site_chat_messages"
    __table_args__ = (sa.Index("ix_site_chat_messages_chat_created", "chat_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("site_chats.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
