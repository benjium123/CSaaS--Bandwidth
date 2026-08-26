"""10DLC brands and campaigns, toll-free verification, and number eligibility.

Two regimes live here and they are deliberately NOT merged (phase-4-plan DR-4):

* **10DLC** — local/long-code numbers. brand → campaign → number assignment, ultimately
  landing in TCR.
* **TFV** — toll-free numbers. No brand, no campaign, no TCR; a submission that a carrier
  approves or rejects.

Modelling TFV as "a campaign with a flag" would put two unrelated state machines in one
table, and the first wrong `approved` would be a compliance incident rather than a bug.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

#: Shared by campaigns and TFV submissions. Monotonic: rank never decreases.
REGISTRATION_STATUSES = ("draft", "submitted", "approved", "rejected")

#: Only `approved` may send. The gap between "submitted" and "approved" is where operators
#: assume they are live and are not - so the two must never compare equal anywhere.
REGISTRATION_RANK: dict[str, int] = {
    "draft": 0,
    "submitted": 10,
    "approved": 20,
    "rejected": 20,
}
TERMINAL_REGISTRATION = frozenset({"approved", "rejected"})


def can_send(status: str) -> bool:
    return status == "approved"


class Brand(Base, TenantScoped, TimestampMixin):
    """A legal entity registered for A2P messaging.

    One brand can be registered with SEVERAL carriers at once - which is the user's actual
    situation - so carrier ids live in a map rather than a column.
    """

    __tablename__ = "brands"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_brands_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    ein: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    entity_type: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default="PRIVATE_PROFIT"
    )
    vertical: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    website: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    street: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(sa.String(127), nullable=True)
    state: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    country: Mapped[str] = mapped_column(sa.String(2), nullable=False, default="US")

    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    #: {"bandwidth": "BXXXX", "telnyx": "..."} - one brand, many registrations.
    carrier_refs: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class Campaign(Base, TenantScoped, TimestampMixin):
    """A 10DLC use case. Numbers assigned to it may send; numbers without one may not."""

    __tablename__ = "campaigns"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_campaigns_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    use_case: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="MIXED")
    description: Mapped[str | None] = mapped_column(sa.String(4000), nullable=True)
    #: The consent language regulators ask for. Stored because "how did you get consent"
    #: is the first question in any complaint, and the answer must not live in a slide deck.
    opt_in_process: Mapped[str | None] = mapped_column(sa.String(4000), nullable=True)
    sample_messages: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)

    help_message: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    opt_out_message: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)

    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    carrier_refs: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class TollFreeVerification(Base, TenantScoped, TimestampMixin):
    """A toll-free number's right to send. Not a campaign; do not merge the two."""

    __tablename__ = "tollfree_verifications"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "number_id", name="uq_tfv_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    number_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("org_numbers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    business_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    use_case: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="MIXED")
    use_case_summary: Mapped[str | None] = mapped_column(sa.String(4000), nullable=True)
    opt_in_process: Mapped[str | None] = mapped_column(sa.String(4000), nullable=True)
    opt_in_screenshot_url: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    message_volume: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    carrier_refs: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
