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


def _client_ip(request: Request) -> str:
    """C3: under `--forwarded-allow-ips "*"` (Dockerfile, unchanged here),
    ``request.client.host`` is whatever Starlette/Uvicorn parsed from
    X-Forwarded-For - the LEFTMOST entry - and that header is entirely
    caller-controlled, so trusting it as the rate-limit key lets any client rotate a
    fake leftmost value to dodge the limiter. nginx appends the real client address as
    the RIGHTMOST entry; prefer that when the header is present, falling back to
    request.client.host only when there is no X-Forwarded-For at all.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(request: Request, identifier: str) -> None:
    settings: Settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return

    ip = _client_ip(request)
    base = f"{request.method}:{request.url.path}"
    keys = (f"{base}:ip:{ip}", f"{base}:identifier:{identifier}")

    retry_after = 0.0
    for key in keys:
        retry_after = _limiter.allow(
            key, settings.rate_limit_max_requests, settings.rate_limit_window_seconds
        )
        if retry_after:
            break

    if retry_after:
        # C7: the app's standard error type, so 429 uses the same {"error": {...}}
        # envelope every other error already does, instead of a bare HTTPException
        # {"detail": ...} body. Retry-After is preserved (see main.py's CsaasError
        # handler) via the exception's own retry_after attribute.
        raise RateLimitExceededError(retry_after=int(retry_after))
