"""Prepaid telephony hard gate: per-org switch, unbilled-call marker, number rental period.

- orgs.telephony_prepaid          per-org hard gate. When true, outbound SMS/MMS, outbound
                                  calls and number orders draw from the P24 prepaid credit
                                  balance and are refused when it cannot cover them; inbound
                                  traffic and number rental are charged. Default false, so
                                  deploying this changes nothing until platform ops turns it
                                  on for an org.
- orgs.telephony_prepaid_since    when the gate was last switched on; calls that started
                                  before it are never billed retroactively.
- calls.billed_at                 set once a finished call's minutes have been charged; the
                                  sweeper bills rows with ended_at set and billed_at NULL.
- org_numbers.rental_paid_through the date through which a number's monthly rental is paid.

Additive. Chains off 0040_org_calling_settings (the true head at time of writing).

Revision ID: 0041_prepaid_telephony
Revises: 0040_org_calling_settings
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_prepaid_telephony"
down_revision = "0040_org_calling_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("telephony_prepaid", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # When the gate was last switched on. Only calls that start after it are billed, so
    # turning prepaid on never retro-charges an org's past traffic.
    op.add_column(
        "orgs", sa.Column("telephony_prepaid_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("calls", sa.Column("billed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_calls_billing", "calls", ["billed_at", "ended_at"])
    op.add_column("org_numbers", sa.Column("rental_paid_through", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("org_numbers", "rental_paid_through")
    op.drop_index("ix_calls_billing", table_name="calls")
    op.drop_column("calls", "billed_at")
    op.drop_column("orgs", "telephony_prepaid_since")
    op.drop_column("orgs", "telephony_prepaid")
