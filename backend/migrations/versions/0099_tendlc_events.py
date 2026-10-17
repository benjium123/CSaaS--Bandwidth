"""10DLC phase 1: carrier event timeline + the saved decline reason. Additive."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0099_tendlc_events"
down_revision = "0098_calendar_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tendlc_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "registration_id",
            GUID(),
            sa.ForeignKey("tendlc_registrations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("telnyx_id", sa.String(64), nullable=False),
        sa.Column("telnyx_event_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_type", sa.String(48), nullable=True),
        sa.Column("status", sa.String(48), nullable=True),
        sa.Column("reasons", PortableJSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw", PortableJSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telnyx_event_id", name="uq_tendlc_events_telnyx_event_id"),
    )
    op.create_index("ix_tendlc_events_org_id", "tendlc_events", ["org_id"])
    op.create_index(
        "ix_tendlc_events_registration", "tendlc_events", ["registration_id", "received_at"]
    )
    op.add_column("tendlc_registrations", sa.Column("rejection", PortableJSON(), nullable=True))


def downgrade():
    op.drop_column("tendlc_registrations", "rejection")
    op.drop_index("ix_tendlc_events_registration", table_name="tendlc_events")
    op.drop_index("ix_tendlc_events_org_id", table_name="tendlc_events")
    op.drop_table("tendlc_events")
