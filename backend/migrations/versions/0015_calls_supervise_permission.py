"""P12: backfill calls:supervise onto existing system 'admin' roles.

New orgs get it from SYSTEM_ROLES automatically; this reaches roles seeded before the
permission existed. Owner needs nothing (wildcard). Custom roles are deliberately left
alone — an operator curated those lists.

Revision ID: 0015_calls_supervise
Revises: 0014_platform_services
Create Date: 2026-08-29
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0015_calls_supervise"
down_revision = "0014_platform_services"
branch_labels = None
depends_on = None

_PERM = "calls:supervise"


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name not in ("postgresql", "sqlite"):
        return
    rows = conn.execute(
        sa.text("SELECT id, permissions FROM roles WHERE name = 'admin' AND is_system = true")
        if conn.dialect.name == "postgresql"
        else sa.text("SELECT id, permissions FROM roles WHERE name = 'admin' AND is_system = 1")
    ).fetchall()
    for role_id, permissions in rows:
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
    rows = conn.execute(
        sa.text("SELECT id, permissions FROM roles WHERE name = 'admin'")
    ).fetchall()
    for role_id, permissions in rows:
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        if _PERM not in perms:
            continue
        perms.remove(_PERM)
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(perms), "id": role_id},
        )
