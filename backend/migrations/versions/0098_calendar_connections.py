"""Calendar connections: external calendars (Google first) the AI books around and into.

Additive. ``calendar_connections`` is new; ``appointments`` gains two nullable columns
recording where a copy of a booking was written. ``downgrade`` drops both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0098_calendar_connections"
down_revision = "0097_number_packs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_connections",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account_email", sa.String(320), nullable=False),
        sa.Column("calendar_id", sa.String(255), nullable=False, server_default="primary"),
        sa.Column("credentials", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "provider", "account_email", name="uq_calendar_connections_account"
        ),
    )
    op.create_index("ix_calendar_connections_org_id", "calendar_connections", ["org_id"])
    op.create_index("ix_calendar_connections_user_id", "calendar_connections", ["user_id"])

    op.add_column(
        "appointments",
        sa.Column(
            "calendar_connection_id",
            GUID(),
            sa.ForeignKey("calendar_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("appointments", sa.Column("external_event_id", sa.String(255), nullable=True))


def downgrade():
    op.drop_column("appointments", "external_event_id")
    op.drop_column("appointments", "calendar_connection_id")
    op.drop_index("ix_calendar_connections_user_id", table_name="calendar_connections")
    op.drop_index("ix_calendar_connections_org_id", table_name="calendar_connections")
    op.drop_table("calendar_connections")
