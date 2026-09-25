"""Fax: faxes sent/received and their carrier events."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0067_fax"
down_revision = "0066_telnyx_cost_daily"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "faxes",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False,
            index=True,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("from_e164", sa.String(20), nullable=False),
        sa.Column("to_e164", sa.String(20), nullable=False),
        sa.Column("provider_fax_id", sa.String(64), unique=True),
        sa.Column("page_count", sa.Integer()),
        sa.Column("media_key", sa.String(255)),
        sa.Column("media_name", sa.String(255)),
        sa.Column("failure_reason", sa.String(255)),
        sa.Column("charged_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_faxes_org_created", "faxes", ["org_id", "created_at"])
    op.create_table(
        "fax_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("provider_fax_id", sa.String(64), index=True),
        sa.Column("payload", PortableJSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("fax_events")
    op.drop_index("ix_faxes_org_created", table_name="faxes")
    op.drop_table("faxes")
