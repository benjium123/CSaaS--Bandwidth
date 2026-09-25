"""Yearly workspace plans: the subscription and the checkout cart remember month or year.

Additive only; every existing row is monthly.
"""

import sqlalchemy as sa
from alembic import op

revision = "0074_yearly_billing"
down_revision = "0073_site_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "subscriptions",
        sa.Column("billing_interval", sa.String(8), nullable=False, server_default="month"),
    )
    op.add_column(
        "number_purchases",
        sa.Column("billing_interval", sa.String(8), nullable=False, server_default="month"),
    )


def downgrade():
    op.drop_column("number_purchases", "billing_interval")
    op.drop_column("subscriptions", "billing_interval")
