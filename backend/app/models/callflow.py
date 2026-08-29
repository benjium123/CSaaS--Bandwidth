"""IVR / queues / voicemail schema (P12). All tables TenantScoped.

Flows are VERSIONED (phase-12-plan DR-3): a definition is immutable once saved; editing
creates a new version row. A call pins the version it started with, so a mid-call edit
can never change a call in flight.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

FLOW_STATUSES = ("draft", "active", "archived")
RING_STRATEGIES = ("simultaneous", "sequential")
QUEUE_OVERFLOWS = ("voicemail", "hangup", "callback")
QUEUE_ENTRY_STATES = (
    "waiting",
    "offered",
    "connected",
    "abandoned",
    "overflowed",
    "callback_requested",
)
#: States after which a queue entry is never offered again.
QUEUE_ENTRY_TERMINAL = frozenset({"connected", "abandoned", "overflowed", "callback_requested"})
VOICEMAIL_TRANSCRIPT_STATUSES = ("pending", "done", "failed", "disabled")


class CallFlow(Base, TenantScoped, TimestampMixin):
    __tablename__ = "call_flows"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "name", "version", name="uq_call_flows_org_name_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    #: The validated flow graph (services/flow_engine.py shape). Immutable per version.
    definition: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class RingGroupDef(Base, TenantScoped, TimestampMixin):
    """Named ring group. `RingGroup` the ACTION name lives in flow_engine; this is the
    stored definition, hence the Def suffix."""

    __tablename__ = "ring_groups"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_ring_groups_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    strategy: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="simultaneous")
    member_user_ids: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    ring_timeout_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=20)


class CallQueue(Base, TenantScoped, TimestampMixin):
    __tablename__ = "call_queues"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_call_queues_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    hold_audio_url: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    max_wait_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=300)
    overflow: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="voicemail")
    ring_group_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("ring_groups.id", ondelete="SET NULL"), nullable=True
    )


class QueueEntry(Base, TenantScoped, TimestampMixin):
    """Position is DERIVED (count of earlier waiting entries) — never stored (DR-6)."""

    __tablename__ = "queue_entries"
    __table_args__ = (sa.Index("ix_queue_entries_queue_state", "queue_id", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    queue_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("call_queues.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="waiting")
    offered_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    offered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    callback_e164: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class BusinessHours(Base, TenantScoped, TimestampMixin):
    """Voice business hours — deliberately NOT ComplianceSettings (DR-10): SMS quiet
    hours are recipient-local law; these are the org's own opening hours."""

    __tablename__ = "business_hours"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_business_hours_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False, default="default")
    timezone: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="America/Chicago")
    #: {"mon": [["09:00","17:00"]], ...} - lists of [open, close] windows per weekday key
    #: mon..sun. A missing/empty day is closed.
    schedule: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: ISO dates ("2026-12-25") treated as the `holiday` branch regardless of weekday.
    holidays: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)


class Voicemail(Base, TenantScoped, TimestampMixin):
    __tablename__ = "voicemails"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recording_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("call_recordings.id", ondelete="SET NULL"), nullable=True
    )
    greeting_node: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    transcript: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    transcript_status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending"
    )
    status: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="new")
