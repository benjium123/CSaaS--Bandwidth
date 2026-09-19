"""P42 password policy (NIST SP 800-63B style): length over complexity, no email-derived
passwords, and never a password already published in a data breach.

The breach check uses Have I Been Pwned's k-anonymity range API: only the first five hex
characters of the password's SHA-1 leave this server, never the password or its full hash.
If the API cannot be reached the check is skipped with a warning - a third-party outage must
not stop people from resetting passwords.
"""

from __future__ import annotations

import hashlib

import httpx
import structlog

from app.config import Settings
from app.errors import ValidationFailedError

log = structlog.get_logger(__name__)

MAX_LENGTH = 128
COMMON_WORDS = frozenset(
    {"password", "passw0rd", "123456789012", "qwertyuiopas", "letmein", "welcome", "csaas"}
)


def _weak(message: str) -> ValidationFailedError:
    return ValidationFailedError(message, code="weak_password")


async def breach_count(
    settings: Settings, password: str, client: httpx.AsyncClient | None = None
) -> int | None:
    """How many times the password appears in known breaches; None when unknown."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 - HIBP protocol
    prefix, suffix = digest[:5], digest[5:]
    owns = client is None
    client = client or httpx.AsyncClient(timeout=5.0)
    try:
        resp = await client.get(
            f"{settings.hibp_api_url.rstrip('/')}/{prefix}",
            headers={"Add-Padding": "true", "User-Agent": f"{settings.app_name}-password-check"},
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        log.warning("hibp_unreachable")
        return None
    finally:
        if owns:
            await client.aclose()
    for line in resp.text.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip().upper() == suffix:
            try:
                return int(count.strip())
            except ValueError:
                return 1
    return 0


async def check(
    settings: Settings,
    password: str,
    *,
    email: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    if len(password) < settings.password_min_length:
        raise _weak(f"Use at least {settings.password_min_length} characters")
    if len(password) > MAX_LENGTH:
        raise _weak(f"Use at most {MAX_LENGTH} characters")
    lowered = password.lower()
    if len(set(lowered)) < 4:
        raise _weak("That password is too repetitive")
    if lowered in COMMON_WORDS:
        raise _weak("That password is too common")
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and local in lowered:
            raise _weak("Your password cannot contain your email address")
    if settings.hibp_enabled:
        count = await breach_count(settings, password, client)
        if count:
            raise _weak(
                "This password has appeared in a data breach and is not safe to use. "
                "Choose a different one."
            )
