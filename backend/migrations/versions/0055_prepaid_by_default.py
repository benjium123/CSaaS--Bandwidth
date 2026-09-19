"""Prepaid telephony on by default: the pay-as-you-go credit gate is now the money gate.

New orgs are created with ``orgs.telephony_prepaid`` true (telephony_billing refuses
outbound SMS/MMS, dials and number orders the balance cannot cover) and with
``orgs.telephony_prepaid_since`` stamped, so an org switched on is billed from that instant
onward and never retroactively.

Three changes:

1. Backfill: every existing org with the gate off is switched ON, and any org whose
   ``telephony_prepaid_since`` is NULL is stamped with the current time, so no past call is
   retro-charged - only traffic after that instant is billed.
2. Backfill: every existing active, unreleased number whose ``rental_paid_through`` is NULL
   is stamped to one month from the migration date. ``renew_number_rentals`` treats NULL as
   "never paid - charge it now", so without this stamp flipping the gate on would bill every
   org a full month for every number it already holds the instant the next sweeper tick
   runs. Billing starts at the NEXT natural cycle, not on migration day - the number-side
   counterpart of the ``telephony_prepaid_since`` stamp that protects calls.
3. The server default of ``orgs.telephony_prepaid`` is changed from false to true, so rows
   inserted before the application default lands are still gated.

The data backfills are ONE-WAY. ``downgrade`` restores only the server default; it does not
flip any org back off or un-stamp any number, because we cannot tell which orgs platform ops
had deliberately switched on before this migration ran. Platform ops can still switch a
specific org off via the platform endpoint - that escape hatch is untouched.

SQLite-safe: CURRENT_TIMESTAMP is used (not now()) for the org stamp, the number stamp's "one
month from today" is computed in Python and bound as a parameter (SQLite and Postgres spell
date arithmetic differently), and the column change goes through batch_alter_table.

Revision ID: 0055_prepaid_by_default
Revises: 0054_subscriptions
Create Date: 2026-09-20
"""

from __future__ import annotations

import calendar
from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "0055_prepaid_by_default"
down_revision = "0054_subscriptions"
branch_labels = None
depends_on = None


def _next_month(day: date) -> date:
    """Same day next month, clamped to the month's length (Jan 31 -> Feb 28/29).

    Deliberately a local copy of telephony_billing._next_month: a migration is a historical
    record and must not change meaning when the application's helper does.
    """
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def upgrade() -> None:
    # Backfill the gate on for every org that had it off. CURRENT_TIMESTAMP is portable
    # across SQLite and Postgres; now() is not.
    op.execute(sa.text("UPDATE orgs SET telephony_prepaid = true WHERE telephony_prepaid = false"))
    # Stamp the "billed from" instant for any org with no gate time yet, so an org switched
    # on here is billed going forward and never for its past traffic.
    op.execute(
        sa.text(
            "UPDATE orgs SET telephony_prepaid_since = CURRENT_TIMESTAMP "
            "WHERE telephony_prepaid = true AND telephony_prepaid_since IS NULL"
        )
    )
    # Existing numbers have rental_paid_through NULL, and renew_number_rentals treats NULL
    # as "never paid - charge it now". Without this stamp, flipping the gate on above would
    # bill every org a full month for every number it already holds, the instant the next
    # sweeper tick runs (an org with 20 numbers would be -$200 on migration day, with no
    # warning and no way to send until they paid off a debt they never agreed to).
    #
    # So: bill from the NEXT cycle, not from today. These customers held these numbers
    # through a period we were not billing for; we do not charge them for it retroactively.
    # This is the number-side counterpart of the telephony_prepaid_since stamp above, which
    # already stops calls being retro-charged.
    #
    # Scoped to exactly what renew_number_rentals would pick up (active, not released), so
    # rows it would never charge are left NULL and untouched.
    op.execute(
        sa.text(
            "UPDATE org_numbers SET rental_paid_through = :through "
            "WHERE rental_paid_through IS NULL "
            "AND released_at IS NULL "
            "AND status = 'active'"
        ).bindparams(through=_next_month(date.today()))
    )
    # SQLite cannot ALTER a column's server default in place, so go through batch mode.
    with op.batch_alter_table("orgs") as batch:
        batch.alter_column(
            "telephony_prepaid",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.true(),
        )


def downgrade() -> None:
    # Only the server default is reversed. The data backfills above are intentionally NOT
    # undone: we cannot tell which orgs were deliberately on before this migration ran, so
    # flipping them all off could silently disable billing for orgs ops meant to gate. The
    # number stamp is likewise left in place - clearing rental_paid_through back to NULL
    # would re-arm the exact mass-charge on every existing number that this migration exists
    # to prevent.
    with op.batch_alter_table("orgs") as batch:
        batch.alter_column(
            "telephony_prepaid",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )
