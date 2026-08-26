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
