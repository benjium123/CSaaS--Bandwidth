"""P9 schema: appointments and the org knowledge base.

`raw_when` is stored alongside the parsed timestamp on purpose: the LLM's normalization
of "tomorrow at 3" is a guess, and when it guesses wrong the human fixing the appointment
needs to see what the CALLER actually said, not only the wrong parse.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID


class Appointment(Base, TenantScoped, TimestampMixin):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="SET NULL")
    )
    contact_e164: Mapped[str] = mapped_column(sa.String(20), nullable=False, index=True)
    #: What was actually said/asked for, verbatim.
    raw_when: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    #: The parsed interpretation - nullable because a raw string that does not parse is
    #: still a booking worth keeping (a human resolves it).
    scheduled_for: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    #: booked | canceled | done
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="booked")
    #: "ai" or the user id that created it.
    created_by: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="ai")


class KbDocument(Base, TenantScoped, TimestampMixin):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="pasted")

    __table_args__ = (
        sa.UniqueConstraint("org_id", "title", name="uq_kb_documents_org_title"),
    )


class KbChunk(Base, TenantScoped, TimestampMixin):
    """Retrieval unit. v1 search is keyword scoring over `text` (honest about it);
    pgvector is the upgrade path and gets its own column+migration when it comes."""

    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("document_id", "seq", name="uq_kb_chunks_doc_seq"),
    )
