"""Tracked short-link creation, click recording, and aggregation helpers.

The public redirect route has no org in its path, so short-link codes must be
globally unique and unguessable. We only shorten safe http/https URLs and
re-check safety at redirect time because an old row could have been written by
an older, looser version of this code.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import string
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ValidationFailedError
from app.models.links import LinkClick, ShortLink

URL_RE = re.compile(
    r"""https?://[^\s<>'"]+(?<![.,!?;:)\]}"'])"""
)

CODE_ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 10


def generate_code(rng: Any | None = None) -> str:
    """Return a cryptographically random, URL-safe short-link code.

    ``secrets.choice`` is the default source. ``rng`` can be any object with a
    ``choice`` method for test callers that need determinism.
    """
    pick = rng.choice if rng is not None else secrets.choice
    return "".join(pick(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def is_safe_target(url: str) -> bool:
    """Return True only for an absolute http/https URL with no userinfo.

    This is the open-redirect / SSRF guard. It is deliberately simple: a URL
    containing userinfo (``@`` before the host) can be interpreted as a
    different destination by a browser, so it must never be stored or
    redirected to.
    """
    if any(ch.isspace() for ch in url):
        return False
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return False

    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not hostname:
        return False
    if "@" in parsed.netloc:
        return False
    return True


def daily_salt(secret: str, moment: datetime | None = None) -> str:
    """Return the configured secret plus the UTC calendar date.

    Rotating the salt daily makes it impossible to correlate a stored ip_hash
    across days, while the raw client IP itself is never kept.
    """
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return f"{secret}{moment.date().isoformat()}"


def hash_ip(
    ip: str | None,
    secret: str,
    moment: datetime | None = None,
) -> str | None:
    """Return a sha256 hex digest of ``ip`` plus the daily salt.

    Returns None when there is no IP, so nothing sensitive is ever written.
    """
    if not ip:
        return None
    value = f"{ip}{daily_salt(secret, moment)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def record_click(
    session: AsyncSession,
    short_link: ShortLink,
    *,
    ip: str | None,
    user_agent: str | None,
    secret: str,
    now: datetime | None = None,
) -> None:
    """Insert one ``LinkClick`` and atomically increment the short link's count.

    The org_id is copied from the ``ShortLink`` row itself. We never use a
    request-supplied org here because the redirect route is unauthenticated and
    has no org in its path. The increment is a single SQL UPDATE expression so
    concurrent clicks cannot lose counts.
    """
    set_org_context(session, short_link.org_id)

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    click = LinkClick(
        short_link_id=short_link.id,
        at=now,
        ip_hash=hash_ip(ip, secret, now),
        user_agent=user_agent[:255] if user_agent else None,
        org_id=short_link.org_id,
    )
    session.add(click)

    await session.execute(
        sa.update(ShortLink)
        .where(ShortLink.id == short_link.id)
        .values(clicks=ShortLink.clicks + 1)
    )
    await session.commit()


async def create_tracked_links(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    body: str,
    message_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    public_web_url: str,
) -> tuple[str, list[ShortLink]]:
    """Replace safe http(s) URLs in ``body`` with tracked short links.

    Identical URLs appearing more than once share one ``ShortLink`` row. The
    rows are added to the session but not committed; the caller owns the
    transaction so links commit atomically with the message row.
    """
    set_org_context(session, org_id)

    base_url = public_web_url.rstrip("/")
    by_target: dict[str, ShortLink] = {}
    new_links: list[ShortLink] = []
    used_codes: set[str] = set()

    async def _make_link(target: str) -> ShortLink:
        for _ in range(5):
            code = generate_code()
            if code in used_codes:
                continue
            # Code is globally unique; checking it must be cross-org because
            # the public redirect route resolves by code alone.
            result = await session.execute(
                sa.select(ShortLink.id)
                .where(ShortLink.code == code)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
            if result.first() is not None:
                continue
            used_codes.add(code)
            return ShortLink(
                code=code,
                target_url=target,
                message_id=message_id,
                contact_id=contact_id,
                org_id=org_id,
                clicks=0,
            )
        raise ValidationFailedError("Could not create a unique link code. Please try again.")

    pieces: list[str] = []
    last = 0
    for match in URL_RE.finditer(body):
        target = match.group(0)
        if not is_safe_target(target) or len(target) > 2048:
            continue

        link = by_target.get(target)
        if link is None:
            link = await _make_link(target)
            by_target[target] = link
            new_links.append(link)
            session.add(link)

        pieces.append(body[last : match.start()])
        pieces.append(f"{base_url}/l/{link.code}")
        last = match.end()

    pieces.append(body[last:])
    return "".join(pieces), new_links


async def links_for_messages(
    session: AsyncSession,
    message_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[ShortLink]]:
    """Return a mapping of message ID to its short links in a single query."""
    if not message_ids:
        return {}

    result = await session.execute(
        sa.select(ShortLink).where(ShortLink.message_id.in_(message_ids))
    )

    grouped: dict[uuid.UUID, list[ShortLink]] = {}
    for link in result.scalars():
        if link.message_id is not None:
            grouped.setdefault(link.message_id, []).append(link)
    return grouped


async def clicks_for_messages(
    session: AsyncSession,
    message_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Return total clicks per message in one grouped query.

    The query groups by a mapped column rather than using a bare count() so
    the tenant guard applies correctly.
    """
    if not message_ids:
        return {}

    result = await session.execute(
        sa.select(
            ShortLink.message_id,
            sa.func.coalesce(sa.func.sum(ShortLink.clicks), 0).label("total_clicks"),
        )
        .where(ShortLink.message_id.in_(message_ids))
        .group_by(ShortLink.message_id)
    )

    return {
        message_id: int(total_clicks)
        for message_id, total_clicks in result.all()
        if message_id is not None
    }
