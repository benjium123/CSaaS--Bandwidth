"""Fixed monthly platform costs for the ops P&L (the VPS first).

Additive: a new platform-wide table, seeded with the VPS at $18/month from the day the
first workspace was created (editable in Ops -> Console).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0076_fixed_costs"
down_revision = "0075_email_2fa_default"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fixed_costs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("monthly_micros", sa.BigInteger(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("updated_by", GUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    conn = op.get_bind()
    first = conn.execute(sa.text("SELECT MIN(created_at) FROM orgs")).scalar()
    if isinstance(first, str):  # SQLite returns text
        first = datetime.fromisoformat(first)
    starts = first.date() if isinstance(first, datetime) else date.today()
    costs = sa.table(
        "fixed_costs",
        sa.column("id", GUID()),
        sa.column("name", sa.String()),
        sa.column("monthly_micros", sa.BigInteger()),
        sa.column("starts_on", sa.Date()),
        sa.column("note", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    conn.execute(
        sa.insert(costs).values(
            id=uuid.uuid4(),
            name="VPS",
            monthly_micros=18_000_000,
            starts_on=starts,
            note="Server hosting (seeded by migration 0076)",
            updated_at=datetime.now(timezone.utc),
        )
    )


def downgrade():
    op.drop_table("fixed_costs")
