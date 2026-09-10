"""P29 voice completeness: dual-channel recording, consent announcement, dispositions.

- call_recordings.channel_layout     'mixed' | 'dual'
- orgs.recording_announcement,
  orgs.recording_announcement_text   play a consent line before connect
- calls.disposition, calls.disposition_note   human-call outcome picked after the call

Additive. Revision chains after 0030_messaging_completeness.

Revision ID: 0031_voice_completeness
Revises: 0030_messaging_completeness
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_voice_completeness"
down_revision = "0030_messaging_completeness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_recordings",
        sa.Column("channel_layout", sa.String(8), nullable=False, server_default="mixed"),
    )
    op.add_column(
        "orgs",
        sa.Column(
            "recording_announcement", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("orgs", sa.Column("recording_announcement_text", sa.Text(), nullable=True))
    op.add_column("calls", sa.Column("disposition", sa.String(32), nullable=True))
    op.add_column("calls", sa.Column("disposition_note", sa.Text(), nullable=True))
    op.create_index("ix_calls_org_disposition", "calls", ["org_id", "disposition"])


def downgrade() -> None:
    op.drop_index("ix_calls_org_disposition", table_name="calls")
    op.drop_column("calls", "disposition_note")
    op.drop_column("calls", "disposition")
    op.drop_column("orgs", "recording_announcement_text")
    op.drop_column("orgs", "recording_announcement")
    op.drop_column("call_recordings", "channel_layout")
