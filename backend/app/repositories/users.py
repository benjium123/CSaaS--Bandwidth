from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.errors import ConflictError
from app.models import User


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(sa.select(User).where(User.email == normalize_email(email)))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def create_user(
    session: AsyncSession, *, email: str, password: str, full_name: str = ""
) -> User:
    email = normalize_email(email)
    if await get_by_email(session, email) is not None:
        raise ConflictError("An account with that email already exists")
    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name.strip(),
    )
    session.add(user)
    await session.flush()
    return user
