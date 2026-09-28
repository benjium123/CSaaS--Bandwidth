"""A person's own profile: display name and their 911 address.

Like MightyCall, the emergency address belongs to the PERSON: it is validated and
registered with the carrier (services/e911.py) and applied to every number assigned to
them. "Assigned" = numbers they hold a member grant on (never numbers reachable only by
admin access, or an admin's address would overwrite everyone's); in a one-person
workspace, every active number. A shared number takes the address of whoever saved most
recently - the profile lists which numbers carry the person's address.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ValidationFailedError
from app.models import EmergencyAddress, OrgMembership, OrgNumber, User

log = structlog.get_logger("profile")


async def assigned_numbers(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> list[OrgNumber]:
    """Active numbers this person is assigned to (org context must be set)."""
    from app.services import inbox_access

    members = (
        await session.execute(
            sa.select(sa.func.count()).select_from(OrgMembership).where(OrgMembership.org_id == org_id)
        )
    ).scalar_one()
    stmt = sa.select(OrgNumber).where(
        OrgNumber.org_id == org_id,
        OrgNumber.released_at.is_(None),
        OrgNumber.status == "active",
    )
    if int(members) > 1:
        # Empty permissions: explicit member grants only, never admin-wide access.
        # include_reports=False: a manager's reports' lines are never "theirs" here - their
        # 911 address must not be pushed onto numbers other people sit at.
        access = await inbox_access.resolve_access(session, user_id, [], include_reports=False)
        if not access.member_e164s:
            return []
        stmt = stmt.where(OrgNumber.e164.in_(sorted(access.member_e164s)))
    return list((await session.execute(stmt.order_by(OrgNumber.e164))).scalars())


async def my_address(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> EmergencyAddress | None:
    return (
        await session.execute(
            sa.select(EmergencyAddress)
            .where(EmergencyAddress.org_id == org_id, EmergencyAddress.user_id == user_id)
            .order_by(EmergencyAddress.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def profile(session: AsyncSession, org_id: uuid.UUID, user: User) -> dict:
    from app.services import e911

    address = await my_address(session, org_id, user.id)
    numbers = await assigned_numbers(session, org_id, user.id)
    covered = []
    for number in numbers:
        status = e911.status_of(number)
        covered.append(
            {
                "e164": number.e164,
                "status": status.get("status"),
                "mine": bool(address) and status.get("address_id") == str(address.id),
            }
        )
    return {
        "full_name": user.full_name or "",
        "email": user.email,
        "emergency_address": e911.public_address(address) if address else None,
        "numbers": covered,
        "notice": e911.LIMITATIONS_NOTICE,
    }


def set_name(user: User, full_name: str) -> None:
    name = (full_name or "").strip()
    if not name:
        raise ValidationFailedError("Enter your name")
    if len(name) > 255:
        raise ValidationFailedError("That name is too long")
    user.full_name = name


async def set_emergency_address(
    session: AsyncSession, settings, org_id: uuid.UUID, user: User, fields: dict  # noqa: ANN001
) -> dict:
    """Save the person's 911 address. With ``e911_carrier_push`` on, also validate +
    register it with the carrier and put it on every number assigned to them (a carrier
    problem on one number marks that number failed - the sweeper retries - and never blocks
    saving the address). With it off (the current default) the address is stored on our
    side only; pushing it to the carrier later is this same function with the flag on."""
    from app.services import e911

    if not getattr(settings, "e911_carrier_push", False):
        address = await e911.save_local_address(session, org_id, fields)
        address.user_id = user.id
        await session.flush()
        log.info("profile_emergency_address_saved", org_id=str(org_id), user_id=str(user.id))
        return {"applied": 0, "skipped": 0}

    address = await e911.create_address(session, settings, org_id, fields)
    address.user_id = user.id
    await session.flush()
    applied, skipped = 0, 0
    for number in await assigned_numbers(session, org_id, user.id):
        try:
            await e911.enable(session, settings, number, address)
            applied += 1
        except ValidationFailedError:
            skipped += 1  # a carrier whose 911 is managed elsewhere
    log.info(
        "profile_emergency_address_set",
        org_id=str(org_id), user_id=str(user.id), applied=applied, skipped=skipped,
    )
    return {"applied": applied, "skipped": skipped}
