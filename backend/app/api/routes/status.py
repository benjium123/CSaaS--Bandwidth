"""Public status surface (P14 DR-8).

``GET /status`` is unauthenticated on purpose - uptime monitors and the marketing/console
shell need it before a user has logged in. That is exactly why its response is minimal:
component NAMES and ``up`` / ``degraded`` / ``down`` / ``unconfigured`` ONLY. No version, no
counts, no hostnames, no carrier account detail - the operator-facing detail (which carrier,
how many consecutive failures, capability flags) stays where it already lives, authenticated,
at ``GET /api/v1/routing/carriers``.

Every probe here is cheap by construction (``SELECT 1``, a raw TCP PING, a bare HTTP GET,
all on short timeouts) and the WHOLE computed response is cached in-process for
``CACHE_SECONDS`` so this endpoint cannot be used to hammer the database, redis, or LiveKit -
the cache lives on ``app.state`` (not a module global) so it never leaks between separate
app instances, e.g. across tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import urlsplit

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Request

from app.db.session import get_sessionmaker

log = structlog.get_logger("status")

router = APIRouter(tags=["status"])

#: In-process cache lifetime for the whole computed response.
CACHE_SECONDS = 15.0
#: Every individual probe below is bounded by this so one slow dependency cannot make
#: /status itself slow.
_PROBE_TIMEOUT = 1.5

_BREAKER_STATE_TO_STATUS = {"closed": "up", "half_open": "degraded", "open": "down"}


async def _probe_db() -> str:
    try:
        async with get_sessionmaker()() as session:
            await asyncio.wait_for(session.execute(sa.text("SELECT 1")), timeout=2.0)
        return "up"
    except Exception:
        return "down"


async def _probe_redis(settings) -> str:  # noqa: ANN001 - Settings
    """Can THIS APP use Redis - not just "is something listening on that port".

    The old probe was a raw TCP PING, and its docstring said "nothing in this app talks to
    redis today". Both stopped being true: session revocation (`services/session_cache.py`),
    rate limits (`rate_limit.py`), OIDC login state and SAML assertion replay protection
    (`services/oidc.py`, `services/saml.py`) all go through the Python client now, and every
    one of them catches its own exceptions and falls back to a PER-PROCESS store. So a wrong
    password, or an image built without the `redis` package, degrades the platform to
    per-process state silently - while production boot validation (REDIS_URL is set), the
    running container, and this endpoint all report healthy. Three green signals and none of
    the behaviour.

    Worse in the old form: it counted a `-NOAUTH` reply as "up" on the grounds that any RESP
    reply proves the server is speaking. That is exactly the failure this needs to catch.

    So: issue a real PING through the app's own client. "degraded" means the server answered
    the socket but this app cannot use it - the state that used to read as "up".
    """
    if not settings.redis_url:
        return "unconfigured"
    from app.services.session_cache import _redis_client

    client = _redis_client(settings)
    if client is not None:
        try:
            await asyncio.wait_for(client.ping(), timeout=_PROBE_TIMEOUT)
            return "up"
        except Exception:
            # Authenticated PING failed. Separate "the server is gone" from "the server is
            # there and we cannot use it", because the fixes are different ones.
            return "degraded" if await _redis_reachable(settings.redis_url) else "down"
    # No client library in this image. The URL is configured and the server may be perfectly
    # healthy; nothing in this app can reach it.
    return "degraded" if await _redis_reachable(settings.redis_url) else "down"


async def _redis_reachable(redis_url: str) -> bool:
    """Raw TCP PING: is a redis-speaking server on the other end at all? No auth, no client
    library - this exists to tell a dead server apart from an unusable one.
    """
    try:
        parts = urlsplit(redis_url)
        host = parts.hostname or "localhost"
        port = parts.port or 6379
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_PROBE_TIMEOUT
        )
        try:
            writer.write(b"PING\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.readline(), timeout=_PROBE_TIMEOUT)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        # ANY RESP reply means a redis is on the other end: "+PONG" and a "-NOAUTH ..." /
        # "-ERR ..." both prove the server is there and speaking. A connection
        # failure/timeout (caught below) or a dead socket (empty read = EOF) does not.
        return data[:1] in (b"+", b"-")
    except Exception:
        return False


async def _probe_media_plane(livekit) -> str:  # noqa: ANN001 - LiveKitApi | None
    if livekit is None:
        return "unconfigured"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            # Any response (even a 404) means the media plane is reachable - this is a
            # liveness probe, not an authenticated capability check.
            await client.get(livekit.http_url + "/")
        return "up"
    except Exception:
        return "down"


def _carrier_states(registry) -> dict[str, str]:  # noqa: ANN001 - CarrierRegistry | None
    if registry is None:
        return {}
    snapshot = registry.health.snapshot()
    return {
        name: _BREAKER_STATE_TO_STATUS.get(snapshot.get(name, {}).get("state", "closed"), "up")
        for name in registry.names()
    }


def _overall(db: str, redis_state: str, carriers: dict[str, str], media_plane: str) -> str:
    if db == "down":
        # The database is not one dependency among many - nothing in the platform works
        # without it, so this is the only condition strong enough to mean "down" overall.
        return "down"
    others = [redis_state, media_plane, *carriers.values()]
    if any(v in ("down", "degraded") for v in others):
        return "degraded"
    return "ok"


def _lock(app) -> asyncio.Lock:  # noqa: ANN001 - FastAPI app
    lock = getattr(app.state, "_status_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state._status_lock = lock
    return lock


@router.get("/status")
async def status(request: Request) -> dict:
    app = request.app
    now = time.monotonic()
    cache = getattr(app.state, "_status_cache", None)
    if cache is not None and cache["expires_at"] > now:
        return cache["body"]

    # Single-flight (Opus review): a cache MISS is exactly when a burst of concurrent
    # requests would otherwise all fire their own full probe set at once. The lock makes
    # every request but one WAIT for the in-flight probe instead - re-checking the cache
    # once inside means a waiter that lost the race gets the fresh result the winner just
    # computed, not a second redundant probe.
    async with _lock(app):
        now = time.monotonic()
        cache = getattr(app.state, "_status_cache", None)
        if cache is not None and cache["expires_at"] > now:
            return cache["body"]

        try:
            settings = app.state.settings
            registry = getattr(app.state, "carriers", None)
            livekit = getattr(app.state, "livekit", None)

            db_state = await _probe_db()
            redis_state = await _probe_redis(settings)
            media_plane_state = await _probe_media_plane(livekit)
            carriers = _carrier_states(registry)

            body = {
                "status": _overall(db_state, redis_state, carriers, media_plane_state),
                "components": {
                    "api": "up",  # this code ran - the api is serving requests by definition
                    "db": db_state,
                    "redis": redis_state,
                    "carriers": carriers,
                    "media_plane": media_plane_state,
                },
            }
        except Exception:
            # A bug in computing the NEW status must not take the status page itself
            # down - serve the last-known-good body if one exists, rather than a 500.
            log.exception("status_probe_failed")
            if cache is not None:
                return cache["body"]
            raise

        app.state._status_cache = {"expires_at": now + CACHE_SECONDS, "body": body}
        return body
