"""users.recovery_codes_acknowledged_at: the owner confirmed they saved their codes.

Codes are generated when the recovery-codes screen opens, so closing the tab before saving
them used to lift the wall anyway (codes remaining > 0). The wall now also stays until this
is set. Backfill: every account that already holds unused codes counts as acknowledged, so
nobody who finished setup is walled again.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0080_recovery_codes_ack"
down_revision = "0079_contact_soft_delete"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("recovery_codes_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    users = sa.table(
        "users",
        sa.column("id"),
        sa.column("recovery_codes_acknowledged_at", sa.DateTime(timezone=True)),
    )
    codes = sa.table("recovery_codes", sa.column("user_id"), sa.column("used_at"))
    op.get_bind().execute(
        sa.update(users)
        .where(users.c.id.in_(sa.select(codes.c.user_id).where(codes.c.used_at.is_(None))))
        .values(recovery_codes_acknowledged_at=datetime.now(timezone.utc))
    )


def downgrade():
    op.drop_column("users", "recovery_codes_acknowledged_at")
