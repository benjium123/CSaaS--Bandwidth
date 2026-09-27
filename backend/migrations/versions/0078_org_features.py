"""Per-workspace feature switches (org_features).

A missing row means the catalog default (services/entitlements.py). Seeds call_recording
ON for every workspace that already turned customer recording on (calling_settings
record_calls), so the switch-over changes nothing that is live.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0078_org_features"
down_revision = "0077_transcription_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "org_features",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("feature_key", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("price_override_micros", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "feature_key", name="uq_org_features_key"),
    )
    op.create_index("ix_org_features_org_id", "org_features", ["org_id"])

    conn = op.get_bind()
    orgs = sa.table("orgs", sa.column("id", GUID()), sa.column("calling_settings", PortableJSON()))
    features = sa.table(
        "org_features",
        sa.column("id", GUID()),
        sa.column("org_id", GUID()),
        sa.column("feature_key", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    for org_id, settings in conn.execute(sa.select(orgs.c.id, orgs.c.calling_settings)).all():
        if isinstance(settings, str):
            settings = json.loads(settings)
        if isinstance(settings, dict) and settings.get("record_calls") is True:
            conn.execute(
                sa.insert(features).values(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    feature_key="call_recording",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )


def downgrade():
    op.drop_index("ix_org_features_org_id", table_name="org_features")
    op.drop_table("org_features")
