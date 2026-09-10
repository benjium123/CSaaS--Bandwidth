"""P26 inbox pro: private notes on conversations and per-user notifications."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

NOTIFICATION_KINDS: tuple[str, ...] = (
    "mention",
    "assignment",
    "overdue",
    "missed_call",
    "new_inbound",
    "low_balance",
)


class ThreadNote(Base, TenantScoped, TimestampMixin):
    """A private note on a conversation. Never sent to the contact; rendered as a yellow
    card in the timeline. `mentions` = list of user ids that were @-mentioned."""

    __tablename__ = "thread_notes"
    __table_args__ = (sa.Index("ix_thread_notes_org_thread", "org_id", "thread_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("message_threads.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mentions: Mapped[list] = mapped_column(
        PortableJSON(), nullable=False, default=list, server_default="[]"
    )


class Notification(Base, TenantScoped, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "user_id", "dedupe_key", name="uq_notifications_dedupe"),
        sa.Index("ix_notifications_user_unread", "org_id", "user_id", "read_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("message_threads.id", ondelete="CASCADE"), nullable=True
    )
    body: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    #: Optional idempotency key (e.g. "overdue:{thread_id}") so a sweeper cannot spam.
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
