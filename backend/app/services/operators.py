"""P41 platform operators - named people who review businesses and suspend accounts.

Operators are created from the command line (``python -m scripts.make_operator``), never
through the API: there is no route that can turn a customer account into an operator.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError, ValidationFailedError
from app.models import OPERATOR_ROLES, PlatformOperator, User

ROLE_RANK = {"reviewer": 1, "admin": 2}

#: H2: what each operator role may do. Every role reads; each non-admin role acts in one area;
#: admin does everything, including the MAJOR actions (auth/deps.py
#: require_operator_permission(major=True)). reviewer and admin keep exactly what they had.
OPS_PERMISSIONS: tuple[str, ...] = (
    "ops:read",
    "ops:kyc",
    "ops:support",
    "ops:billing",
    "ops:site",
    "ops:admin",
)
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "read_only": frozenset({"ops:read"}),
    # ops:site = website chats and leads, answered by reviewers before H2 and by support now.
    "reviewer": frozenset({"ops:read", "ops:kyc", "ops:site"}),
    "support": frozenset({"ops:read", "ops:support", "ops:site"}),
    "billing": frozenset({"ops:read", "ops:billing"}),
    "admin": frozenset(OPS_PERMISSIONS),
}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def permissions_for(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, frozenset()))


async def get_active(session: AsyncSession, user_id: uuid.UUID) -> PlatformOperator | None:
    now = datetime.now(timezone.utc)
    return (
        await session.execute(
            sa.select(PlatformOperator).where(
                PlatformOperator.user_id == user_id,
                PlatformOperator.is_active.is_(True),
                # H4: break-glass access ends by itself.
                sa.or_(PlatformOperator.expires_at.is_(None), PlatformOperator.expires_at > now),
            )
        )
    ).scalar_one_or_none()


async def is_operator(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return await get_active(session, user_id) is not None


def role_satisfies(actual: str, required: str) -> bool:
    return ROLE_RANK.get(actual, 0) >= ROLE_RANK[required]


async def grant(session: AsyncSession, *, email: str, role: str) -> PlatformOperator:
    if role not in OPERATOR_ROLES:
        raise ValidationFailedError(f"Operator role must be one of {', '.join(OPERATOR_ROLES)}")
    user = (
        await session.execute(sa.select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No account with email {email}")
    row = (
        await session.execute(
            sa.select(PlatformOperator).where(PlatformOperator.user_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlatformOperator(id=uuid.uuid4(), user_id=user.id, role=role, is_active=True)
        session.add(row)
    else:
        row.role = role
        row.is_active = True
    row.expires_at = None  # a normal grant is permanent (break_glass sets an expiry)
    return row


BREAK_GLASS_MAX = timedelta(hours=1)


async def break_glass(
    session: AsyncSession, *, email: str, reason: str, minutes: int = 60
) -> PlatformOperator:
    """H4: temporary admin for a named, existing account (scripts/break_glass.py only).

    Refuses an account that is already an active operator (nothing to break into) and any
    reason shorter than 10 characters. Access ends by itself (get_active); the account
    still needs its own second factor to use the console. The caller records the audit
    row, raises the alert and commits."""
    reason = reason.strip()
    if len(reason) < 10:
        raise ValidationFailedError("Give a real reason (10+ characters)")
    minutes = max(1, min(int(minutes), int(BREAK_GLASS_MAX.total_seconds() // 60)))
    user = (
        await session.execute(sa.select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No account with email {email}")
    if await get_active(session, user.id) is not None:
        raise ValidationFailedError(f"{email} is already an active operator")
    row = (
        await session.execute(
            sa.select(PlatformOperator).where(PlatformOperator.user_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlatformOperator(id=uuid.uuid4(), user_id=user.id, role="admin", is_active=True)
        session.add(row)
    else:
        row.role = "admin"
        row.is_active = True
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return row


async def revoke(session: AsyncSession, *, email: str) -> None:
    user = (
        await session.execute(sa.select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No account with email {email}")
    row = await get_active(session, user.id)
    if row is not None:
        row.is_active = False
