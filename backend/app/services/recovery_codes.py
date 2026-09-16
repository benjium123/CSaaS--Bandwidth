"""P42 recovery codes: one-time codes that stand in for a lost authenticator or passkey.

Format ``xxxx-xxxx-xxxx`` from an unambiguous alphabet (no 0/O/1/l), 60 bits each. Shown to
the user exactly once; only SHA-256 hashes are stored. Generating a new set voids the old one.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RecoveryCode

ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def _normalize(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch in ALPHABET)


def _hash(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


async def generate(session: AsyncSession, user_id: uuid.UUID, count: int) -> list[str]:
    await session.execute(sa.delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    codes = [_new_code() for _ in range(count)]
    for code in codes:
        session.add(RecoveryCode(id=uuid.uuid4(), user_id=user_id, code_hash=_hash(code)))
    return codes


async def remaining(session: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count(RecoveryCode.id)).where(
                RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None)
            )
        )
    ).scalar_one()


async def consume(session: AsyncSession, user_id: uuid.UUID, code: str) -> bool:
    """Mark a matching unused code as used. Conditional UPDATE, so two concurrent attempts
    with the same code cannot both succeed."""
    if len(_normalize(code)) != 12:
        return False
    result = await session.execute(
        sa.update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash == _hash(code),
            RecoveryCode.used_at.is_(None),
        )
        .values(used_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1
