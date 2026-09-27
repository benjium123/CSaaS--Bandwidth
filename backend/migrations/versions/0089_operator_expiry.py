"""H4 break-glass: platform_operators.expires_at (temporary access ends by itself)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089_operator_expiry"
down_revision = "0088_operator_view_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "platform_operators",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("platform_operators", "expires_at")
