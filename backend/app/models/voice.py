"""Voice schema: calls, legs, voice events, recordings.

The load-bearing decision (phase-5-plan): **a call is N legs**. Both carriers fire
callbacks per leg, and a transfer creates a new leg while the old one dies. A single-row
call model corrupts on exactly that case — the first leg's hangup would mark the whole
call completed while the transferred party is still talking. So legs are rows, call status
is DERIVED from them, and both run the same monotonic-rank state machine as messages,
media and registration (the fourth time the shape has been needed, for the same reason:
carriers retry unordered, and a late webhook must never walk a fact backwards).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

# Ranks strictly increase; every rank-40 status is terminal, and one terminal never
# replaces another. "bridged" sits between answered and terminal because a transfer can
# bridge an answered call, but nothing un-bridges one short of ending it.
CALL_STATUS_RANK: dict[str, int] = {
    "queued": 0,
    "initiated": 10,
    "ringing": 20,
    "answered": 30,
    "bridged": 35,
    "completed": 40,
    "failed": 40,
    "busy": 40,
    "no_answer": 40,
    "canceled": 40,
}
TERMINAL_CALL_STATUSES = frozenset({"completed", "failed", "busy", "no_answer", "canceled"})

LEG_STATUS_RANK: dict[str, int] = {
    "created": 0,
    "dialing": 10,
    "ringing": 20,
    "answered": 30,
    "hungup": 40,
    "failed": 40,
}
TERMINAL_LEG_STATUSES = frozenset({"hungup", "failed"})


class Call(Base, TenantScoped, TimestampMixin):
    """One logical conversation, which may span several legs (transfer, AMD, later
    conference). `status` is derived from the legs by the service layer — answered when
    any leg answers, terminal only when EVERY leg is terminal — never set directly from a
    single webhook."""

    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)  # inbound|outbound
    #: Their number and ours, denormalized to the CALL because the answering leg can change
    #: (transfer) while the conversation stays with the same outside party.
    contact_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False, index=True)
    our_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False, index=True)
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="queued")
    #: Populated on the first terminal transition; a terminal status never changes, so
    #: neither does this.
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Sum of leg talk time is NOT call duration (legs overlap during a transfer); this is
    #: wall-clock answered→ended, computed once at the terminal transition.
    duration_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    #: Free-form correlation tag the API caller supplied; echoed on webhooks by carriers.
    tag: Mapped[str | None] = mapped_column(sa.String(128))
    extra: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)

    __table_args__ = (sa.Index("ix_calls_org_created", "org_id", "created_at"),)


class CallLeg(Base, TenantScoped, TimestampMixin):
    """One carrier-side call leg. The carrier's `provider_call_id` identifies a LEG, not
    our Call — every webhook resolves through this table first."""

    __tablename__ = "call_legs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Globally unique per carrier; the webhook resolver's key. Nullable because an
    #: outbound leg exists in our DB before the carrier has assigned it an id.
    provider_call_id: Mapped[str | None] = mapped_column(sa.String(128), index=True)
    to_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    from_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="created")
    #: Why this leg exists: "original" | "transfer" | "amd_retry" (P11).
    reason: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="original")
    #: "human" | "machine" | None. Recorded verbatim from async AMD; ACTING on it is P11.
    amd_result: Mapped[str | None] = mapped_column(sa.String(16))
    answered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    hangup_cause: Mapped[str | None] = mapped_column(sa.String(64))
    extra: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)

    __table_args__ = (
        # One leg per carrier call id. Two carriers could theoretically collide on an id
        # format, so the carrier name participates via the parent call — enforced here as
        # a plain unique on provider_call_id, which both carriers guarantee is a UUID.
        sa.UniqueConstraint("provider_call_id", name="uq_call_legs_provider_call_id"),
    )


class VoiceEvent(Base, TenantScoped, TimestampMixin):
    """Every voice webhook event we accepted, exactly once.

    The unique constraint IS the dedupe (D6): both carriers redeliver, and an application-
    level "have I seen this?" check has a race a DB constraint does not.
    """

    __tablename__ = "voice_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), index=True
    )
    leg_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("call_legs.id", ondelete="CASCADE"), index=True
    )
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Carrier's own event id where it provides one; otherwise a deterministic digest the
    #: adapter computes from (call id, event type, timestamp) so replays still collide.
    provider_event_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint(
            "carrier", "provider_event_id", name="uq_voice_events_carrier_event"
        ),
    )


class CallRecording(Base, TenantScoped, TimestampMixin):
    """A recording we hold OURSELVES. The carrier's URL never reaches the UI: it expires,
    it needs carrier credentials, and serving it would leak those. Same discipline as MMS
    media (P3) - fetch once with carrier auth, store via app/storage, serve from our
    origin behind our auth."""

    __tablename__ = "call_recordings"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leg_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("call_legs.id", ondelete="SET NULL")
    )
    #: Carrier's recording id; dedupe key for redelivered recording_ready events.
    provider_recording_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="audio/mpeg"
    )
    size_bytes: Mapped[int | None] = mapped_column(sa.Integer)
    duration_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    #: "pending" | "stored" | "failed" - the fetch is async; the row exists from the
    #: moment the carrier tells us a recording exists, so nothing is silently lost if the
    #: download fails (it can be retried off this row).
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")

    __table_args__ = (
        sa.UniqueConstraint(
            "provider_recording_id", name="uq_call_recordings_provider_id"
        ),
    )
