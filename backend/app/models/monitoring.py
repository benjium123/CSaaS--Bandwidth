"""P43 traffic monitoring: the AI watches texts and calls so verified businesses can't hide
scam traffic inside normal traffic.

Tenant-scoped (one workspace's traffic):
  text_verdicts      cached safety verdict per normalised message body
  monitor_signals    every risk signal (blocked text, scam call, complaint, STOP spike, ...)
  org_monitoring     the workspace's running score, level and pause state + AI case file
  call_reviews       AI review of one call's transcript
Platform-wide (NOT TenantScoped):
  number_reports     the public "report a number" form (the reporter isn't a tenant)
  monitor_labels     operator decisions, exported into the AI exam library
  monitor_health     canary and exam results that prove the monitor still works
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

#: messages.moderation_state
MODERATION_STATES: tuple[str, ...] = ("allowed", "held", "blocked", "cleared", "exempt")
TEXT_VERDICTS: tuple[str, ...] = ("allow", "hold", "block")
MONITOR_LEVELS: tuple[str, ...] = ("normal", "watch", "restricted", "paused")
CALL_REVIEW_STATUSES: tuple[str, ...] = ("pending", "reviewed", "skipped", "error")
CALL_VERDICTS: tuple[str, ...] = ("ok", "suspicious", "scam")
SIGNAL_KINDS: tuple[str, ...] = (
    "text_blocked",
    "text_held_confirmed",
    "text_unchecked_flagged",
    "call_suspicious",
    "call_scam",
    "complaint_reply",
    "stop_rate",
    "short_calls",
    "no_answer_rate",
    "volume_spike",
    "carrier_spam_flag",
    "public_report",
)


class TextVerdict(Base, TenantScoped, TimestampMixin):
    """One verdict per distinct message body (per workspace), so a campaign of 10,000
    identical texts costs one AI call and every recipient gets the same decision."""

    __tablename__ = "text_verdicts"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "body_hash", name="uq_text_verdicts_org_body"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    body_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    category: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    #: rules | ai | second_look | operator | unchecked
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    confidence: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class MonitorSignal(Base, TenantScoped, TimestampMixin):
    __tablename__ = "monitor_signals"
    __table_args__ = (sa.Index("ix_monitor_signals_org_created", "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    weight: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    summary: Mapped[str] = mapped_column(sa.String(500), nullable=False, default="")
    detail: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)


class OrgMonitoring(Base, TenantScoped, TimestampMixin):
    __tablename__ = "org_monitoring"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_org_monitoring_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    score: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    level: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="normal", server_default="normal"
    )
    level_changed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: The AI-written case file shown to the operator when an account is paused.
    case_file: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    #: Customer's appeal text, if they asked for a review.
    appeal: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    appealed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    #: Signals older than this were cleared by an operator (unpause) and no longer count.
    cleared_before: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class CallReview(Base, TenantScoped, TimestampMixin):
    __tablename__ = "call_reviews"
    __table_args__ = (sa.UniqueConstraint("call_id", name="uq_call_reviews_call"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    #: why this call was chosen: new_account | watch | sample
    reason: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    verdict: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    confidence: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    summary: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
    evidence: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class NumberReport(Base, TimestampMixin):
    """A member of the public reporting a call or text from one of our numbers."""

    __tablename__ = "number_reports"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    reported_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False, index=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # call | text
    description: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    reporter_contact: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    #: The AI's read of the report (credible? what kind of scam?).
    assessment: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)


class MonitorLabel(Base, TimestampMixin):
    """An operator's decision on something the monitor flagged - the truth the AI exam is
    built from."""

    __tablename__ = "monitor_labels"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # text | call
    label: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # scam | legit
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MonitorHealth(Base, TimestampMixin):
    """Proof the monitor works today: canary runs and exam scores."""

    __tablename__ = "monitor_health"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)  # canary|exam
    passed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    detail: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
