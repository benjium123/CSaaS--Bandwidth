"""Telnyx cost reconciliation: Telnyx's own billed cost per org per day."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0066_telnyx_cost_daily"
down_revision = "0065_prepaid_hard_stop"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telnyx_cost_daily",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL")),
        sa.Column("period_date", sa.Date(), nullable=False),
        sa.Column("record_type", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default=""),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_telnyx_cost_daily_date", "telnyx_cost_daily", ["period_date"])
    op.create_index(
        "ix_telnyx_cost_daily_org_date", "telnyx_cost_daily", ["org_id", "period_date"]
    )


def downgrade():
    op.drop_index("ix_telnyx_cost_daily_org_date", table_name="telnyx_cost_daily")
    op.drop_index("ix_telnyx_cost_daily_date", table_name="telnyx_cost_daily")
    op.drop_table("telnyx_cost_daily")
