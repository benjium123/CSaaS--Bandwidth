"""P9: appointments, knowledge base, voicemail_message on agent_profiles

Additive - three new tables + one nullable-with-default column.

Revision ID: 0009_appointments_kb
Revises: 0008_agent
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0009_appointments_kb"
down_revision = "0008_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appointments",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("call_id", GUID(), sa.ForeignKey("calls.id", ondelete="SET NULL")),
        sa.Column("contact_e164", sa.String(20), nullable=False, index=True),
        sa.Column("raw_when", sa.String(255), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="booked"),
        sa.Column("created_by", sa.String(64), nullable=False, server_default="ai"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source", sa.String(255), nullable=False, server_default="pasted"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "title", name="uq_kb_documents_org_title"),
    )

    op.create_table(
        "kb_chunks",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "document_id",
            GUID(),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "seq", name="uq_kb_chunks_doc_seq"),
    )

    op.add_column(
        "agent_profiles",
        sa.Column("voicemail_message", sa.String(500), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "voicemail_message")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    op.drop_table("appointments")
