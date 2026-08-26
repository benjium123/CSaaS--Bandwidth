"""AI agent: agent_profiles + call_transcripts

Purely additive - two new tables.

Revision ID: 0008_agent
Revises: 0007_voice_core
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0008_agent"
down_revision = "0007_voice_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("greeting", sa.String(500), nullable=False, server_default=""),
        sa.Column("voice_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("llm_provider", sa.String(16), nullable=False, server_default=""),
        sa.Column("llm_model", sa.String(64), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("extra", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_agent_profiles_org_name"),
    )

    op.create_table(
        "call_transcripts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "call_id",
            GUID(),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "call_id", "role", "at_ms", name="uq_call_transcripts_call_role_at"
        ),
    )
    op.create_index("ix_call_transcripts_call_at", "call_transcripts", ["call_id", "at_ms"])


def downgrade() -> None:
    op.drop_index("ix_call_transcripts_call_at", table_name="call_transcripts")
    op.drop_table("call_transcripts")
    op.drop_table("agent_profiles")
