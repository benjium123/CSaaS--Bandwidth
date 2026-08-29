"""P11: outbound engine - contact lists, campaigns, sends, dial attempts.

Additive. One column on org_numbers (warmup_started_at, NULL = no ramp so every
pre-existing number keeps full throughput), five new tables. The UNIQUE
(campaign_id, e164) constraints on outbound_sends / dial_attempts ARE the
double-enqueue protection (phase-11-plan DR-4) - database, not application.

Revision ID: 0012_outbound_engine
Revises: 0011_consent_seq_sms_agent
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0012_outbound_engine"
down_revision = "0011_consent_seq_sms_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "org_numbers",
        sa.Column("warmup_started_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "contact_lists",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("source_filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="importing"),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dnc_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "contact_list_rows",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "list_id",
            GUID(),
            sa.ForeignKey("contact_lists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw", PortableJSON(), nullable=False),
        sa.Column("e164", sa.String(20), nullable=True),
        sa.Column(
            "contact_id",
            GUID(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("line_type", sa.String(16), nullable=True),
        sa.Column("fields", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_list_rows_list_status", "contact_list_rows", ["list_id", "status"])

    op.create_table(
        "outbound_campaigns",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False, server_default="sms"),
        sa.Column(
            "list_id",
            GUID(),
            sa.ForeignKey("contact_lists.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "template_id",
            GUID(),
            sa.ForeignKey("message_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("from_numbers", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("rate_per_minute", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("respect_warmup", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dialer_mode", sa.String(16), nullable=True),
        sa.Column("parallel_lines", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("local_presence", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("retry_backoff_minutes", sa.Integer(), nullable=False, server_default="240"),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "outbound_sends",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "campaign_id",
            GUID(),
            sa.ForeignKey("outbound_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "row_id",
            GUID(),
            sa.ForeignKey("contact_list_rows.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "contact_id",
            GUID(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("e164", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column(
            "message_id",
            GUID(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "e164", name="uq_outbound_sends_campaign_e164"),
    )
    op.create_index(
        "ix_outbound_sends_campaign_status", "outbound_sends", ["campaign_id", "status"]
    )

    op.create_table(
        "dial_attempts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "campaign_id",
            GUID(),
            sa.ForeignKey("outbound_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "row_id",
            GUID(),
            sa.ForeignKey("contact_list_rows.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "contact_id",
            GUID(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("e164", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column(
            "call_id", GUID(), sa.ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("amd_verdict", sa.String(16), nullable=True),
        sa.Column("disposition", sa.String(32), nullable=True),
        sa.Column(
            "agent_user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "e164", name="uq_dial_attempts_campaign_e164"),
    )
    op.create_index(
        "ix_dial_attempts_campaign_status", "dial_attempts", ["campaign_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_dial_attempts_campaign_status", table_name="dial_attempts")
    op.drop_table("dial_attempts")
    op.drop_index("ix_outbound_sends_campaign_status", table_name="outbound_sends")
    op.drop_table("outbound_sends")
    op.drop_table("outbound_campaigns")
    op.drop_index("ix_list_rows_list_status", table_name="contact_list_rows")
    op.drop_table("contact_list_rows")
    op.drop_table("contact_lists")
    op.drop_column("org_numbers", "warmup_started_at")
