"""numbers: brands, campaigns, toll-free verification, and number lifecycle columns

Additive. Every new `org_numbers` column carries a server_default matching the model
default, so existing rows keep their current meaning: `local`, `active`, no campaign -
which the registration gate reads as "unknown", i.e. exactly the behaviour they had before
this migration existed.

Revision ID: 0006_numbers_10dlc_tfv
Revises: 0005_carrier_routing
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0006_numbers_10dlc_tfv"
down_revision = "0005_carrier_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("ein", sa.String(32), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=False, server_default="PRIVATE_PROFIT"),
        sa.Column("vertical", sa.String(64), nullable=True),
        sa.Column("website", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("street", sa.String(255), nullable=True),
        sa.Column("city", sa.String(127), nullable=True),
        sa.Column("state", sa.String(32), nullable=True),
        sa.Column("postal_code", sa.String(16), nullable=True),
        sa.Column("country", sa.String(2), nullable=False, server_default="US"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("carrier_refs", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_brands_org_name"),
    )

    op.create_table(
        "campaigns",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        # RESTRICT, not CASCADE: deleting a brand out from under an approved campaign
        # would silently un-register live numbers.
        sa.Column(
            "brand_id", GUID(), sa.ForeignKey("brands.id", ondelete="RESTRICT"),
            nullable=False, index=True,
        ),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("use_case", sa.String(64), nullable=False, server_default="MIXED"),
        sa.Column("description", sa.String(4000), nullable=True),
        sa.Column("opt_in_process", sa.String(4000), nullable=True),
        sa.Column("sample_messages", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("help_message", sa.String(500), nullable=True),
        sa.Column("opt_out_message", sa.String(500), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("carrier_refs", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_campaigns_org_name"),
    )

    for column in (
        sa.Column("number_type", sa.String(16), nullable=False, server_default="local"),
        sa.Column("capabilities", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("provider_ref", sa.String(64), nullable=True),
        sa.Column("campaign_id", GUID(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("org_numbers", column)

    op.create_index("ix_org_numbers_campaign_id", "org_numbers", ["campaign_id"])
    op.create_foreign_key(
        "fk_org_numbers_campaign",
        "org_numbers",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "tollfree_verifications",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "number_id", GUID(), sa.ForeignKey("org_numbers.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("business_name", sa.String(255), nullable=False),
        sa.Column("use_case", sa.String(64), nullable=False, server_default="MIXED"),
        sa.Column("use_case_summary", sa.String(4000), nullable=True),
        sa.Column("opt_in_process", sa.String(4000), nullable=True),
        sa.Column("opt_in_screenshot_url", sa.String(500), nullable=True),
        sa.Column("message_volume", sa.Integer, nullable=True),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("carrier_refs", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "number_id", name="uq_tfv_number"),
    )


def downgrade() -> None:
    op.drop_table("tollfree_verifications")
    op.drop_constraint("fk_org_numbers_campaign", "org_numbers", type_="foreignkey")
    op.drop_index("ix_org_numbers_campaign_id", table_name="org_numbers")
    for name in (
        "released_at",
        "campaign_id",
        "provider_ref",
        "status",
        "capabilities",
        "number_type",
    ):
        op.drop_column("org_numbers", name)
    op.drop_table("campaigns")
    op.drop_table("brands")
