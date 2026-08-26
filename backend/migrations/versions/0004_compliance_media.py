"""compliance + media: consent ledger, DNC, blocks, settings, media assets, templates

All additive. The one data step (re-seeding system roles) is idempotent.

Revision ID: 0004_compliance_media
Revises: 0003_inbox_contacts
Create Date: 2026-08-26
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0004_compliance_media"
down_revision = "0003_inbox_contacts"
branch_labels = None
depends_on = None

# Inlined, as in 0003: a migration must keep meaning what it meant the day it ran, so it
# must not import constants the app will keep changing.
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
    "compliance:read", "compliance:manage",
    "templates:read", "templates:manage",
]
_ADMIN = [p for p in _ALL_PERMISSIONS if p not in ("org:delete", "org:billing")]
_AGENT = [
    "inbox:read", "inbox:send", "inbox:manage",
    "contacts:read", "contacts:write",
    "compliance:read", "templates:read",
]
_SYSTEM_ROLE_PERMISSIONS = {"owner": ["*"], "admin": _ADMIN, "agent": _AGENT}


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "consent_events",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("contact_e164", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False, server_default="sms"),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("keyword_matched", sa.String(31), nullable=True),
        sa.Column(
            "message_id", GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "actor_user_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("details", PortableJSON(), nullable=False),
        *_stamps(),
        # Idempotency for keyword processing under webhook replay, at the constraint level.
        sa.UniqueConstraint("message_id", name="uq_consent_events_message"),
    )
    op.create_index("ix_consent_events_org_id", "consent_events", ["org_id"])
    op.create_index(
        "ix_consent_org_e164_channel_created",
        "consent_events",
        ["org_id", "contact_e164", "channel", "created_at"],
    )

    op.create_table(
        "dnc_entries",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("e164", sa.String(20), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column(
            "added_by_user_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_stamps(),
        sa.UniqueConstraint("org_id", "e164", name="uq_dnc_org_e164"),
    )
    op.create_index("ix_dnc_entries_org_id", "dnc_entries", ["org_id"])

    op.create_table(
        "compliance_blocks",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("contact_e164", sa.String(20), nullable=False),
        sa.Column("from_e164", sa.String(20), nullable=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("body_excerpt", sa.String(255), nullable=True),
        sa.Column("exemption", sa.String(32), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_compliance_blocks_org_id", "compliance_blocks", ["org_id"])
    op.create_index(
        "ix_compliance_blocks_org_e164_created",
        "compliance_blocks",
        ["org_id", "contact_e164", "created_at"],
    )

    op.create_table(
        "compliance_settings",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("window_start", sa.String(5), nullable=False, server_default="08:00"),
        sa.Column("window_end", sa.String(5), nullable=False, server_default="21:00"),
        sa.Column("help_contact", sa.String(255), nullable=False, server_default=""),
        sa.Column("optout_text", sa.Text(), nullable=False),
        sa.Column("optin_text", sa.Text(), nullable=False),
        sa.Column("help_text", sa.Text(), nullable=False),
        sa.Column(
            "quiet_hours_enforced", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        *_stamps(),
        sa.UniqueConstraint("org_id", name="uq_compliance_settings_org"),
    )
    op.create_index("ix_compliance_settings_org_id", "compliance_settings", ["org_id"])

    op.create_table(
        "media_assets",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column(
            "message_id", GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("direction", sa.String(8), nullable=False, server_default="outbound"),
        sa.Column("storage_key", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(127), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("fetch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_media_assets_org_id", "media_assets", ["org_id"])
    op.create_index("ix_media_assets_message_id", "media_assets", ["message_id"])
    op.create_index(
        "ix_media_assets_status_next_attempt", "media_assets", ["status", "next_attempt_at"]
    )

    op.create_table(
        "message_templates",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("media_asset_ids", PortableJSON(), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("org_id", "name", name="uq_templates_org_name"),
    )
    op.create_index("ix_message_templates_org_id", "message_templates", ["org_id"])

    op.add_column(
        "messages", sa.Column("hold_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_messages_hold_until", "messages", ["hold_until"])
    op.add_column("contacts", sa.Column("timezone", sa.String(64), nullable=True))

    # Idempotent data step: existing orgs gain the four new permission keys.
    roles = sa.table(
        "roles",
        sa.column("name", sa.String),
        sa.column("permissions", sa.JSON),
        sa.column("is_system", sa.Boolean),
    )
    for role_name, perms in _SYSTEM_ROLE_PERMISSIONS.items():
        op.execute(
            roles.update()
            .where(sa.and_(roles.c.name == role_name, roles.c.is_system.is_(True)))
            .values(permissions=json.loads(json.dumps(perms)))
        )


def downgrade() -> None:
    op.drop_column("contacts", "timezone")
    op.drop_index("ix_messages_hold_until", table_name="messages")
    op.drop_column("messages", "hold_until")
    op.drop_table("message_templates")
    op.drop_table("media_assets")
    op.drop_table("compliance_settings")
    op.drop_table("compliance_blocks")
    op.drop_table("dnc_entries")
    op.drop_table("consent_events")
