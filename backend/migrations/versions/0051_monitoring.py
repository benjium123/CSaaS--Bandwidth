"""P43 AI traffic monitoring.

- messages.moderation_state / moderation_reason   the AI text guard's decision per text
- text_verdicts        cached verdict per normalised body (one AI call per campaign text)
- monitor_signals      risk signals per workspace
- org_monitoring       score, level (normal/watch/restricted/paused), case file, appeal
- call_reviews         AI review of call transcripts
- number_reports       public "report a number" submissions
- monitor_labels       operator decisions feeding the AI exam library
- monitor_health       canary and exam results

Existing messages keep moderation_state NULL (not screened). Nothing is paused by this
migration; MONITOR_ENFORCED controls whether the guard acts.

Revision ID: 0051_monitoring
Revises: 0050_kyc_automation
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0051_monitoring"
down_revision = "0050_kyc_automation"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _org() -> sa.Column:
    return sa.Column(
        "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.add_column("messages", sa.Column("moderation_state", sa.String(16), nullable=True))
    op.add_column("messages", sa.Column("moderation_reason", sa.String(255), nullable=True))
    op.create_index(
        "ix_messages_moderation_held",
        "messages",
        ["moderation_state"],
        postgresql_where=sa.text("moderation_state = 'held'"),
    )

    op.create_table(
        "text_verdicts",
        sa.Column("id", GUID(), primary_key=True),
        _org(),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_ts(),
        sa.UniqueConstraint("org_id", "body_hash", name="uq_text_verdicts_org_body"),
    )
    op.create_index("ix_text_verdicts_org_id", "text_verdicts", ["org_id"])

    op.create_table(
        "monitor_signals",
        sa.Column("id", GUID(), primary_key=True),
        _org(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("detail", PortableJSON(), nullable=True),
        sa.Column("message_id", GUID(), nullable=True),
        sa.Column("call_id", GUID(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_monitor_signals_org_id", "monitor_signals", ["org_id"])
    op.create_index("ix_monitor_signals_org_created", "monitor_signals", ["org_id", "created_at"])

    op.create_table(
        "org_monitoring",
        sa.Column("id", GUID(), primary_key=True),
        _org(),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("level_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("case_file", PortableJSON(), nullable=True),
        sa.Column("appeal", sa.Text(), nullable=True),
        sa.Column("appealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_before", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("org_id", name="uq_org_monitoring_org"),
    )
    op.create_index("ix_org_monitoring_org_id", "org_monitoring", ["org_id"])

    op.create_table(
        "call_reviews",
        sa.Column("id", GUID(), primary_key=True),
        _org(),
        sa.Column(
            "call_id", GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column("summary", sa.String(1000), nullable=True),
        sa.Column("evidence", PortableJSON(), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("call_id", name="uq_call_reviews_call"),
    )
    op.create_index("ix_call_reviews_org_id", "call_reviews", ["org_id"])
    op.create_index("ix_call_reviews_status", "call_reviews", ["status"])

    op.create_table(
        "number_reports",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("reported_e164", sa.String(20), nullable=False),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("reporter_contact", sa.String(255), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("assessment", PortableJSON(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_number_reports_reported_e164", "number_reports", ["reported_e164"])
    op.create_index("ix_number_reports_org_id", "number_reports", ["org_id"])

    op.create_table(
        "monitor_labels",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("context", PortableJSON(), nullable=True),
        sa.Column("org_id", GUID(), nullable=True),
        sa.Column("source_id", GUID(), nullable=True),
        sa.Column(
            "decided_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        *_ts(),
    )

    op.create_table(
        "monitor_health",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", PortableJSON(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_monitor_health_kind", "monitor_health", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_monitor_health_kind", table_name="monitor_health")
    op.drop_table("monitor_health")
    op.drop_table("monitor_labels")
    op.drop_index("ix_number_reports_org_id", table_name="number_reports")
    op.drop_index("ix_number_reports_reported_e164", table_name="number_reports")
    op.drop_table("number_reports")
    op.drop_index("ix_call_reviews_status", table_name="call_reviews")
    op.drop_index("ix_call_reviews_org_id", table_name="call_reviews")
    op.drop_table("call_reviews")
    op.drop_index("ix_org_monitoring_org_id", table_name="org_monitoring")
    op.drop_table("org_monitoring")
    op.drop_index("ix_monitor_signals_org_created", table_name="monitor_signals")
    op.drop_index("ix_monitor_signals_org_id", table_name="monitor_signals")
    op.drop_table("monitor_signals")
    op.drop_index("ix_text_verdicts_org_id", table_name="text_verdicts")
    op.drop_table("text_verdicts")
    op.drop_index("ix_messages_moderation_held", table_name="messages")
    op.drop_column("messages", "moderation_reason")
    op.drop_column("messages", "moderation_state")
