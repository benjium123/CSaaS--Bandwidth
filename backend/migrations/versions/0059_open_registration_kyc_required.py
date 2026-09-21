"""Add orgs.kyc_required for open-registration identity checks.

Revision ID: 0059_open_registration_kyc_required
Revises: 0058_merge_0057_heads
Create Date: 2026-09-20

Open registration lets anyone create an individual workspace without an operator in the
loop, so new orgs start with kyc_required = true and must clear identity verification
before they transact. Every org that already exists predates that policy, so it is
grandfathered: the column is added with a server default of false (which also backfills
the existing rows on PostgreSQL, where a NOT NULL column needs a default anyway), and
only afterwards is the server default switched to true, so future rows are gated.

SQLite has no ALTER for this, so both directions go through batch_alter_table, which
recreates the table on SQLite and emits plain ALTERs on PostgreSQL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_open_registration_kyc_required"
down_revision = "0058_merge_0057_heads"
branch_labels = None
depends_on = None

_TABLE = "orgs"
_COLUMN = "kyc_required"


def upgrade() -> None:
    # Step 1: add the column with server_default false. PostgreSQL backfills existing rows
    # from it and SQLite stores it inline, so every org that already exists is
    # grandfathered in as "not required".
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                _COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # Step 2: future rows created by register() are gated. Changing the server default does
    # not rewrite the grandfathered rows - they keep their backfilled false.
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.true(),
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_column(_COLUMN)
