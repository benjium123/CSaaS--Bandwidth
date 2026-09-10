"""60-second check for whether an access-token session has been revoked.

The auth dependency needs to reject a revoked token without paying a Postgres round-trip on
every request. This module puts a tiny TTL cache in front of that lookup. It defaults to an
in-process LRU cache so the app works with no extra dependency. Redis is used only when
``settings.redis_url`` is configured AND the optional ``redis`` package is installed.

Redis is deliberately failure-tolerant: every Redis exception is logged as a warning and
treated as a cache miss / no-op write. A cache-outage must never lock users out of the app.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from threading import Lock
from typing import Any

import structlog

from app.config import Settings

TTL_SECONDS = 60
_MAX_KEYS = 50_000
logger = structlog.get_logger("session_cache")


class _MemoryCache:
    """In-process TTL cache with a 50k-key LRU cap.

    The eviction discipline mirrors ``app/rate_limit.py``: an OrderedDict keeps
    least-recently-touched keys at the front, and ``popitem(last=False)`` evicts them.
    """

    def __init__(self) -> None:
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                del self._store[key]
                return None
            # LRU order: the most recently touched key moves to the end, so eviction
            # below (popitem(last=False)) always drops the least-recently-touched one.
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._store[key] = (now + ttl, value)
            self._store.move_to_end(key)
            self._sweep_locked(now)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _sweep_locked(self, now: float) -> None:
        while self._store:
            _key, (expires_at, _value) = next(iter(self._store.items()))
            if expires_at <= now or len(self._store) > _MAX_KEYS:
                self._store.popitem(last=False)
            else:
                break


_memory_cache = _MemoryCache()

_resolved_redis: tuple[str, Any] | None = None
_resolved_lock = Lock()


def _redis_client(settings: Settings) -> Any | None:
    """Return a lazily-created redis.asyncio client, or None when Redis is unavailable.

    The import and connection construction happen inside this function on purpose: nothing
    in the app imports Redis until it is actually configured, so the module stays
    importable in environments without the optional dependency.
    """
    global _resolved_redis

    url = settings.redis_url
    if not url:
        return None

    if _resolved_redis is not None and _resolved_redis[0] == url:
        return _resolved_redis[1]

    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return None

    with _resolved_lock:
        if _resolved_redis is not None and _resolved_redis[0] == url:
            return _resolved_redis[1]
        # decode_responses keeps the same "1"/"0" string contract as the in-process cache.
        client = redis_asyncio.from_url(url, decode_responses=True)
        _resolved_redis = (url, client)

    return _resolved_redis[1]


def _key(sid: uuid.UUID) -> str:
    return f"sess:{sid}"


async def _write(settings: Settings, key: str, value: str) -> None:
    """One write path for both backends. A failed write is a no-op, never an exception:
    the worst case is a stale cache entry that expires within TTL_SECONDS."""
    try:
        client = _redis_client(settings)
        if client is None:
            _memory_cache.set(key, value, TTL_SECONDS)
        else:
            await client.set(key, value, ex=TTL_SECONDS)
    except Exception as exc:
        logger.warning("session_cache_unavailable", error=type(exc).__name__)


async def is_revoked(settings: Settings, sid: uuid.UUID) -> bool | None:
    """Return True/False when cached, or None when the DB should be consulted."""
    key = _key(sid)
    # EVERY backend interaction - including resolving the client itself, which can raise
    # on a malformed REDIS_URL - is inside the try. A cache outage must degrade to "ask
    # the DB", never to a 500 on every authenticated request.
    try:
        client = _redis_client(settings)
        raw = _memory_cache.get(key) if client is None else await client.get(key)
    except Exception as exc:
        logger.warning("session_cache_unavailable", error=type(exc).__name__)
        return None

    if raw is None:
        return None
    if raw == "1":
        return True
    if raw == "0":
        return False
    return None


async def remember(settings: Settings, sid: uuid.UUID, revoked: bool) -> None:
    await _write(settings, _key(sid), "1" if revoked else "0")


async def mark_revoked(settings: Settings, sid: uuid.UUID) -> None:
    """Write the revoked bit immediately so a revoke takes effect NOW, not after TTL."""
    await _write(settings, _key(sid), "1")


def reset_memory_cache() -> None:
    """Clear the in-process cache. Tests call this between cases."""
    _memory_cache.clear()
