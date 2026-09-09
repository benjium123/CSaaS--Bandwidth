"""Inbox "important" star on conversations.

message_threads.is_important (bool, NOT NULL, default false). Drives the Important
filter and the star toggle in the inbox. Additive.

Revision ID: 0023_thread_is_important
Revises: 0022_bugfix_columns
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_thread_is_important"
down_revision = "0022_bugfix_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_threads",
        sa.Column(
            "is_important",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_threads_org_important",
        "message_threads",
        ["org_id", "is_important"],
    )


def downgrade() -> None:
    op.drop_index("ix_threads_org_important", table_name="message_threads")
    op.drop_column("message_threads", "is_important")
