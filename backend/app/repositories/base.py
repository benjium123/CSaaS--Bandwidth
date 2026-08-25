from __future__ import annotations

import uuid
from typing import Generic, TypeVar

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import TenantScoped, set_org_context

T = TypeVar("T")


class TenantRepository(Generic[T]):
    """Ergonomic access to tenant-scoped models.

    This is the convenience layer. It is NOT the safety mechanism — the session listeners
    in ``app.db.base`` are. Constructing the repository binds the org to the session, so
    even a caller who bypasses these helpers and writes raw ORM queries still gets filtered.
    """

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id
        set_org_context(session, org_id)

    async def get(self, model: type[T], obj_id: uuid.UUID) -> T | None:
        return await self.session.get(model, obj_id)

    async def list(self, model: type[T], *where) -> list[T]:
        stmt = sa.select(model)
        if where:
            stmt = stmt.where(*where)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def add(self, obj: T) -> T:
        if isinstance(obj, TenantScoped) and getattr(obj, "org_id", None) is None:
            obj.org_id = self.org_id
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def delete(self, obj: T) -> None:
        await self.session.delete(obj)
        await self.session.flush()
