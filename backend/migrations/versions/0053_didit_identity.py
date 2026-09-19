"""Didit as a second identity-verification provider.

- kyc_persons.identity_provider    which provider ran the check ("stripe" | "didit")
- kyc_persons.provider_session_id  that provider's own session id
- identity_webhook_events          processed webhook event ids, for replay protection

Why a provider-neutral pair instead of reusing stripe_verification_session_id:
that column is Stripe-shaped and already populated for existing rows, so it stays
exactly as it is. New rows record the provider and its session id in the new
columns, and the old column is left alone so nothing has to be backfilled.

Why a separate ledger table: Stripe has its own event ledger, but Didit (and any
provider after it) needs durable idempotency keyed on the provider's event id, or
a retried webhook would be applied twice. The table is platform-wide rather than
tenant-scoped because a webhook arrives before we know which org it belongs to.

Both new columns are nullable and no existing row is touched.

Revision ID: 0053_didit_identity
Revises: 0052_agent_calls_place
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_didit_identity"
down_revision = "0052_agent_calls_place"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kyc_persons", sa.Column("identity_provider", sa.String(16), nullable=True))
    op.add_column("kyc_persons", sa.Column("provider_session_id", sa.String(128), nullable=True))
    # SQLite cannot add a UNIQUE constraint with ALTER TABLE, so uniqueness is expressed
    # as a unique index instead. The model declares the same rule as `unique=True` on the
    # column, which create_all renders as an inline UNIQUE - the same guarantee under a
    # different name, so an autogenerate run may report the naming as drift.
    op.create_index(
        "uq_kyc_persons_provider_session_id",
        "kyc_persons",
        ["provider_session_id"],
        unique=True,
    )

    op.create_table(
        "identity_webhook_events",
        sa.Column("id", sa.String(320), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("identity_webhook_events")
    op.drop_index("uq_kyc_persons_provider_session_id", table_name="kyc_persons")
    op.drop_column("kyc_persons", "provider_session_id")
    op.drop_column("kyc_persons", "identity_provider")
