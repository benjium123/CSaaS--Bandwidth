"""P41 platform operators - named people who review businesses and suspend accounts.

Operators are created from the command line (``python -m scripts.make_operator``), never
through the API: there is no route that can turn a customer account into an operator.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError, ValidationFailedError
from app.models import OPERATOR_ROLES, PlatformOperator, User

ROLE_RANK = {"reviewer": 1, "admin": 2}


async def get_active(session: AsyncSession, user_id: uuid.UUID) -> PlatformOperator | None:
    return (
        await session.execute(
            sa.select(PlatformOperator).where(
                PlatformOperator.user_id == user_id, PlatformOperator.is_active.is_(True)
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
