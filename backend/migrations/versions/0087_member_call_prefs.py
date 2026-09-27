"""Per-member calling preferences: Do Not Disturb and where a DND member's rings go."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import PortableJSON

revision = "0087_member_call_prefs"
down_revision = "0089_operator_expiry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("org_memberships", sa.Column("call_prefs", PortableJSON(), nullable=True))


def downgrade():
    op.drop_column("org_memberships", "call_prefs")
