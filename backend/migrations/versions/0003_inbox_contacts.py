"""inbox + contacts: contacts, companies, tags, notes, custom fields, thread state, 2FA

All schema changes are ADDITIVE. The two data fixes are idempotent and safe to re-run.

Revision ID: 0003_inbox_contacts
Revises: 0002_messaging
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0003_inbox_contacts"
down_revision = "0002_messaging"
branch_labels = None
depends_on = None

# Permission literals are INLINED here on purpose: a migration must not import app code,
# because the app's constants will keep changing while this migration must keep meaning
# what it meant the day it ran.
_ALL_PERMISSIONS = [
    "org:read", "org:update", "org:delete", "org:billing",
    "members:read", "members:invite", "members:update", "members:remove",
    "roles:read", "roles:write",
    "inbox:read", "inbox:send", "inbox:manage",
    "contacts:read", "contacts:write",
    "numbers:read", "numbers:manage",
    "campaigns:read", "campaigns:manage",
    "calls:read", "calls:place",
    "reports:read",
    "settings:read", "settings:write",
]
_ADMIN = [p for p in _ALL_PERMISSIONS if p not in ("org:delete", "org:billing")]
_AGENT = ["inbox:read", "inbox:send", "inbox:manage", "contacts:read", "contacts:write"]
_SYSTEM_ROLE_PERMISSIONS = {"owner": ["*"], "admin": _ADMIN, "agent": _AGENT}


def _org_fk(name: str = "org_id") -> sa.Column:
    return sa.Column(
        name, GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    # ---------------- companies / contacts / phones ----------------
    op.create_table(
        "companies",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("attributes", PortableJSON(), nullable=False),
        *_stamps(),
    )
    op.create_index("ix_companies_org_id", "companies", ["org_id"])

    op.create_table(
        "contacts",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("first_name", sa.String(127), nullable=True),
        sa.Column("last_name", sa.String(127), nullable=True),
        sa.Column(
            "company_id", GUID(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("attributes", PortableJSON(), nullable=False),
        *_stamps(),
    )
    op.create_index("ix_contacts_org_id", "contacts", ["org_id"])
    op.create_index("ix_contacts_org_display_name", "contacts", ["org_id", "display_name"])

    op.create_table(
        "contact_phones",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column(
            "contact_id", GUID(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("e164", sa.String(20), nullable=False),
        sa.Column("label", sa.String(31), nullable=False, server_default="mobile"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_stamps(),
        # A phone belongs to at most one contact per org. This is what makes
        # phone -> contact resolution deterministic and prevents the classic dupe.
        sa.UniqueConstraint("org_id", "e164", name="uq_contact_phones_org_e164"),
    )
    op.create_index("ix_contact_phones_org_id", "contact_phones", ["org_id"])
    op.create_index("ix_contact_phones_contact_id", "contact_phones", ["contact_id"])

    # ---------------- tags / labels / notes ----------------
    op.create_table(
        "tags",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("color", sa.String(7), nullable=False, server_default="#64748b"),
        *_stamps(),
        sa.UniqueConstraint("org_id", "name", name="uq_tags_org_name"),
    )
    op.create_index("ix_tags_org_id", "tags", ["org_id"])

    op.create_table(
        "contact_tags",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column(
            "contact_id", GUID(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tag_id", GUID(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("contact_id", "tag_id", name="uq_contact_tags"),
    )
    op.create_index("ix_contact_tags_org_id", "contact_tags", ["org_id"])
    op.create_index("ix_contact_tags_contact_id", "contact_tags", ["contact_id"])
    op.create_index("ix_contact_tags_tag_id", "contact_tags", ["tag_id"])

    op.create_table(
        "thread_labels",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column(
            "thread_id",
            GUID(),
            sa.ForeignKey("message_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag_id", GUID(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("thread_id", "tag_id", name="uq_thread_labels"),
    )
    op.create_index("ix_thread_labels_org_id", "thread_labels", ["org_id"])
    op.create_index("ix_thread_labels_thread_id", "thread_labels", ["thread_id"])
    op.create_index("ix_thread_labels_tag_id", "thread_labels", ["tag_id"])

    op.create_table(
        "contact_notes",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column(
            "contact_id", GUID(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "author_user_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("body", sa.Text(), nullable=False),
        *_stamps(),
    )
    op.create_index("ix_contact_notes_org_id", "contact_notes", ["org_id"])
    op.create_index("ix_contact_notes_contact_id", "contact_notes", ["contact_id"])

    op.create_table(
        "custom_field_defs",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("label", sa.String(127), nullable=False),
        sa.Column("kind", sa.String(15), nullable=False, server_default="text"),
        sa.Column("options", PortableJSON(), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("org_id", "key", name="uq_custom_field_defs_org_key"),
    )
    op.create_index("ix_custom_field_defs_org_id", "custom_field_defs", ["org_id"])

    # ---------------- message_threads: conversation state ----------------
    op.add_column(
        "message_threads",
        sa.Column(
            "contact_id", GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.add_column(
        "message_threads",
        sa.Column("status", sa.String(8), nullable=False, server_default="open"),
    )
    op.add_column(
        "message_threads",
        sa.Column(
            "assigned_user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "message_threads", sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_message_threads_contact_id", "message_threads", ["contact_id"])
    op.create_index("ix_message_threads_assigned_user_id", "message_threads", ["assigned_user_id"])
    op.create_index(
        "ix_threads_org_status_last", "message_threads", ["org_id", "status", "last_message_at"]
    )

    # ---------------- users: 2FA ----------------
    op.add_column("users", sa.Column("totp_secret", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("totp_last_used_step", sa.BigInteger(), nullable=True))

    # ---------------- data fixes (idempotent) ----------------
    # The inbox sorts on last_message_at; NULLs would make the keyset walk partial.
    op.execute(
        "UPDATE message_threads SET last_message_at = created_at WHERE last_message_at IS NULL"
    )

    # Re-seed system roles so existing orgs gain inbox:manage. Only is_system rows are
    # touched; hand-edited custom roles are left alone.
    import json

    threads = sa.table(
        "roles",
        sa.column("name", sa.String),
        sa.column("permissions", sa.JSON),
        sa.column("is_system", sa.Boolean),
    )
    for role_name, perms in _SYSTEM_ROLE_PERMISSIONS.items():
        op.execute(
            threads.update()
            .where(sa.and_(threads.c.name == role_name, threads.c.is_system.is_(True)))
            .values(permissions=json.loads(json.dumps(perms)))
        )


def downgrade() -> None:
    op.drop_column("users", "totp_last_used_step")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")

    op.drop_index("ix_threads_org_status_last", table_name="message_threads")
    op.drop_index("ix_message_threads_assigned_user_id", table_name="message_threads")
    op.drop_index("ix_message_threads_contact_id", table_name="message_threads")
    op.drop_column("message_threads", "last_read_at")
    op.drop_column("message_threads", "assigned_user_id")
    op.drop_column("message_threads", "status")
    op.drop_column("message_threads", "contact_id")

    op.drop_table("custom_field_defs")
    op.drop_table("contact_notes")
    op.drop_table("thread_labels")
    op.drop_table("contact_tags")
    op.drop_table("tags")
    op.drop_table("contact_phones")
    op.drop_table("contacts")
    op.drop_table("companies")
