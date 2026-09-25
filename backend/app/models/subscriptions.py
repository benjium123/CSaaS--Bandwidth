"""The local mirror of a Stripe subscription, and the ONE predicate that decides access.

An org subscribes to exactly one of the catalogue plans (app/models/plans.py) through
Stripe Checkout in ``subscription`` mode; the ``customer.subscription.*`` webhooks keep the
row here in step with Stripe. The org -> plan link itself is unchanged: it stays
``orgs.plan_code``, which is what services/plans.py reads. This table records the PAYMENT
relationship, not a second plan linkage.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

#: Stripe's own subscription statuses, mirrored exactly. Widening this tuple is not the
#: same as widening access - is_entitled() below is the only thing that grants it.
SUBSCRIPTION_STATUSES: tuple[str, ...] = (
    "incomplete",
    "incomplete_expired",
    "trialing",
    "active",
    "past_due",
    "canceled",
    "unpaid",
    "paused",
)

#: PAST_DUE IS ENTITLED, and that is the judgement call in this module.
#: Stripe sets past_due on the FIRST failed renewal charge and then keeps retrying for days
#: under its dunning schedule. Cutting a business off from its own phone number and inbound
#: calls the instant one retry fails is disproportionate to what is nearly always an expired
#: card, and the damage (missed customer calls) cannot be undone by paying afterwards. When
#: dunning is exhausted Stripe moves the subscription to `canceled` or `unpaid` - neither of
#: which is in here - so service does end for genuine non-payment, at the end of the grace
#: period rather than at its start.
ENTITLED_SUBSCRIPTION_STATUSES: frozenset[str] = frozenset({"trialing", "active", "past_due"})


def is_entitled(status: str | None) -> bool:
    """True exactly when this Stripe status grants access to the phone system.

    The single predicate: no caller anywhere may re-derive entitlement from a status
    string, or the rule ends up written twice and true in one place. Total by
    construction - None, "" and any status Stripe adds later answer False, so a new
    Stripe status can never silently widen access.
    """
    return status in ENTITLED_SUBSCRIPTION_STATUSES


class Subscription(Base, TenantScoped, TimestampMixin):
    """One org's Stripe subscription, as Stripe last told us it was.

    Never the source of truth - Stripe is - which is why every field is written from a
    verified webhook and nothing here is computed locally. The unique key on
    ``stripe_subscription_id`` is load-bearing: webhook handling upserts on it, so a
    replayed or out-of-order delivery updates the one row rather than creating a second.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"
        ),
        sa.Index("ix_subscriptions_org_status", "org_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    plan_code: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("plans.code", ondelete="RESTRICT"), nullable=False
    )
    stripe_subscription_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(24), nullable=False, default="incomplete", server_default="incomplete"
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    #: Workspace plans (services/plan_billing.py): paid add-ons on top of what the plan
    #: includes, mirrored from the subscription item quantities on every Stripe event.
    extra_users: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    extra_numbers: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
