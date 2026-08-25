from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(database_url: str, *, echo: bool = False, **kwargs) -> AsyncEngine:
    opts: dict = {"echo": echo, "future": True}
    if database_url.startswith("sqlite"):
        # SQLite has no pool sizing and needs same-thread checks relaxed for asyncio.
        opts["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url:
            # Every connection to ":memory:" would otherwise get its OWN empty database,
            # so the schema created in a fixture would be invisible to the request under
            # test. StaticPool pins the whole engine to one connection.
            opts["poolclass"] = StaticPool
    else:
        opts.update(kwargs)
    return create_async_engine(database_url, **opts)


def init_engine(database_url: str, *, echo: bool = False, **kwargs) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = make_engine(database_url, echo=echo, **kwargs)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialised; call init_engine() during app startup.")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Sessionmaker not initialised; call init_engine() during startup.")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Yields a session with NO org context bound.

    Binding the org is ``get_current_org``'s job — until it runs, any tenant-scoped query
    raises MissingTenantContextError, which is the entire point.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        finally:
            await session.close()
