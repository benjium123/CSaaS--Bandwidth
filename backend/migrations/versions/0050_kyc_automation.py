"""P43 hands-off verification: residential address per person, AI document reviews.

- kyc_persons.residential_address   where the owner lives now (JSON address)
- kyc_documents.person_id           which owner a proof of address belongs to
- kyc_documents.review_result/review/reviewed_at   the AI read of the document

Existing applications are untouched; the new proof-of-address requirement applies to
applications submitted from now on.

Revision ID: 0050_kyc_automation
Revises: 0049_enterprise_sso
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0050_kyc_automation"
down_revision = "0049_enterprise_sso"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kyc_persons", sa.Column("residential_address", PortableJSON(), nullable=True))
    op.add_column("kyc_documents", sa.Column("person_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_kyc_documents_person_id",
        "kyc_documents",
        "kyc_persons",
        ["person_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_kyc_documents_person_id", "kyc_documents", ["person_id"])
    op.add_column("kyc_documents", sa.Column("review_result", sa.String(16), nullable=True))
    op.add_column("kyc_documents", sa.Column("review", PortableJSON(), nullable=True))
    op.add_column(
        "kyc_documents", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("kyc_documents", "reviewed_at")
    op.drop_column("kyc_documents", "review")
    op.drop_column("kyc_documents", "review_result")
    op.drop_index("ix_kyc_documents_person_id", table_name="kyc_documents")
    op.drop_constraint("fk_kyc_documents_person_id", "kyc_documents", type_="foreignkey")
    op.drop_column("kyc_documents", "person_id")
    op.drop_column("kyc_persons", "residential_address")
