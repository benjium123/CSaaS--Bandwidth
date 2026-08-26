"""voice core: calls, call_legs, voice_events, call_recordings

Purely additive - four new tables, nothing existing is touched.

Revision ID: 0007_voice_core
Revises: 0006_numbers_10dlc_tfv
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0007_voice_core"
down_revision = "0006_numbers_10dlc_tfv"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calls",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("contact_e164", sa.String(20), nullable=False, index=True),
        sa.Column("our_e164", sa.String(20), nullable=False, index=True),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("tag", sa.String(128)),
        sa.Column("extra", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_calls_org_created", "calls", ["org_id", "created_at"])

    op.create_table(
        "call_legs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "call_id",
            GUID(),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider_call_id", sa.String(128), index=True),
        sa.Column("to_e164", sa.String(20), nullable=False),
        sa.Column("from_e164", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("reason", sa.String(16), nullable=False, server_default="original"),
        sa.Column("amd_result", sa.String(16)),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("hangup_cause", sa.String(64)),
        sa.Column("extra", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_call_id", name="uq_call_legs_provider_call_id"),
    )

    op.create_table(
        "voice_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "call_id", GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), index=True
        ),
        sa.Column(
            "leg_id", GUID(), sa.ForeignKey("call_legs.id", ondelete="CASCADE"), index=True
        ),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("provider_event_id", sa.String(160), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "carrier", "provider_event_id", name="uq_voice_events_carrier_event"
        ),
    )

    op.create_table(
        "call_recordings",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "call_id",
            GUID(),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "leg_id", GUID(), sa.ForeignKey("call_legs.id", ondelete="SET NULL")
        ),
        sa.Column("provider_recording_id", sa.String(128), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column(
            "content_type", sa.String(64), nullable=False, server_default="audio/mpeg"
        ),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_recording_id", name="uq_call_recordings_provider_id"),
    )


def downgrade() -> None:
    op.drop_table("call_recordings")
    op.drop_table("voice_events")
    op.drop_table("call_legs")
    op.drop_index("ix_calls_org_created", table_name="calls")
    op.drop_table("calls")
