"""Deleting a contact hides it instead of destroying it (restorable).

deleted_at marks the row; deleted_phones keeps the numbers it had, because the contact_phones
rows are released on delete (org+e164 is unique, so a kept row would stop the number from
being added again or from creating a fresh contact on the next inbound text).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0079_contact_soft_delete"
down_revision = "0078_org_features"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "deleted_by_user_id",
                GUID(),
                sa.ForeignKey(
                    "users.id", ondelete="SET NULL", name="fk_contacts_deleted_by_user_id"
                ),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("deleted_phones", PortableJSON(), nullable=True))
    op.create_index("ix_contacts_org_deleted_at", "contacts", ["org_id", "deleted_at"])


def downgrade():
    op.drop_index("ix_contacts_org_deleted_at", table_name="contacts")
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("deleted_phones")
        batch.drop_column("deleted_by_user_id")
        batch.drop_column("deleted_at")
