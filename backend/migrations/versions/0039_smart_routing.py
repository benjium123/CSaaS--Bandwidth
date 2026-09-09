"""P21 smart routing: route explainability + the one customer switch.

- messages.route_reason / calls.route_reason  (nullable; one plain sentence per send/call)
- routing_policies.smart_routing              (bool, default true; "Prefer a provider" = false)

Additive. Numbered 0039 by the roadmap (docs/ROADMAP.md) even though it lands before
0025-0038: Alembic orders by the revision chain, not the filename, and this revision
chains directly after 0024. Later phases MUST set their down_revision to the true head
at the time they are written.

Revision ID: 0039_smart_routing
Revises: 0024_contact_ownership_roles
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_smart_routing"
down_revision = "0024_contact_ownership_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("route_reason", sa.String(255), nullable=True))
    op.add_column("calls", sa.Column("route_reason", sa.String(255), nullable=True))
    op.add_column(
        "routing_policies",
        sa.Column("smart_routing", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("routing_policies", "smart_routing")
    op.drop_column("calls", "route_reason")
    op.drop_column("messages", "route_reason")
