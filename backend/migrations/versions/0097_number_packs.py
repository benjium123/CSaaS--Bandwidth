"""Number packs: which packs a workspace's plan subscription carries.

``subscriptions.number_packs``: NULL = no packs (every subscription before this migration);
otherwise ``{"25": 1, "100": 2}`` = pack code to quantity, mirrored from the Stripe
subscription items by ``plan_billing.upsert_from_stripe``. Additive and nullable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import PortableJSON

revision = "0097_number_packs"
down_revision = "0096_support_chat_ai"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subscriptions", sa.Column("number_packs", PortableJSON(), nullable=True))


def downgrade():
    op.drop_column("subscriptions", "number_packs")
