"""P29 voice completeness: org-level calling settings.

P29 needs two ORG-level calling settings that migration 0031 did not give a column:
the org's default recording channel layout ('mixed' | 'dual'), and the org's
configurable call-result (disposition) catalogue. `calls.disposition` and
`call_recordings.channel_layout` exist per row; neither is a place to keep the org's
own configuration.

Adds ONE nullable JSON blob, `orgs.calling_settings`, shaped:

    {"channel_layout": "mixed"|"dual", "dispositions": ["Interested", ...]}

NULL means "platform defaults" (see backend/app/services/calling_settings.py), so this
is additive with no backfill and no behaviour change for an org that never opens
Settings -> Calling.

Chains off 0038_trust (the true chain head at time of writing - filenames in this repo
are not chained in numeric order; see docs/ROADMAP.md).

Revision ID: 0040_org_calling_settings
Revises: 0038_trust
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import PortableJSON

revision = "0040_org_calling_settings"
down_revision = "0038_trust"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orgs", sa.Column("calling_settings", PortableJSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("orgs", "calling_settings")
