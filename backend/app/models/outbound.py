"""Outbound engine schema (P11): contact lists, campaigns, sends, dial attempts.

Naming (phase-11-plan DR-1): the registration model already owns ``Campaign`` (10DLC),
so everything here is ``outbound_*`` / ``contact_list*``. All tables are TenantScoped.

Idempotency (DR-4): ``outbound_sends`` and ``dial_attempts`` are UNIQUE per
(campaign_id, e164) — enqueueing a contact twice into the same campaign is impossible at
the database, not the application. Retries mutate the ONE row (attempts,
next_attempt_at); they never create a sibling.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

LIST_STATUSES = ("importing", "ready", "failed")
LIST_ROW_STATUSES = ("accepted", "invalid", "duplicate", "dnc")
CAMPAIGN_CHANNELS = ("sms", "voice")
CAMPAIGN_STATUSES = ("draft", "scheduled", "running", "paused", "completed", "cancelled")
DIALER_MODES = ("preview", "power", "parallel", "predictive")

#: Terminal per-row send states. "deferred" is NOT terminal - the compliance gate holds
#: the message and the sweeper releases it; the campaign's job for that row is done once
#: the message row exists, so the campaign marks it terminal-for-the-campaign via "sent"
#: only when a message was actually created. See services/outbound.py.
SEND_STATUSES = ("queued", "sending", "sent", "deferred", "blocked", "failed", "skipped")
SEND_TERMINAL = frozenset({"sent", "deferred", "blocked", "failed", "skipped"})

DIAL_STATUSES = (
    "queued",
    "dialing",
    "connected",
    "no_answer",
    "busy",
    "voicemail",
    "failed",
    "abandoned",
    "completed",
)
#: Dial states that will never be dialed again for this campaign row. no_answer/busy/
#: failed are RETRYABLE (the row returns to queued with a backoff) until attempts run out.
DIAL_TERMINAL = frozenset({"voicemail", "abandoned", "completed"})


class ContactList(Base, TenantScoped, TimestampMixin):
    """An uploaded list. Counts are written once by the import task when it finishes —
    they are a report artifact, not live state, so storing them does not violate the
    "derive, never count" rule for live counters."""

    __tablename__ = "contact_lists"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    source_filename: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="importing")
    error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    total_rows: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    invalid_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    dnc_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ContactListRow(Base, TenantScoped, TimestampMixin):
    """Per-row outcome — THE import report the P11 gate demands."""

    __tablename__ = "contact_list_rows"
    __table_args__ = (sa.Index("ix_list_rows_list_status", "list_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    list_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("contact_lists.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    raw: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    e164: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    #: Populated only when a lookup-capable carrier is configured (plan DR-9). NULL is
    #: "unknown", never a guess.
    line_type: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    #: Canonical extracted fields (first_name, message, ...) per the commit-time mapping.
    #: `raw` keeps the original row verbatim; this is what campaigns render from (DR-14).
    fields: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class OutboundCampaign(Base, TenantScoped, TimestampMixin):
    __tablename__ = "outbound_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    channel: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="sms")
    list_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("contact_lists.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    body: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True
    )
    #: Empty list = sticky sender / deterministic pick over the org's full active pool.
    from_numbers: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    rate_per_minute: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=6)
    daily_cap: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=200)
    respect_warmup: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    start_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # --- voice-only ---------------------------------------------------------------
    dialer_mode: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    parallel_lines: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    local_presence: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # --- retry policy (both channels) ---------------------------------------------
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=2)
    retry_backoff_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=240)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class OutboundSend(Base, TenantScoped, TimestampMixin):
    __tablename__ = "outbound_sends"
    __table_args__ = (
        sa.UniqueConstraint("campaign_id", "e164", name="uq_outbound_sends_campaign_e164"),
        sa.Index("ix_outbound_sends_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("outbound_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    row_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contact_list_rows.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="queued")
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class DialAttempt(Base, TenantScoped, TimestampMixin):
    __tablename__ = "dial_attempts"
    __table_args__ = (
        sa.UniqueConstraint("campaign_id", "e164", name="uq_dial_attempts_campaign_e164"),
        sa.Index("ix_dial_attempts_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("outbound_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    row_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contact_list_rows.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="queued")
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    amd_verdict: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    disposition: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    agent_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
