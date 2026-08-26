"""Sticky sender selection.

Replaces P1's ``resolve_from_number``. The rule that matters: **a conversation never
silently changes its sender number.** That is gotcha #18 from the parity research — under
pool churn, threads quietly jump to a different origin number, the recipient sees a
stranger, and opt-out attribution gets confusing. Here a jump requires an explicit,
logged decision.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import StickySenderUnavailableError, ValidationFailedError
from app.models import MessageThread, OrgNumber

log = structlog.get_logger("sender")


def pick_deterministic(contact_e164: str, numbers: Sequence[str]) -> str:
    """Stable contact→number assignment with no counter table.

    Pure and sorted-input-normalised, so the same contact lands on the same number across
    restarts and regardless of the order the pool came back from the database.

    NOTE: changing this function re-shuffles every future conversation's affinity. The unit
    test pins exact outputs so that can never happen by accident.
    """
    if not numbers:
        raise ValidationFailedError("This org has no active numbers; add one first")
    ordered = sorted(numbers)
    digest = hashlib.sha256(contact_e164.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % len(ordered)
    return ordered[idx]


async def _active_numbers(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.is_active.is_(True)))
    ).scalars().all()
    return [n.e164 for n in rows]


async def select_sender(
    session: AsyncSession,
    org_id: uuid.UUID,
    contact_e164: str,
    *,
    requested: str | None = None,
    allow_reassign: bool = False,
) -> str:
    """Choose which of the org's numbers sends to ``contact_e164``."""
    # 1. Explicit request: must be an active number OF THIS ORG. The session guard makes a
    #    number owned by another org indistinguishable from one that does not exist.
    if requested:
        row = (
            await session.execute(
                sa.select(OrgNumber).where(
                    OrgNumber.e164 == requested, OrgNumber.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValidationFailedError(f"{requested} is not an active number for this org")
        return row.e164

    active = await _active_numbers(session)
    if not active:
        raise ValidationFailedError("This org has no active numbers; add one first")

    # 2. Sticky: the most recently used thread for this contact wins.
    prior = (
        await session.execute(
            sa.select(MessageThread)
            .where(MessageThread.contact_e164 == contact_e164)
            .order_by(MessageThread.last_message_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()

    if prior is not None:
        if prior.our_e164 in active:
            return prior.our_e164
        # 3. The sticky number is retired. FAIL LOUDLY rather than silently jumping.
        if not allow_reassign:
            raise StickySenderUnavailableError(
                f"This conversation used {prior.our_e164}, which is no longer active. "
                f"Resend with allow_reassign=true to move it to a different number."
            )
        chosen = pick_deterministic(contact_e164, active)
        log.warning(
            "sticky_sender_reassigned",
            contact=contact_e164,
            previous=prior.our_e164,
            chosen=chosen,
        )
        return chosen

    # 4. Brand-new conversation: deterministic spread across the pool.
    return pick_deterministic(contact_e164, active)
