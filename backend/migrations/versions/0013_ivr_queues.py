"""P12: IVR call flows, ring groups, queues, business hours, voicemails.

Additive. org_numbers.call_flow_id is NULL for every existing number, so nothing
changes for inbound calls until an operator binds a flow (phase-12-plan DR-3).

Revision ID: 0013_ivr_queues
Revises: 0012_outbound_engine
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0013_ivr_queues"
down_revision = "0012_outbound_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_flows",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("definition", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", "version", name="uq_call_flows_org_name_version"),
    )

    op.add_column(
        "org_numbers",
        sa.Column(
            "call_flow_id",
            GUID(),
            sa.ForeignKey("call_flows.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "ring_groups",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("strategy", sa.String(16), nullable=False, server_default="simultaneous"),
        sa.Column("member_user_ids", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("ring_timeout_seconds", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_ring_groups_org_name"),
    )

    op.create_table(
        "call_queues",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("hold_audio_url", sa.String(512), nullable=True),
        sa.Column("max_wait_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("overflow", sa.String(16), nullable=False, server_default="voicemail"),
        sa.Column(
            "ring_group_id",
            GUID(),
            sa.ForeignKey("ring_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_call_queues_org_name"),
    )

    op.create_table(
        "queue_entries",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "queue_id",
            GUID(),
            sa.ForeignKey("call_queues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "call_id",
            GUID(),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("state", sa.String(24), nullable=False, server_default="waiting"),
        sa.Column(
            "offered_user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_e164", sa.String(20), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_queue_entries_queue_state", "queue_entries", ["queue_id", "state"])

    op.create_table(
        "business_hours",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False, server_default="default"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Chicago"),
        sa.Column("schedule", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("holidays", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_business_hours_org_name"),
    )

    op.create_table(
        "voicemails",
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
            "recording_id",
            GUID(),
            sa.ForeignKey("call_recordings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("greeting_node", sa.String(64), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("transcript_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("status", sa.String(8), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("voicemails")
    op.drop_table("business_hours")
    op.drop_index("ix_queue_entries_queue_state", table_name="queue_entries")
    op.drop_table("queue_entries")
    op.drop_table("call_queues")
    op.drop_table("ring_groups")
    op.drop_column("org_numbers", "call_flow_id")
    op.drop_table("call_flows")
