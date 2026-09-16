"""P42 API key hardening: per-key network allowlist and last-used IP.

Existing keys keep working unchanged (NULL = no per-key restriction). The default maximum
lifetime (API_KEY_MAX_DAYS) applies only to keys created from now on.

Revision ID: 0048_api_key_networks
Revises: 0047_sessions
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import PortableJSON

revision = "0048_api_key_networks"
down_revision = "0047_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("allowed_cidrs", PortableJSON(), nullable=True))
    op.add_column("api_keys", sa.Column("last_used_ip", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "last_used_ip")
    op.drop_column("api_keys", "allowed_cidrs")
