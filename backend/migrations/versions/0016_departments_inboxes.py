"""P15: departments, inboxes, tiered inbox grants.

Additive + fail-closed. Every existing org_number gets an Inbox row (named by its e164)
with NO grants — after upgrade, non-admin users see nothing until an admin grants
inboxes. That is deliberate: a missing grant must never widen access. Admins are
unaffected (``inboxes:admin`` bypasses the tier), so day-one behaviour for the people
who configure the system is unchanged.

Also backfills the three new permissions onto existing system 'admin' roles, exactly as
0015 did for calls:supervise. Owner needs nothing (wildcard); custom roles are left
alone — an operator curated those lists.

Revision ID: 0016_departments_inboxes
Revises: 0015_calls_supervise
Create Date: 2026-09-01
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0016_departments_inboxes"
down_revision = "0015_calls_supervise"
branch_labels = None
depends_on = None

_NEW_PERMS = ("inboxes:admin", "departments:read", "departments:manage")


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_departments_org_name"),
    )

    op.create_table(
        "department_members",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "department_id",
            GUID(),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("department_id", "user_id", name="uq_dept_members_dept_user"),
    )

    op.create_table(
        "inboxes",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column(
            "number_id",
            GUID(),
            sa.ForeignKey("org_numbers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("number_id", name="uq_inboxes_number"),
    )

    op.create_table(
        "inbox_grants",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "inbox_id",
            GUID(),
            sa.ForeignKey("inboxes.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("grantee_type", sa.String(12), nullable=False),
        sa.Column("grantee_id", GUID(), nullable=False, index=True),
        sa.Column("role", sa.String(8), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "inbox_id", "grantee_type", "grantee_id", name="uq_inbox_grants_inbox_grantee"
        ),
    )

    conn = op.get_bind()

    # ---- Backfill: one inbox per existing number, named by its e164, zero grants -----
    numbers = conn.execute(sa.text("SELECT id, org_id, e164 FROM org_numbers")).fetchall()
    for number_id, org_id, e164 in numbers:
        conn.execute(
            sa.text(
                "INSERT INTO inboxes (id, org_id, name, number_id, color, created_at,"
                " updated_at) VALUES (:id, :org_id, :name, :number_id, NULL,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "id": str(uuid.uuid4()) if conn.dialect.name == "sqlite" else uuid.uuid4(),
                "org_id": org_id,
                "name": e164,
                "number_id": number_id,
            },
        )

    # ---- Backfill: new permissions onto existing system admin roles ------------------
    is_true = "true" if conn.dialect.name == "postgresql" else "1"
    rows = conn.execute(
        sa.text(f"SELECT id, permissions FROM roles WHERE name = 'admin' AND is_system = {is_true}")
    ).fetchall()
    for role_id, permissions in rows:
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        if "*" in perms:
            continue
        changed = False
        for perm in _NEW_PERMS:
            if perm not in perms:
                perms.append(perm)
                changed = True
        if changed:
            conn.execute(
                sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
                {"p": json.dumps(perms), "id": role_id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    # Symmetric with upgrade: system admin roles only — operator-curated custom roles
    # named 'admin' were never touched on the way up, so they keep whatever they have.
    is_true = "true" if conn.dialect.name == "postgresql" else "1"
    rows = conn.execute(
        sa.text(f"SELECT id, permissions FROM roles WHERE name = 'admin' AND is_system = {is_true}")
    ).fetchall()
    for role_id, permissions in rows:
        perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
        kept = [p for p in perms if p not in _NEW_PERMS]
        if len(kept) != len(perms):
            conn.execute(
                sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
                {"p": json.dumps(kept), "id": role_id},
            )

    op.drop_table("inbox_grants")
    op.drop_table("inboxes")
    op.drop_table("department_members")
    op.drop_table("departments")
