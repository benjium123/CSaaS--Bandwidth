"""P36 trust: PII envelope encryption columns, per-org data keys, status incidents.

- org_data_keys                 per-org data-encryption key wrapped by CREDENTIALS_MASTER_KEY,
                                with a version for rotation
- messages.body_enc, call_transcripts.text_enc, contact_notes.body_enc
                                ciphertext columns; plaintext columns are DROPPED in a
                                follow-up migration after the backfill job completes
- contacts.search_hash          keyed hash of the normalised primary phone/name for exact match
- status_incidents              public status-page history

Additive. Revision chains after 0037_channels.

Revision ID: 0038_trust
Revises: 0037_channels
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0038_trust"
down_revision = "0037_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_data_keys",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "version", name="uq_org_data_keys_org_version"),
    )
    op.create_index("ix_org_data_keys_org_current", "org_data_keys", ["org_id", "is_current"])

    op.add_column("messages", sa.Column("body_enc", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("enc_key_version", sa.Integer(), nullable=True))
    op.add_column("call_transcripts", sa.Column("text_enc", sa.Text(), nullable=True))
    op.add_column("call_transcripts", sa.Column("enc_key_version", sa.Integer(), nullable=True))
    op.add_column("contact_notes", sa.Column("body_enc", sa.Text(), nullable=True))
    op.add_column("contact_notes", sa.Column("enc_key_version", sa.Integer(), nullable=True))
    op.add_column("contacts", sa.Column("search_hash", sa.String(64), nullable=True))
    op.create_index("ix_contacts_org_search_hash", "contacts", ["org_id", "search_hash"])

    op.create_table(
        "status_incidents",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # api | db | redis | media_plane | messaging | voice
        sa.Column("component", sa.String(16), nullable=False),
        # minor | major | critical
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("title", sa.String(127), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_status_incidents_started", "status_incidents", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_status_incidents_started", table_name="status_incidents")
    op.drop_table("status_incidents")
    op.drop_index("ix_contacts_org_search_hash", table_name="contacts")
    op.drop_column("contacts", "search_hash")
    op.drop_column("contact_notes", "enc_key_version")
    op.drop_column("contact_notes", "body_enc")
    op.drop_column("call_transcripts", "enc_key_version")
    op.drop_column("call_transcripts", "text_enc")
    op.drop_column("messages", "enc_key_version")
    op.drop_column("messages", "body_enc")
    op.drop_index("ix_org_data_keys_org_current", table_name="org_data_keys")
    op.drop_table("org_data_keys")
