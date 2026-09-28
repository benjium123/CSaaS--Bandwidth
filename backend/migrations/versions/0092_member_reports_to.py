"""Org hierarchy: each member may report to one manager in the same workspace.

A manager gets every line granted directly to anyone below them (services/org_tree.py,
services/inbox_access.py). Additive and nullable: NULL = reports to nobody.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0092_member_reports_to"
down_revision = "0091_profile_support"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "org_memberships",
        sa.Column("reports_to_user_id", GUID(), nullable=True),
    )
    op.create_index(
        "ix_org_memberships_reports_to_user_id", "org_memberships", ["reports_to_user_id"]
    )


def downgrade():
    op.drop_index("ix_org_memberships_reports_to_user_id", table_name="org_memberships")
    op.drop_column("org_memberships", "reports_to_user_id")
