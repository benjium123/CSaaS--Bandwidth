"""Workspace plans: paid add-on users and numbers on a plan subscription. Additive only.

The Solo / Team / Business catalogue rows are written by services/plan_billing.ensure_catalog,
so code and database cannot disagree about what a plan includes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072_workspace_plans"
down_revision = "0071_email_second_factor"
branch_labels = None
depends_on = None


def upgrade():
    # Every number purchase on a workspace plan shares the plan's one subscription.
    with op.batch_alter_table("number_purchases") as batch:
        batch.drop_constraint("number_purchases_subscription_id_key", type_="unique")
        batch.create_index("ix_number_purchases_subscription_id", ["subscription_id"])
        batch.add_column(sa.Column("plan_code", sa.String(32), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("extra_users", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscriptions",
        sa.Column("extra_numbers", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    with op.batch_alter_table("number_purchases") as batch:
        batch.drop_column("plan_code")
        batch.drop_index("ix_number_purchases_subscription_id")
        batch.create_unique_constraint("number_purchases_subscription_id_key", ["subscription_id"])
    op.drop_column("subscriptions", "extra_numbers")
    op.drop_column("subscriptions", "extra_users")
