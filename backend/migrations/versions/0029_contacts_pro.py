"""P27 contacts pro: merge, saved views, retention, subject erasure, import summary (D39).

- contacts.merged_into_contact_id     the survivor after a merge (loser row kept, hidden)
- saved_views                         per-user or shared filter sets on Contacts
- retention_policies                  one row per org: messages/recordings/transcripts/imports days
- erasure_requests                    "Erase this person" jobs (anonymise + delete recordings;
                                      compliance ledger rows survive by law)
- contact_lists.import_summary        JSON summary of the last import (unknown owners, assigned)

Additive. Revision chains after 0028_inbox_pro.

Revision ID: 0029_contacts_pro
Revises: 0028_inbox_pro
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0029_contacts_pro"
down_revision = "0028_inbox_pro"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "merged_into_contact_id",
            GUID(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_contacts_org_merged_into", "contacts", ["org_id", "merged_into_contact_id"])

    op.create_table(
        "saved_views",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        # NULL user_id = shared with the whole workspace
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("filters", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("sort", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_saved_views_org_user", "saved_views", ["org_id", "user_id"])

    op.create_table(
        "retention_policies",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        # NULL = keep forever
        sa.Column("messages_days", sa.Integer(), nullable=True),
        sa.Column("recordings_days", sa.Integer(), nullable=True, server_default="90"),
        sa.Column("transcripts_days", sa.Integer(), nullable=True, server_default="365"),
        sa.Column("imports_days", sa.Integer(), nullable=True, server_default="30"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_retention_policies_org"),
    )

    op.create_table(
        "erasure_requests",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "contact_id", GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "requested_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        # pending | done | failed
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_erasure_requests_org_status", "erasure_requests", ["org_id", "status"])

    op.add_column(
        "contact_lists", sa.Column("import_summary", PortableJSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("contact_lists", "import_summary")
    op.drop_index("ix_erasure_requests_org_status", table_name="erasure_requests")
    op.drop_table("erasure_requests")
    op.drop_table("retention_policies")
    op.drop_index("ix_saved_views_org_user", table_name="saved_views")
    op.drop_table("saved_views")
    op.drop_index("ix_contacts_org_merged_into", table_name="contacts")
    op.drop_column("contacts", "merged_into_contact_id")
