"""P16: backfill calls:read onto existing system 'agent' roles.

The unified conversation timeline renders calls next to SMS, so an inbox agent needs
calls:read (P15 grants still decide WHICH numbers). New orgs get it from SYSTEM_ROLES;
this reaches agent roles seeded before. Custom roles are left alone, as in 0015/0016.

Revision ID: 0017_agent_calls_read
Revises: 0016_departments_inboxes
Create Date: 2026-09-01
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0017_agent_calls_read"
down_revision = "0016_departments_inboxes"
branch_labels = None
depends_on = None

_PERM = "calls:read"


def _system_agent_rows(conn):  # noqa: ANN001, ANN202
    is_true = "true" if conn.dialect.name == "postgresql" else "1"
    return conn.execute(
        sa.text(f"SELECT id, permissions FROM roles WHERE name = 'agent' AND is_system = {is_true}")
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
