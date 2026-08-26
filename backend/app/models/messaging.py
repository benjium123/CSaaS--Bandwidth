"""Messaging schema.

Every tenant table uses ``TenantScoped`` — that mixin IS the isolation contract (P0 D9).
``webhook_dead_letters`` deliberately is NOT tenant-scoped: a dead letter exists precisely
because no org could be resolved.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

# Status ranks. Monotonic: a transition is allowed only if it strictly increases rank and
# the current state is not terminal. See phase-1-plan DR-5.
STATUS_RANK: dict[str, int] = {
    "queued": 0,
    "accepted": 10,
    "sending": 20,
    "delivered": 30,
    "failed": 30,
    "rejected": 30,
    "received": 30,  # inbound is born terminal
}
TERMINAL_STATUSES = frozenset({"delivered", "failed", "rejected", "received"})

EVENT_TO_STATUS: dict[str, str] = {
    "message-sending": "sending",
    "message-delivered": "delivered",
    "message-failed": "failed",
}


class OrgNumber(Base, TenantScoped, TimestampMixin):
    """A phone number owned by an org. P4 EXTENDS this table; it does not replace it."""

    __tablename__ = "org_numbers"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # Globally unique: a number belongs to exactly one org, ever.
    e164: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True, index=True)
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="bandwidth")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class MessageThread(Base, TenantScoped, TimestampMixin):
    """A pure (org, our number, contact number) bucket.

    Deliberately NOT P2's inbox: no participants, no assignment, no read state, no contact
    linkage. Threading at write time is one upsert; threading retrofitted later is a
    backfill migration.
    """

    __tablename__ = "message_threads"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "our_e164", "contact_e164", name="uq_threads_org_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    our_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    contact_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Message(Base, TenantScoped, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        sa.UniqueConstraint("carrier", "provider_message_id", name="uq_messages_provider_id"),
        sa.Index("ix_messages_org_thread_created", "org_id", "thread_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("message_threads.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)  # outbound|inbound
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    from_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    to_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    body: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    media: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="bandwidth")
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    segment_count_est: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    segment_count_carrier: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class MessageEvent(Base, TenantScoped, TimestampMixin):
    """Idempotency ledger + audit trail.

    The unique constraint below IS the idempotency mechanism — not an application-level
    check. Bandwidth publishes no event id and retries in PARALLEL, so only a database
    constraint is safe under true concurrency.
    """

    __tablename__ = "message_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "carrier", "provider_message_id", "event_type", name="uq_msg_events_dedupe"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    event_time: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    processing_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class WebhookDeadLetter(Base, TimestampMixin):
    """NOT tenant-scoped, by definition: no org could be resolved.

    Never stores the Authorization header.
    """

    __tablename__ = "webhook_dead_letters"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
