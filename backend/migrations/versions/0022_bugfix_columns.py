"""Bugfix round 2026-09: additive nullable/defaulted columns.

- queue_entries.dial_now_claimed_at   (3.13)  one-shot claim for /dial-now, double-dial guard
- org_numbers.order_poll_attempts     (4.14)  sweeper poll ceiling for stuck carrier orders
- contact_lists.import_started_at     (6.8)   atomic claim for /commit's background import
- call_scores.tokens_in / tokens_out  (6.14)  scoring LLM usage, previously unmetered

All additive; no data rewrite. Server defaults keep existing rows valid.

Revision ID: 0022_bugfix_columns
Revises: 0021_threads_coalesced_index
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_bugfix_columns"
down_revision = "0021_threads_coalesced_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "queue_entries",
        sa.Column("dial_now_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "org_numbers",
        sa.Column(
            "order_poll_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "contact_lists",
        sa.Column("import_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("call_scores", sa.Column("tokens_in", sa.Integer(), nullable=True))
    op.add_column("call_scores", sa.Column("tokens_out", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("call_scores", "tokens_out")
    op.drop_column("call_scores", "tokens_in")
    op.drop_column("contact_lists", "import_started_at")
    op.drop_column("org_numbers", "order_poll_attempts")
    op.drop_column("queue_entries", "dial_now_claimed_at")
