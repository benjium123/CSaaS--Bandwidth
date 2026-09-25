"""User seats come from the workspace plan: the users it includes plus paid add-on users
(services/plan_billing.py). The owner occupies a seat like anyone else.

Before workspace plans, seats followed paid phone numbers (one paid, unreleased number bought
one user). That still applies to a workspace holding such a purchase and no plan:

The owner occupies a seat like anyone else, so a workspace that bought three numbers holds
three people. The seat count is read from the SAME source Stripe bills from - the entries of
number purchases whose subscription is entitled, minus released and refunded ones (both
lower the Stripe quantity, in ``sync_released_number`` and ``refund_unprovisioned``) - so
what a customer pays for and what they may use can never drift apart.

Workspaces created before per-number billing (``number_subscription_required`` false) are
not limited: they never bought seats and must not lose the ability to add teammates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import PermissionDeniedError

_UNSCOPED = {ALLOW_UNSCOPED_KEY: True}


@dataclass(frozen=True)
class SeatUsage:
    enforced: bool
    limit: int | None
    members: int
    pending_invites: int

    @property
    def available(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.members - self.pending_invites, 0)

    def public(self) -> dict:
        return {
            "enforced": self.enforced,
            "limit": self.limit,
            "members": self.members,
            "pending_invites": self.pending_invites,
            "available": self.available,
        }


async def paid_numbers(session: AsyncSession, org_id: uuid.UUID) -> int:
    from app.models import NumberPurchase
    from app.models.subscriptions import ENTITLED_SUBSCRIPTION_STATUSES

    purchases = (
        await session.execute(
            sa.select(NumberPurchase.numbers)
            .where(
                NumberPurchase.org_id == org_id,
                NumberPurchase.subscription_status.in_(ENTITLED_SUBSCRIPTION_STATUSES),
            )
            .execution_options(**_UNSCOPED)
        )
    ).scalars()
    return sum(
        1
        for numbers in purchases
        for entry in numbers or []
        if entry.get("state") not in ("released", "refunded")
    )


async def usage(session: AsyncSession, org_id: uuid.UUID, *, lock: bool = False) -> SeatUsage:
    from app.models import Org
    from app.models.invites import Invite
    from app.models.rbac import OrgMembership

    stmt = sa.select(Org).where(Org.id == org_id).execution_options(**_UNSCOPED)
    if lock:
        # Serialises concurrent adds for one workspace, so two admins racing for the last
        # seat cannot both get it.
        stmt = stmt.with_for_update()
    org = (await session.execute(stmt)).scalar_one()
    members = (
        await session.execute(
            sa.select(sa.func.count(OrgMembership.id))
            .where(OrgMembership.org_id == org_id)
            .execution_options(**_UNSCOPED)
        )
    ).scalar_one()
    pending = (
        await session.execute(
            sa.select(sa.func.count(Invite.id))
            .where(
                Invite.org_id == org_id,
                Invite.accepted_at.is_(None),
                Invite.revoked_at.is_(None),
                Invite.expires_at > datetime.now(timezone.utc),
            )
            .execution_options(**_UNSCOPED)
        )
    ).scalar_one()
    if not org.number_subscription_required:
        return SeatUsage(False, None, int(members), int(pending))
    from app.services import plan_billing

    ent = await plan_billing.entitlement(session, org_id)
    if ent is not None:
        return SeatUsage(True, ent.users, int(members), int(pending))
    return SeatUsage(True, await paid_numbers(session, org_id), int(members), int(pending))


async def require_seat(
    session: AsyncSession, org_id: uuid.UUID, *, redeeming_invite: bool = False
) -> None:
    """Refuse adding a person when every paid seat is taken.

    Outstanding invites hold a seat, so an admin cannot send ten invitations against two
    seats. The invite being redeemed already holds its own seat, so it is not counted twice.
    """
    seats = await usage(session, org_id, lock=True)
    if not seats.enforced:
        return
    taken = seats.members + seats.pending_invites - (1 if redeeming_invite else 0)
    if taken < (seats.limit or 0):
        return
    if seats.limit:
        message = (
            f"All {seats.limit} users on your plan are in use. Add a user for $15/month "
            "from Team, or move to a bigger plan."
        )
    else:
        message = "Choose a plan to add users. Every plan comes with users and numbers."
    raise PermissionDeniedError(message, code="seat_limit_reached")
