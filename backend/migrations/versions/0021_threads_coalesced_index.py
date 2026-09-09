"""Bugfix 5.10 follow-up: functional index for the coalesced thread ordering.

The inbox thread list now orders and paginates on
``coalesce(last_message_at, created_at)`` (so call-only threads with a NULL
last_message_at stay reachable). A plain column index cannot serve that
expression, so without this index Postgres falls back to a full sort of the
org's threads on the hottest query in the product. Additive; Postgres-only
expression index (tests run on SQLite via ``Base.metadata.create_all``).

Revision ID: 0021_threads_coalesced_index
Revises: 0020_provider_spend
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op

revision = "0021_threads_coalesced_index"
down_revision = "0020_provider_spend"
branch_labels = None
depends_on = None

INDEX = "ix_threads_org_status_coalesced"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} ON message_threads "
        "(org_id, status, coalesce(last_message_at, created_at) DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
