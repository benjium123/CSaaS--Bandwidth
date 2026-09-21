"""Add orgs.account_type: 'business' (default) | 'individual'.

Revision ID: 0057_individual_accounts
Revises: 0056_merge_heads
Create Date: 2026-09-20

The column is non-nullable with a server default of 'business', which backfills every
org that already exists (all of them are company workspaces). A named check constraint,
ck_orgs_account_type, keeps the value domain closed. Classification is immutable at the
API level: nothing here or in the model reclassifies an existing org, and register()
stamps 'individual' on the row it just created.

SQLite has no ALTER for this, so both directions go through batch_alter_table, which
recreates the table on SQLite and emits plain ALTERs on PostgreSQL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_individual_accounts"
down_revision = "0056_merge_heads"
branch_labels = None
depends_on = None

_COLUMN = "account_type"
_CHECK_NAME = "ck_orgs_account_type"
_CHECK_SQL = "account_type IN ('business', 'individual')"


def upgrade() -> None:
    with op.batch_alter_table("orgs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                _COLUMN,
                sa.String(length=16),
                nullable=False,
                server_default="business",
            )
        )
        batch_op.create_check_constraint(_CHECK_NAME, _CHECK_SQL)


def downgrade() -> None:
    with op.batch_alter_table("orgs", schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.drop_column(_COLUMN)
