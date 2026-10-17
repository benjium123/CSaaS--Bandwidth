"""10DLC phase 5: the campaign fields carrier vetters ask for. Additive, all nullable."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100_campaign_filing_fields"
down_revision = "0099_tendlc_events"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("privacy_policy_url", sa.String(500)),
    ("terms_url", sa.String(500)),
    ("opt_in_keywords", sa.String(255)),
    ("opt_out_keywords", sa.String(255)),
    ("help_keywords", sa.String(255)),
    ("opt_in_message", sa.String(500)),
    ("opt_in_proof_url", sa.String(500)),
    ("expected_monthly_messages", sa.Integer()),
)


def upgrade():
    for name, type_ in _COLUMNS:
        op.add_column("campaigns", sa.Column(name, type_, nullable=True))


def downgrade():
    for name, _ in reversed(_COLUMNS):
        op.drop_column("campaigns", name)
