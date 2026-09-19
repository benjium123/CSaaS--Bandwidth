"""Backfill calls:place onto existing system 'agent' roles.

An employee given a line is given the whole line: this product is calls and texts on one
number, and an agent who could text from a line but not ring back from it had half a
phone. New orgs get it from SYSTEM_ROLES; this reaches agent roles seeded before.

Deliberately narrow, exactly as 0015/0016/0017 were:
  * system 'agent' roles only - a CUSTOM role named 'agent' is somebody's own policy and
    is left alone,
  * skipped when the permission is already present or the role holds the wildcard,
  * reversible, and the downgrade removes only what the upgrade added.

This widens capability, not reach. `services/inbox_access.py` still decides WHICH numbers
a caller may use, and `create_call` refuses any `from` the caller lacks `can_use` on - so
an agent can dial out only from a line they hold a `member` grant for. The prepaid hard
gate and monitoring selection both sit below the permission check in
`voice_plane/service.py` and are unaffected.

Revision ID: 0052_agent_calls_place
Revises: 0051_monitoring
Create Date: 2026-09-18
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0052_agent_calls_place"
down_revision = "0051_monitoring"
branch_labels = None
depends_on = None

_PERM = "calls:place"


def _system_agent_rows(conn):  # noqa: ANN001, ANN202
    # SQLite has no native boolean literal; mirrors 0017 rather than inventing a second
    # way of asking the same question.
    is_true = "true" if conn.dialect.name == "postgresql" else "1"
    return conn.execute(
        sa.text(
            f"SELECT id, permissions FROM roles WHERE name = 'agent' AND is_system = {is_true}"
        )
    ).fetchall()


def upgrade() -> None:
    conn = op.get_bind()
    for role_id, permissions in _system_agent_rows(conn):
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        if _PERM in perms or "*" in perms:
            continue
        perms.append(_PERM)
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(perms), "id": role_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for role_id, permissions in _system_agent_rows(conn):
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        if _PERM not in perms:
            continue
        perms.remove(_PERM)
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(perms), "id": role_id},
        )
