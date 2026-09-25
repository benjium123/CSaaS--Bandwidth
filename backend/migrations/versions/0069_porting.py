"""P44f: number porting (port-in requests, port-out tracking) and the per-number port lock."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0069_porting"
down_revision = "0068_e911"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "port_requests",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False,
            index=True,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("numbers", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(24), nullable=False, index=True),
        sa.Column("carrier_ref", sa.String(64), index=True),
        sa.Column("foc_date", sa.String(32)),
        sa.Column("details", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("secret_enc", sa.Text()),
        sa.Column("loa_media_key", sa.String(255)),
        sa.Column("invoice_media_key", sa.String(255)),
        sa.Column("submitted_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(255)),
        sa.Column("events", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "org_numbers",
        sa.Column("port_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("org_numbers", "port_locked")
    op.drop_table("port_requests")
