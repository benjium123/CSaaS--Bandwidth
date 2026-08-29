"""Platform services schema (P13): API keys, outbox + outbound webhooks, audit log,
call scores, usage rollups.

- API keys store a SHA-256 hash only (phase-13-plan DR-3) - never the secret.
- Webhook endpoint secrets are Fernet-encrypted (the deliverer must sign with them),
  same mechanism as 2FA secrets (auth/security.encrypt_credential).
- platform_events is a DURABLE OUTBOX (DR-4): rows are written in the same transaction
  as the domain change. The in-process bus stays UI-freshness only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

API_KEY_STATUSES = ("active", "revoked")
WEBHOOK_ENDPOINT_STATUSES = ("active", "disabled")
DELIVERY_STATUSES = ("pending", "delivered", "failed", "dead")
SCORE_STATUSES = ("pending", "done", "failed", "disabled")
#: v1 outbox event catalogue (DR-4). Endpoints may subscribe to any subset.
PLATFORM_EVENT_TYPES = (
    "message.received",
    "message.finalized",
    "call.completed",
    "voicemail.created",
    "campaign.completed",
    "appointment.booked",
)
USAGE_METRICS = (
    "sms_segments",
    "mms_messages",
    "voice_minutes",
    "ai_sms_turns",
    "ai_tokens",
    "storage_bytes",
)
#: Delivery retry backoff schedule in seconds (DR-5); after the last, status = dead.
DELIVERY_BACKOFF_SECONDS = (60, 300, 1800, 7200, 43200)


class ApiKey(Base, TenantScoped, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    #: First segment shown in the UI so operators can tell keys apart. Globally unique
    #: so hash lookup is a single indexed fetch.
    prefix: Mapped[str] = mapped_column(sa.String(16), nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    scopes: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PlatformEvent(Base, TenantScoped, TimestampMixin):
    __tablename__ = "platform_events"
    __table_args__ = (sa.Index("ix_platform_events_org_created", "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class WebhookEndpoint(Base, TenantScoped, TimestampMixin):
    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    #: Fernet ciphertext of the signing secret (shown once at creation).
    secret_encrypted: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    event_types: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    #: Consecutive failed DELIVERY ATTEMPTS across rows; reset on any 2xx. At 20 the
    #: endpoint auto-disables (DR-5) with an audit row.
    failure_streak: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class WebhookDelivery(Base, TenantScoped, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        sa.UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_ep_event"),
        sa.Index("ix_webhook_deliveries_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("platform_events.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_status_code: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class AuditLogEntry(Base, TenantScoped, TimestampMixin):
    __tablename__ = "audit_log"
    __table_args__ = (sa.Index("ix_audit_log_org_created", "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(sa.String(48), nullable=False, default="")
    target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    #: Human-relevant context only. NEVER secrets, tokens, or full request bodies.
    detail: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class CallScore(Base, TenantScoped, TimestampMixin):
    __tablename__ = "call_scores"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    sentiment: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    score: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")


class UsageRecord(Base, TenantScoped, TimestampMixin):
    """Daily rollup per metric. Recomputing a day REPLACES quantity (derived, never
    incremented — DR-2). carrier_quantity carries the carrier-reported side where one
    exists (sms_segments), for the reconciliation report."""

    __tablename__ = "usage_records"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "period_date", "metric", name="uq_usage_org_date_metric"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    period_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    metric: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    carrier_quantity: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
