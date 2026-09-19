"""P42 cookie sessions: opaque, revocable-on-the-spot session secrets in HttpOnly cookies.

Cookie value ``<session id>.<secret>``: the id finds the ``sessions`` row, the secret is
checked in constant time against ``sessions.token_hash`` (SHA-256). Nothing about the user
is in the cookie, it cannot be read by page scripts (HttpOnly), it is never sent cross-site
on unsafe requests (SameSite=Lax) and, in production, never over plain HTTP (Secure +
``__Host-`` prefix, which also pins it to this exact host).

CSRF: unsafe requests authenticated by the cookie must echo ``X-CSRF-Token``. The token is
an HMAC of the session id with SESSION_SECRET, delivered in a readable ``csaas_csrf`` cookie,
so it is bound to the session and needs no storage.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

from fastapi import Response

from app.config import Settings
from app.models import Session as IdentitySession

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-csrf-token"


def cookie_secure(settings: Settings) -> bool:
    if settings.session_cookie_secure is not None:
        return settings.session_cookie_secure
    return settings.is_production or settings.public_web_url.startswith("https://")


def session_cookie_name(settings: Settings) -> str:
    return "__Host-csaas_session" if cookie_secure(settings) else "csaas_session"


def csrf_cookie_name(settings: Settings) -> str:
    return "__Host-csaas_csrf" if cookie_secure(settings) else "csaas_csrf"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def csrf_token(settings: Settings, sid: uuid.UUID) -> str:
    key = settings.session_secret.get_secret_value().encode()
    return hmac.new(key, f"csrf:{sid}".encode(), hashlib.sha256).hexdigest()


def issue_secret(row: IdentitySession) -> str:
    """(Re)generate the session secret; returns the cookie value. Rotating invalidates the
    previous cookie immediately."""
    secret = secrets.token_urlsafe(32)
    row.token_hash = _hash(secret)
    return f"{row.id}.{secret}"


def parse(value: str | None) -> tuple[uuid.UUID, str] | None:
    if not value or "." not in value:
        return None
    sid_raw, _, secret = value.partition(".")
    try:
        sid = uuid.UUID(sid_raw)
    except ValueError:
        return None
    if len(secret) < 20:
        return None
    return sid, secret


def secret_matches(row: IdentitySession, secret: str) -> bool:
    return bool(row.token_hash) and hmac.compare_digest(row.token_hash, _hash(secret))


def set_cookies(response: Response, settings: Settings, row: IdentitySession, value: str) -> None:
    secure = cookie_secure(settings)
    max_age = settings.session_max_hours * 3600
    response.set_cookie(
        session_cookie_name(settings),
        value,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        csrf_cookie_name(settings),
        csrf_token(settings, row.id),
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_cookies(response: Response, settings: Settings) -> None:
    secure = cookie_secure(settings)
    for name, http_only in (
        (session_cookie_name(settings), True),
        (csrf_cookie_name(settings), False),
    ):
        response.delete_cookie(name, path="/", secure=secure, httponly=http_only, samesite="lax")


def rotate(response: Response, settings: Settings, row: IdentitySession) -> None:
    """New secret for an existing session after re-authentication, so a cookie captured
    before the step-up stops working. Bearer-only rows are left alone."""
    if row.token_hash is None:
        return
    set_cookies(response, settings, row, issue_secret(row))
