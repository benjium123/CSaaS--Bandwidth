"""P30 reports: scheduled report emails and org email settings.

- report_schedules      which report, params, cadence, recipients, last_sent_at
- org_email_settings    one row per org: provider (smtp | postmark), encrypted credentials,
                        from address, status (probe result)

Additive. Revision chains after 0031_voice_completeness.

Revision ID: 0032_reports
Revises: 0031_voice_completeness
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0032_reports"
down_revision = "0031_voice_completeness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        # team | inbox_sla | campaigns | assistant
        sa.Column("report", sa.String(16), nullable=False),
        sa.Column("params", PortableJSON(), nullable=False, server_default="{}"),
        # daily | weekly | monthly
        sa.Column("cadence", sa.String(8), nullable=False),
        sa.Column("recipients", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_report_schedules_org", "report_schedules", ["org_id"])

    op.create_table(
        "org_email_settings",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_detail", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_org_email_settings_org"),
    )


def downgrade() -> None:
    op.drop_table("org_email_settings")
    op.drop_index("ix_report_schedules_org", table_name="report_schedules")
    op.drop_table("report_schedules")
