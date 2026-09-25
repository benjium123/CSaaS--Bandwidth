"""P44e: mandatory 911 (E911) address per number.

emergency_addresses: the service addresses a workspace registers (validated by the
carrier). org_numbers gains the address it is bound to and the carrier-side E911 state.
Additive only.
"""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0068_e911"
down_revision = "0067_fax"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "emergency_addresses",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False,
            index=True,
        ),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("caller_name", sa.String(128), nullable=False),
        sa.Column("line1", sa.String(128), nullable=False),
        sa.Column("line2", sa.String(128)),
        sa.Column("city", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("postal_code", sa.String(16), nullable=False),
        sa.Column("country", sa.String(2), nullable=False, server_default="US"),
        sa.Column("carrier_refs", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.String(255)),
        sa.Column("created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "org_numbers",
        sa.Column(
            "emergency_address_id",
            GUID(),
            sa.ForeignKey("emergency_addresses.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "org_numbers",
        sa.Column("e911_status", sa.String(16), nullable=False, server_default="none"),
    )
    op.add_column("org_numbers", sa.Column("e911_error", sa.String(255)))
    op.add_column("org_numbers", sa.Column("e911_updated_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("org_numbers", "e911_updated_at")
    op.drop_column("org_numbers", "e911_error")
    op.drop_column("org_numbers", "e911_status")
    op.drop_column("org_numbers", "emergency_address_id")
    op.drop_table("emergency_addresses")
