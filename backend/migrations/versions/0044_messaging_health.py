"""P41 messaging health: failure classes on messages + per-workspace daily rollup.

- messages.failure_class      one of spam_blocked | carrier_rejected | invalid_destination |
                              opted_out | unknown, set when a delivery receipt (or an
                              immediate rejection) reports a failure. Derived from the
                              carrier's raw error_code by app/providers/failure_classes.py,
                              stored so rollups never re-parse carrier codes and a later
                              classification change never rewrites history.
- org_messaging_daily         one row per (org, day, carrier): sent / delivered / failed by
                              class / inbound / opt-outs / help / opt-ins. Filled by the
                              sweeper for today AND yesterday on every run (receipts arrive
                              late); the unique key makes the upsert idempotent. Rates are
                              derived at read time and never stored.

Additive and inert: no rows are created here, the card and the ops table render "no data"
until the first rollup runs. No money, no ledger.

Revision ID: 0044_messaging_health
Revises: 0043_plan_allowances
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0044_messaging_health"
down_revision = "0043_plan_allowances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("failure_class", sa.String(24), nullable=True))

    op.create_table(
        "org_messaging_daily",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("period_date", sa.Date(), nullable=False),
        #: bandwidth | telnyx | twilio | plivo | signalwire - one row per carrier so a
        #: problem on one carrier is visible as that carrier's, not averaged away.
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("sent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("delivered", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed_spam_blocked", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "failed_carrier_rejected", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "failed_invalid_destination", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("failed_opted_out", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("inbound", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("opt_outs", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("help_requests", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("opt_ins", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "period_date", "carrier", name="uq_org_messaging_daily_key"
        ),
    )
    op.create_index(
        "ix_org_messaging_daily_org_date", "org_messaging_daily", ["org_id", "period_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_org_messaging_daily_org_date", table_name="org_messaging_daily")
    op.drop_table("org_messaging_daily")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("failure_class")
