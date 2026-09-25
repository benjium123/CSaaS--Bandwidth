"""E911: registered emergency addresses, and the address each number purchase registers."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0068_e911"
down_revision = "0067_fax"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "emergency_addresses",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("telnyx_address_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("street_address", sa.String(255), nullable=False),
        sa.Column("extended_address", sa.String(255)),
        sa.Column("locality", sa.String(127), nullable=False),
        sa.Column("administrative_area", sa.String(32), nullable=False),
        sa.Column("postal_code", sa.String(16), nullable=False),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "number_purchases",
        sa.Column(
            "emergency_address_id",
            GUID(),
            sa.ForeignKey("emergency_addresses.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "number_purchases",
        sa.Column("e911_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("number_purchases", "e911_acknowledged_at")
    op.drop_column("number_purchases", "emergency_address_id")
    op.drop_table("emergency_addresses")
