"""Recurring number checkout records."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0062_number_purchases"
down_revision = "0061_email_verification"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "orgs",
        sa.Column(
            "number_subscription_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_table(
        "number_purchases",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("numbers", PortableJSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("checkout_id", sa.String(255), unique=True),
        sa.Column("checkout_url", sa.Text()),
        sa.Column("subscription_id", sa.String(255), unique=True),
        sa.Column("subscription_status", sa.String(32)),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("number_purchases")
    op.drop_column("orgs", "number_subscription_required")
