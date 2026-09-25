"""Billing v2 hard stop: switch every org's prepaid gate ON, billed from now.

Live orgs were created while TELEPHONY_PREPAID_DEFAULT was false, so none was billed. This
turns the gate on for them and RE-STAMPS telephony_prepaid_since to now (an older stamp
would let the next sweeper pass bill a week of past calls), and moves every active,
non-Stripe number's rental clock to next month so the switch itself charges nothing.

One-way data step (same reasoning as 0055); downgrade is a no-op.
"""

from __future__ import annotations

import calendar
from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "0065_prepaid_hard_stop"
down_revision = "0064_billing_v2"
branch_labels = None
depends_on = None


def _next_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def upgrade() -> None:
    today = date.today()
    op.execute(
        sa.text(
            "UPDATE org_numbers SET rental_paid_through = :through "
            "WHERE released_at IS NULL AND status = 'active' "
            "AND (rental_paid_through IS NULL OR rental_paid_through <= :today) "
            "AND org_id IN (SELECT id FROM orgs WHERE telephony_prepaid = false)"
        ).bindparams(through=_next_month(today), today=today)
    )
    op.execute(
        sa.text(
            "UPDATE orgs SET telephony_prepaid = true, "
            "telephony_prepaid_since = CURRENT_TIMESTAMP WHERE telephony_prepaid = false"
        )
    )


def downgrade() -> None:
    pass
