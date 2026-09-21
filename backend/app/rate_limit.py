from __future__ import annotations

import time
from collections import OrderedDict, deque
from math import ceil
from threading import Lock

from fastapi import Request

from app.config import Settings
from app.errors import RateLimitExceededError

#: C5: an unbounded dict of per-key deques (keyed by ip/identifier) grows forever under
#: real traffic - every new IP or scoped identifier mints a brand-new entry that never
#: gets removed once its window elapses, if nothing ever touches that exact key again.
_MAX_KEYS = 50_000


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = Lock()

    def allow(self, key: str, max_requests: int, window_seconds: int) -> float:
        now = time.monotonic()
        with self._lock:
            q = self._events.get(key)
            if q is None:
                q = deque()
                self._events[key] = q
            # LRU order: the most recently touched key moves to the end, so eviction
            # below (popitem(last=False)) always drops the least-recently-touched one.
            self._events.move_to_end(key)

            cutoff = now - window_seconds
            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= max_requests:
                retry_after = ceil(window_seconds - (now - q[0]))
                self._sweep()
                return max(1.0, retry_after)

            q.append(now)
            self._sweep()
            return 0.0

    def _sweep(self) -> None:
        """C5: drop empty deques (a key nothing has hit since its window fully
        elapsed) and cap total key count via LRU eviction of the oldest-touched
        bucket. Only runs once the table has actually grown past the cap, so the
        common case (a small, active key set) never pays for a full-dict scan.
        Caller already holds ``self._lock``.
        """
        if len(self._events) <= _MAX_KEYS:
            return
        for k in [k for k, dq in self._events.items() if not dq]:
            del self._events[k]
        while len(self._events) > _MAX_KEYS:
            self._events.popitem(last=False)


_limiter = SlidingWindowLimiter()

#: Paths that share a single IP ceiling with another route. The admin login endpoint is the
#: same credential check as the public one, so it must not be a second, independent ceiling:
#: an attacker who can spend the public login budget can otherwise spend the admin one too,
#: doubling the attempts available against the same account. Canonicalizing the path here
#: (rather than at the call site) keeps the policy in one place and means the admin route
#: needs no line of its own.
_CANONICAL_PATHS = {
    "/api/v1/auth/admin/login": "/api/v1/auth/login",
}


def _canonical_path(path: str) -> str:
    """Map a request path to the path whose ceiling it shares. Unknown paths are unchanged."""
    return _CANONICAL_PATHS.get(path, path)


#: Routes whose IP ceiling is NOT the global default, resolved by exact path.
#:
#: Deliberately a table here rather than an argument at the call site. `/auth/register` is
#: currently being edited by three sessions at once (a business-email check, org creation on
#: signup, and this), and a change that needs no line in that function cannot collide with
#: theirs. It also keeps the policy in one readable place instead of spread across call sites,
#: so "what are our ceilings" is one file rather than a grep.
#:
#: The value is a tuple of (max_requests, window_seconds) buckets, ALL of which are checked.
#: Two windows stop different attacks: the short one stops a script, the long one stops a
#: patient script. See the config comment on `registration_ip_burst_max` for why these numbers.
def _route_ip_buckets(settings: Settings, path: str) -> tuple[tuple[int, int], ...]:
    if _canonical_path(path) == "/api/v1/auth/register":
        return (
            (settings.registration_ip_burst_max, settings.registration_ip_burst_seconds),
            (settings.registration_ip_hourly_max, settings.registration_ip_hourly_seconds),
        )
    return ()


def _client_ip(request: Request) -> str:
    """C3/P42: one trusted-proxy-aware rule for every IP decision (app/net.py)."""
    from app.net import client_ip

    return client_ip(request) or "unknown"


