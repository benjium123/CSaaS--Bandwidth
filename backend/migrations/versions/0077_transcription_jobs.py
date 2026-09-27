"""Transcription queue: one job per call, scheduled by services/transcription.py. Additive only."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0077_transcription_jobs"
down_revision = "0076_fixed_costs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transcription_jobs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "call_id", GUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "recording_id",
            GUID(),
            sa.ForeignKey("call_recordings.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tier", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("engine", sa.String(16)),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(255)),
        sa.Column("audio_seconds", sa.Integer()),
        sa.Column("cpu_ms", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("call_id", name="uq_transcription_jobs_call"),
    )
    op.create_index("ix_transcription_jobs_org_id", "transcription_jobs", ["org_id"])
    op.create_index(
        "ix_transcription_jobs_status_tier",
        "transcription_jobs",
        ["status", "tier", "created_at"],
    )


def downgrade():
    op.drop_index("ix_transcription_jobs_status_tier", table_name="transcription_jobs")
    op.drop_index("ix_transcription_jobs_org_id", table_name="transcription_jobs")
    op.drop_table("transcription_jobs")
