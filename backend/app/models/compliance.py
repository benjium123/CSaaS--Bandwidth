"""Compliance schema.

The load-bearing design choice is in ``ConsentEvent``: the key is
``(org_id, contact_e164, channel)`` and contains **no ``our_e164`` anywhere**. That is what
makes pool-wide suppression structural rather than procedural — there is nothing per-number
to check, so gotcha #1 ("STOP to number A must suppress numbers B, C, D") cannot regress
into a per-number opt-out list.

The table is **APPEND-ONLY**. Current state is derived by reading the latest event, never
denormalized into a flag. A flag would drift under webhook replay; a derived read cannot.
No UPDATE or DELETE statement may ever target this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

# "help_request" is a ledger row, not a consent change: it records that the contact asked
# for help (so the reply is idempotent under webhook replay) without affecting opt-in state.
CONSENT_EVENTS = ("opt_out", "opt_in", "dnc_add", "dnc_remove", "help_request")
CONSENT_SOURCES = ("keyword", "manual", "import", "api")
CHANNELS = ("sms", "voice")

# The federal TCPA floor, in the RECIPIENT's local time. An org may narrow this window;
# it may never widen it.
FEDERAL_WINDOW_START = "08:00"
FEDERAL_WINDOW_END = "21:00"


class ConsentEvent(Base, TenantScoped, TimestampMixin):
    __tablename__ = "consent_events"
    __table_args__ = (
        # Idempotency at the CONSTRAINT level: a replayed inbound webhook cannot record a
        # second opt-out or fire a second auto-reply. NULLs are distinct on both dialects,
        # so manual/api events (no message) are unaffected.
        sa.UniqueConstraint("message_id", name="uq_consent_events_message"),
        sa.Index(
            "ix_consent_org_e164_channel_created",
            "org_id",
            "contact_e164",
            "channel",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    contact_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    channel: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="sms")
    event: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    keyword_matched: Mapped[str | None] = mapped_column(sa.String(31), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class DncEntry(Base, TenantScoped, TimestampMixin):
    """Internal suppression list. Mutable working table — every change also appends a
    ConsentEvent, so the append-only ledger stays the one complete audit trail."""

    __tablename__ = "dnc_entries"
    __table_args__ = (sa.UniqueConstraint("org_id", "e164", name="uq_dnc_org_e164"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="manual")
    reason: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ComplianceBlock(Base, TenantScoped, TimestampMixin):
    """Audit row written (and committed) whenever the gate denies a send.

    Committed before the error is raised, so the record survives the exception. P1 DR-8
    assigned this ledger to P3.
    """

    __tablename__ = "compliance_blocks"
    __table_args__ = (
        sa.Index("ix_compliance_blocks_org_e164_created", "org_id", "contact_e164", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    contact_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    from_e164: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    reason: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    body_excerpt: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    exemption: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class ComplianceSettings(Base, TenantScoped, TimestampMixin):
    """One row per org, lazily created with defaults on first read.

    ``window_start``/``window_end`` describe the **allowed** sending window in the
    recipient's local time — named that way deliberately to avoid the classic inversion
    bug where "quiet hours 08:00-21:00" gets read as the blocked period.
    """

    __tablename__ = "compliance_settings"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_compliance_settings_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    window_start: Mapped[str] = mapped_column(
        sa.String(5), nullable=False, default=FEDERAL_WINDOW_START
    )
    window_end: Mapped[str] = mapped_column(
        sa.String(5), nullable=False, default=FEDERAL_WINDOW_END
    )
    help_contact: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    optout_text: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
        default=(
            "You are unsubscribed from {org} messages. No more messages will be sent. "
            "Reply START to resubscribe. Reply HELP for help."
        ),
    )
    optin_text: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
        default="You are resubscribed to {org} messages. Reply STOP to unsubscribe.",
    )
    help_text: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
        default=(
            "{org}: for help contact {help_contact}. Msg & data rates may apply. "
            "Reply STOP to unsubscribe."
        ),
    )
    quiet_hours_enforced: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True
    )


class MediaAsset(Base, TenantScoped, TimestampMixin):
    """An MMS attachment, inbound or outbound.

    Inbound assets are created ``pending`` inside the webhook's deduped transaction and
    fetched LATER, outside the request path — Bandwidth only hosts media ~48h, so we
    re-host, but the ingestion path must stay DB-only and 2xx-fast (D6).
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        sa.Index("ix_media_assets_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="outbound")
    storage_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(sa.String(127), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    fetch_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
