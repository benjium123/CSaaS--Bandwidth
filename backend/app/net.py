"""P42: the ONE way to read a client's IP address.

``X-Forwarded-For`` is a list the caller controls, except for the entries our own reverse
proxies appended at the end. With TRUSTED_PROXY_COUNT proxies in front of the app (nginx = 1),
the real client is the entry that many positions from the right. Anything to its left was
supplied by the caller and is ignored.

Before P42 two different rules existed: the rate limiter took the rightmost entry (correct)
while login events, sessions and the per-org IP allowlist took the LEFTMOST - so a caller
could put any address first and walk straight past an IP allowlist.
"""

from __future__ import annotations

from fastapi import Request

from app.config import get_active_settings


def client_ip(request: Request) -> str | None:
    app = request.scope.get("app")
    settings = getattr(getattr(app, "state", None), "settings", None) if app else None
    trusted = (settings or get_active_settings()).trusted_proxy_count
    forwarded = request.headers.get("x-forwarded-for")
    if trusted > 0 and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            index = max(len(parts) - trusted, 0)
            return parts[index][:64]
    client = request.client
    if client is not None and client.host:
        return client.host[:64]
    return None
