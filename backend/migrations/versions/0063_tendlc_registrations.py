"""Self-serve 10DLC registrations: paid checkout through to an active campaign."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0063_tendlc_registrations"
down_revision = "0062_number_purchases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tendlc_registrations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "brand_id",
            GUID(),
            sa.ForeignKey("brands.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "campaign_id",
            GUID(),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fee_tier", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("filing", PortableJSON(), nullable=False),
        sa.Column("checkout_id", sa.String(255), unique=True),
        sa.Column("checkout_url", sa.Text()),
        sa.Column("subscription_id", sa.String(255), unique=True),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("otp_sent_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_checked_at", sa.DateTime(timezone=True)),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("tendlc_registrations")
