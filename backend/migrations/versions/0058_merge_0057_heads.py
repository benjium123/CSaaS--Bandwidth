"""Merge the 0057 inbox-order and individual-accounts heads.

Revision ID: 0058_merge_0057_heads
Revises: 0057_inbox_order, 0057_individual_accounts
Create Date: 2026-09-20

Both parents descend from 0056_merge_heads and touch unrelated objects
(org_memberships.inbox_order and orgs.account_type), so this revision only
rejoins the two branches into one head. It contains no schema changes.
"""

from __future__ import annotations

revision = "0058_merge_0057_heads"
down_revision = ("0057_inbox_order", "0057_individual_accounts")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge point for the 0057 branches - nothing to apply."""


def downgrade() -> None:
    """Un-merge the 0057 branches - nothing to revert."""
