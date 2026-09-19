"""Join the messaging-health branch to the P41-P45 chain.

Revision ID: 0056_merge_heads
Revises: 0044_messaging_health, 0055_prepaid_by_default
Create Date: 2026-09-19

No DDL. This exists purely to heal a split in the revision graph, and the split is
worth recording because it was invisible to git.

Two migrations were authored as "0044" on different branches, both declaring
`down_revision = "0043_plan_allowances"`:

    0043_plan_allowances
      |-- 0044_messaging_health      (main; DEPLOYED - production's version pointer
      |                               sits here)
      `-- 0044_account_security      (p41-kyc)
            `-- 0045_kyc ... 0055_prepaid_by_default

Git merged them without a conflict, because they are two different FILES that never
touch the same line. Nothing in a diff, a review or a test run says anything is wrong.
Alembic then sees two heads and `alembic upgrade head` aborts with "Multiple head
revisions are present".

That failure mode is worse than it sounds given how this repo deploys:
deploy/deploy.sh ships the tracked files first (`git archive HEAD`, ~line 125) and runs
`alembic upgrade head` afterwards (~line 194). So without this revision a deploy leaves
the new application code running against a database that never migrated - every table
added by 0044_account_security onward is simply absent.

With this merge revision the graph has a single head again and a production database
stamped at 0044_messaging_health walks 0044_account_security -> ... -> 0055 -> 0056 in
one `upgrade head`, keeping the messaging-health tables it already has.

Do not "clean this up" by renumbering either 0044. 0044_messaging_health is already
applied in production; changing its revision id would strand that database on an id
alembic can no longer find.
"""

from __future__ import annotations

revision = "0056_merge_heads"
down_revision = ("0044_messaging_health", "0055_prepaid_by_default")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Graph-only. Both parents have already done their own DDL."""


def downgrade() -> None:
    """Splits the graph back into two heads; the parents keep their own downgrades."""
