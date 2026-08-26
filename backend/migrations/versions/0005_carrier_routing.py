"""carrier routing: per-org routing policy

Purely additive. No existing row changes meaning: an org with no policy row routes exactly
as it did before this migration, because the router creates the row lazily with defaults
that reproduce the old single-carrier behaviour.

Revision ID: 0005_carrier_routing
Revises: 0004_compliance_media
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0005_carrier_routing"
down_revision = "0004_compliance_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routing_policies",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("preference", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column(
            "allow_intra_carrier_failover",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        # Off by default and deliberately so: switching carrier switches the number the
        # recipient sees. That must be somebody's decision, never a default.
        sa.Column(
            "allow_cross_carrier_failover",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("pinned_carrier", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_routing_policy_org"),
    )


def downgrade() -> None:
    op.drop_table("routing_policies")
