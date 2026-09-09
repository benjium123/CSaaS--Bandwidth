"""P22: contact ownership, visibility policy, team leads, custom-role permissions.

- contacts.owner_user_id / contacts.department_id  (nullable; unowned is a valid state)
- department_members.is_lead                         (a lead sees their team's contacts under
                                                      the 'owner' policy)
- orgs.contact_visibility                            ('everyone' | 'department' | 'owner';
                                                      default 'everyone' = today's behaviour)
- backfill contacts:read_all + contacts:assign onto existing system 'admin' roles (same
  portable pattern as 0015; owner has the wildcard; custom roles untouched)

All additive. No data rewrite beyond the admin-role backfill.

Revision ID: 0024_contact_ownership_roles
Revises: 0023_thread_is_important
Create Date: 2026-09-10
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0024_contact_ownership_roles"
down_revision = "0023_thread_is_important"
branch_labels = None
depends_on = None

_NEW_PERMS = ("contacts:read_all", "contacts:assign")


def _admin_rows(conn):
    stmt = (
        sa.text("SELECT id, permissions FROM roles WHERE name = 'admin' AND is_system = true")
        if conn.dialect.name == "postgresql"
        else sa.text("SELECT id, permissions FROM roles WHERE name = 'admin' AND is_system = 1")
    )
    return conn.execute(stmt).fetchall()


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "owner_user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "department_id",
            GUID(),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_contacts_org_owner", "contacts", ["org_id", "owner_user_id"])
    op.create_index("ix_contacts_org_department", "contacts", ["org_id", "department_id"])

    op.add_column(
        "department_members",
        sa.Column("is_lead", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column(
        "orgs",
        sa.Column(
            "contact_visibility",
            sa.String(16),
            nullable=False,
            server_default="everyone",
        ),
    )

    conn = op.get_bind()
    if conn.dialect.name not in ("postgresql", "sqlite"):
        return
    for role_id, permissions in _admin_rows(conn):
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        if "*" in perms:
            continue
        missing = [p for p in _NEW_PERMS if p not in perms]
        if not missing:
            continue
        perms.extend(missing)
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(perms), "id": role_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for role_id, permissions in _admin_rows(conn):
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        kept = [p for p in perms if p not in _NEW_PERMS]
        if len(kept) == len(perms):
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(kept), "id": role_id},
        )

    op.drop_column("orgs", "contact_visibility")
    op.drop_column("department_members", "is_lead")
    op.drop_index("ix_contacts_org_department", table_name="contacts")
    op.drop_index("ix_contacts_org_owner", table_name="contacts")
    op.drop_column("contacts", "department_id")
    op.drop_column("contacts", "owner_user_id")
