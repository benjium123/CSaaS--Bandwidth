"""P33 agencies: sub-accounts and white-label branding.

- orgs.parent_org_id      a client workspace under an agency (NULL = top-level)
- org_branding            logo, colour, app name, support email, custom domain + status

Additive. Revision chains after 0034_plans_invoices.

Revision ID: 0035_agencies
Revises: 0034_plans_invoices
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0035_agencies"
down_revision = "0034_plans_invoices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column(
            "parent_org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_orgs_parent", "orgs", ["parent_org_id"])

    op.create_table(
        "org_branding",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logo_key", sa.String(255), nullable=True),
        sa.Column("primary_color", sa.String(7), nullable=True),
        sa.Column("app_name", sa.String(63), nullable=True),
        sa.Column("support_email", sa.String(320), nullable=True),
        sa.Column("custom_domain", sa.String(253), nullable=True),
        # pending | active | failed
        sa.Column("domain_status", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("domain_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_org_branding_org"),
        sa.UniqueConstraint("custom_domain", name="uq_org_branding_domain"),
    )


def downgrade() -> None:
    op.drop_table("org_branding")
    op.drop_index("ix_orgs_parent", table_name="orgs")
    op.drop_column("orgs", "parent_org_id")
