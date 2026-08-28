"""AI agent schema: per-org agent profiles and call transcripts.

`call_transcripts` is deliberately its own table, not JSON on the call: P9 trains on
transcripts across thousands of calls, and segments arrive in BATCHES from a worker that
retries — so rows need their own dedupe constraint, exactly like every other
at-least-once ingest in this system.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

#: Default handoff triggers (P10). Matched case-insensitively as whole phrases against an
#: inbound SMS. "stop" is deliberately ABSENT: STOP belongs to the compliance keyword
#: engine, which must keep sole ownership of opt-out semantics.
DEFAULT_SMS_HANDOFF_KEYWORDS = ("human", "agent", "representative", "person", "stop the bot")

#: Outcomes of one AI SMS turn (P10). One row per inbound message the agent considered.
SMS_TURN_STATUSES = ("replied", "skipped", "blocked", "handoff", "error")


class AgentProfile(Base, TenantScoped, TimestampMixin):
    """How this org's AI agent talks. v1 keeps dispatch explicit; the profile only says
    WHAT the agent is, never WHEN it answers (that policy is P9)."""

    __tablename__ = "agent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    system_prompt: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    greeting: Mapped[str] = mapped_column(sa.String(500), nullable=False, default="")
    #: ElevenLabs voice id; empty = the plugin's default voice.
    voice_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    llm_provider: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    #: Spoken after the voicemail beep on outbound drops (P9). Empty = no drop.
    voicemail_message: Mapped[str] = mapped_column(sa.String(500), nullable=False, default="")
    #: Exactly one default per org is enforced in the service layer (partial unique
    #: indexes are not portable to SQLite).
    is_default: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    extra: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)

    # --- P10: the SMS surface ---------------------------------------------------
    #: Off is the default FOREVER — an AI that starts texting customers because a
    #: migration ran is an incident, so enabling is always an explicit operator act.
    sms_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    #: AI replies per thread since last (re)arm before a forced handoff (plan DR-7).
    sms_turn_ceiling: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=10, server_default="10"
    )
    #: Case-insensitive whole-phrase triggers that hand the thread to a human.
    sms_handoff_keywords: Mapped[list] = mapped_column(
        PortableJSON(), nullable=False, default=lambda: list(DEFAULT_SMS_HANDOFF_KEYWORDS)
    )
    #: Hard clamp on one AI reply. 480 chars ≈ 3 GSM-7 segments.
    sms_max_reply_chars: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=480, server_default="480"
    )

    __table_args__ = (
        sa.UniqueConstraint("org_id", "name", name="uq_agent_profiles_org_name"),
    )


class CallTranscriptSegment(Base, TenantScoped, TimestampMixin):
    """One utterance of one call. The worker posts batches at least once; the unique
    constraint makes redelivery a no-op instead of a duplicated conversation."""

    __tablename__ = "call_transcripts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: "user" (the caller) | "agent" (the AI). Human agents' speech is not transcribed
    #: in v1 - only rooms with the AI in them produce transcripts.
    role: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Milliseconds from call start, as reported by the worker's clock. Ordering key.
    at_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "call_id", "role", "at_ms", name="uq_call_transcripts_call_role_at"
        ),
        sa.Index("ix_call_transcripts_call_at", "call_id", "at_ms"),
    )


class AgentSmsTurn(Base, TenantScoped, TimestampMixin):
    """One AI decision about one inbound SMS (P10).

    The UNIQUE inbound_message_id is the idempotency mechanism, not an audit nicety:
    carriers deliver webhooks at least once, and "did we already answer this?" must be a
    constraint the database enforces, never a check-then-act race in the turn engine.
    """

    __tablename__ = "agent_sms_turns"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        sa.ForeignKey("message_threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: Set only for status "replied" (and "handoff" when a farewell message was sent).
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    #: One of SMS_TURN_STATUSES.
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Operator-facing reason: the matched keyword, the gate's verdict, the error class.
    detail: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
