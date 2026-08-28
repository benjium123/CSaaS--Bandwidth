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

    # ---- P4 -----------------------------------------------------------------------
    #: "local" | "tollfree". Decides WHICH registration regime gates this number:
    #: 10DLC for local, toll-free verification for toll-free. They are not interchangeable.
    number_type: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="local")
    #: What the carrier says this number can do. Declared by the carrier at order time,
    #: never inferred from the number itself.
    capabilities: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: "active" | "pending" | "released". Numbers are NEVER deleted (phase-4-plan DR-5):
    #: threads, messages and the consent ledger all point here, and the ledger is the
    #: evidence that somebody opted out.
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    #: The carrier's own id for this number, needed to configure or release it later.
    provider_ref: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    #: 10DLC campaign. NULL means this number may not send - see compliance.registration.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class MessageThread(Base, TenantScoped, TimestampMixin):
    """A pure (org, our number, contact number) bucket.

    Deliberately NOT P2's inbox: no participants, no assignment, no read state, no contact
    linkage. Threading at write time is one upsert; threading retrofitted later is a
    backfill migration.
    """

    __tablename__ = "message_threads"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "our_e164", "contact_e164", name="uq_threads_org_pair"),
        sa.Index("ix_threads_org_status_last", "org_id", "status", "last_message_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    our_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    contact_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # --- P2 conversation state -------------------------------------------------
    # SET NULL: threads and messages are immutable comms records. Deleting a contact
    # must never delete the history of what was said.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="open")
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Read state is DERIVED from this cursor, never counted. A counter incremented from a
    # webhook handler is exactly the "increment side effect" ARCHITECTURE D6 bans, and it
    # would drift on replay. A derived count cannot.
    last_read_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # --- P10 AI state machine (plan DR-5): off | active | handed_off ------------
    # `off` until an operator arms the thread's org profile; `handed_off` is sticky —
    # only the explicit re-arm endpoint returns a thread to `active`, never the bot.
    ai_state: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, default="off", server_default="off"
    )
    # Set every time ai_state becomes "active" (auto-arm and manual re-arm alike). The
    # turn ceiling counts replies AFTER this instant — counting "since the last
    # bot-initiated handoff" instead would make a human takeover + re-arm inherit the old
    # count and trip the ceiling on the very next inbound (DR-7 says "since last (re)arm").
    ai_armed_at: Mapped[datetime | None] = mapped_column(
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
    # Set when quiet hours DEFER a send. The message row exists and is queued; the
    # sweeper releases it and RE-RUNS THE FULL GATE, so an opt-out landing during the
    # hold still kills the send. Gate at dispatch, never only at enqueue.
    hold_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, index=True
    )


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


class MessageTemplate(Base, TenantScoped, TimestampMixin):
    """A reusable message body with {{merge}} fields.

    Templates never touch the carrier themselves - they render to text which then goes
    through the normal send API, so they cannot bypass the compliance gate.
    """

    __tablename__ = "message_templates"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_templates_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    media_asset_ids: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
