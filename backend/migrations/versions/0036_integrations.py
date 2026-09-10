"""P34 developers and integrations: connected integrations and their sync log.

- integrations          hubspot | salesforce | zapier | generic_webhook per org, encrypted creds
- integration_sync_log  one row per sync run (direction, counts, error)

Additive. Revision chains after 0035_agencies.

Revision ID: 0036_integrations
Revises: 0035_agencies
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0036_integrations"
down_revision = "0035_agencies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(127), nullable=False, server_default=""),
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        sa.Column("config", PortableJSON(), nullable=False, server_default="{}"),
        # unverified | active | failed | disabled
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "kind", "label", name="uq_integrations_org_kind_label"),
    )
    op.create_index("ix_integrations_org_kind", "integrations", ["org_id", "kind"])

    op.create_table(
        "integration_sync_log",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "integration_id",
            GUID(),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # pull | push | both
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("counts", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_integration_sync_log_integration",
        "integration_sync_log",
        ["integration_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_sync_log_integration", table_name="integration_sync_log")
    op.drop_table("integration_sync_log")
    op.drop_index("ix_integrations_org_kind", table_name="integrations")
    op.drop_table("integrations")
