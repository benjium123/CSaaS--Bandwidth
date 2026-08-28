"""P10: consent_events monotonic tiebreaker + AI SMS agent surface

Additive - one column on consent_events (the recorded latest_consent coin-flip fix), four
columns on agent_profiles, one on message_threads, and the agent_sms_turns table.

consent_events.seq is backfilled 0: pre-existing rows keep their created_at order, which
is exactly the behaviour they had, while every NEW row gets a strictly-increasing value
so a same-timestamp opt-out/opt-in pair can never again resolve by random UUID.

Revision ID: 0011_consent_seq_sms_agent
Revises: 0010_invites
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0011_consent_seq_sms_agent"
down_revision = "0010_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consent_events",
        sa.Column("seq", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.add_column(
        "agent_profiles",
        sa.Column("sms_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "agent_profiles",
        sa.Column("sms_turn_ceiling", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "agent_profiles",
        sa.Column(
            "sms_handoff_keywords",
            PortableJSON(),
            nullable=False,
            # Matches DEFAULT_SMS_HANDOFF_KEYWORDS in app/models/agent.py.
            server_default='["human", "agent", "representative", "person", "stop the bot"]',
        ),
    )
    op.add_column(
        "agent_profiles",
        sa.Column("sms_max_reply_chars", sa.Integer(), nullable=False, server_default="480"),
    )

    op.add_column(
        "message_threads",
        sa.Column("ai_state", sa.String(12), nullable=False, server_default="off"),
    )
    op.add_column(
        "message_threads",
        sa.Column("ai_armed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "agent_sms_turns",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "thread_id",
            GUID(),
            sa.ForeignKey("message_threads.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "inbound_message_id",
            GUID(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "outbound_message_id",
            GUID(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_sms_turns")
    op.drop_column("message_threads", "ai_armed_at")
    op.drop_column("message_threads", "ai_state")
    op.drop_column("agent_profiles", "sms_max_reply_chars")
    op.drop_column("agent_profiles", "sms_handoff_keywords")
    op.drop_column("agent_profiles", "sms_turn_ceiling")
    op.drop_column("agent_profiles", "sms_enabled")
    op.drop_column("consent_events", "seq")
