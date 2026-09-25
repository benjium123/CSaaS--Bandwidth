"""P44e: SignalWire's id for an emergency address (numbers on SignalWire register there).
Additive only."""

import sqlalchemy as sa
from alembic import op

revision = "0070_e911_signalwire"
down_revision = "0069_porting"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "emergency_addresses", sa.Column("signalwire_address_id", sa.String(64), nullable=True)
    )


def downgrade():
    op.drop_column("emergency_addresses", "signalwire_address_id")
