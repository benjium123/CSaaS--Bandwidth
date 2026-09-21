"""Per-user custom ordering of the Lines rail.

org_memberships.inbox_order: a nullable JSON array of inbox id strings, the order this
member last dragged their number list into. NULL means "no custom order yet" - the app
falls back to a computed default (own numbers, then department numbers, then unassigned)
rather than storing that default here, so the default logic can change without a backfill.

Revision ID: 0057_inbox_order
Revises: 0056_merge_heads
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import PortableJSON

revision = "0057_inbox_order"
down_revision = "0056_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "org_memberships", sa.Column("inbox_order", PortableJSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("org_memberships", "inbox_order")