_REDIS_WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return math.ceil(window - (now - tonumber(oldest[2])))
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return 0
"""


async def _redis_allow(
    settings: Settings, key: str, max_requests: int, window: int
) -> float | None:
    """P42: the same sliding window, shared by every worker through Redis. Returns None when
    Redis is not configured or fails, so the caller falls back to the in-process limiter.

    READ THIS BEFORE RAISING THE WORKER COUNT. That fallback is per PROCESS, so with N workers
    every ceiling in this module silently becomes N times looser - a 5-per-minute registration
    limit becomes 5 per minute per worker. Nothing reports it, in either direction: a limiter
    with no shared state simply ALLOWS MORE, so no request fails, no log line appears, and no
    test goes red. The deployed image ships `--workers 1`, which is why the multiplier is 1
    today and why the first person to change that number will not notice they weakened every
    limit on the public front door.

    The protection is elsewhere: `Settings._validate` refuses to boot in production without
    REDIS_URL. This comment exists anyway because whoever raises the worker count will be
    reading this file, not that one - and because "required in production" is not the same as
    "present right now", which is the gap a staging box with one worker and no Redis lives in.
    """
    from app.services.session_cache import _redis_client

    client = _redis_client(settings)
    if client is None:
        return None
    try:
        import uuid as _uuid

        result = await client.eval(
            _REDIS_WINDOW_SCRIPT,
            1,
            f"rl:{key}",
            f"{time.time():.6f}",
            str(window),
            str(max_requests),
            _uuid.uuid4().hex,
        )
        return max(1.0, float(result)) if result else 0.0
    except Exception:  # noqa: BLE001 - a Redis outage must not take login down
        return None


async def enforce_rate_limit(request: Request, identifier: str) -> None:
    """Rate-limit this request by client IP and by ``identifier``, tripping on whichever binds.

    BOTH keys are checked, which matters for any route where the caller chooses the identifier:
    at `/auth/register` the identifier is an email address the attacker picks, so the identifier
    key alone would never bind. The IP key is the one doing the work there.

    A route may override the IP ceiling (see ``_route_ip_buckets``). The identifier key keeps the
    global default everywhere: it exists to stop repeated attempts against ONE account or
    address, which is a different question from how many accounts one machine may create.

    The bucket base is built from the CANONICAL path (see ``_CANONICAL_PATHS``), so routes that
    are the same operation under two URLs share one ceiling instead of each getting their own.
    The identifier is supplied by the caller and never read from a request header, so a client
    cannot choose its own bucket.

    WHAT THIS CANNOT DO. Every bucket resolves through Redis and falls back to an in-process
    limiter when Redis is unreachable, so a 5-per-minute ceiling silently becomes 5 per minute
    PER WORKER. The tighter the ceiling the more that fallback matters, which is why open
    registration in production requires ``REDIS_URL`` (see Settings._validate) rather than
    trusting that a shared limiter happens to be configured.
    """
    settings: Settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return

    ip = _client_ip(request)
    canonical_path = _canonical_path(request.url.path)
    base = f"{request.method}:{canonical_path}"

    default = (settings.rate_limit_max_requests, settings.rate_limit_window_seconds)
    buckets: list[tuple[str, int, int]] = []
    route_buckets = _route_ip_buckets(settings, request.url.path)
    if route_buckets:
        # The window is part of the key so two ceilings on one route cannot share a counter -
        # without it the 5/60 and 20/3600 buckets would both consume the same sliding window and
        # whichever ran first would decide both answers.
        buckets += [(f"{base}:ip:{ip}:{window}", mx, window) for mx, window in route_buckets]
    else:
        # Key shape UNCHANGED for every other route, so no existing counter resets on deploy.
        buckets.append((f"{base}:ip:{ip}", *default))
    buckets.append((f"{base}:identifier:{identifier}", *default))

    retry_after = 0.0
    for key, max_requests, window_seconds in buckets:
        shared = await _redis_allow(settings, key, max_requests, window_seconds)
        retry_after = (
            shared
            if shared is not None
            else _limiter.allow(key, max_requests, window_seconds)
        )
        if retry_after:
            break

    if retry_after:
        # C7: the app's standard error type, so 429 uses the same {"error": {...}}
        # envelope every other error already does, instead of a bare HTTPException
        # {"detail": ...} body. Retry-After is preserved (see main.py's CsaasError
        # handler) via the exception's own retry_after attribute.
        raise RateLimitExceededError(retry_after=int(retry_after))
