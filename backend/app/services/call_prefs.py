"""Per-member calling preferences: Do Not Disturb, and an internal forward target.

A member on DND is never rung (call.ring fan-out, simultaneous ring groups) and is skipped
in sequential ring groups - unless they chose a teammate to forward to, in which case a
ring addressed to them rings that teammate instead. Forwarding never leaves the
workspace: the target must be a member who may place calls (checked when it is set).
Stored on ``org_memberships.call_prefs``.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ValidationFailedError
from app.models import WILDCARD, OrgMembership, Role

log = structlog.get_logger("call_prefs")

#: The softphone event gate asks once per ring per connection; a short cache keeps that
#: to one query per org per few seconds. Setting a preference clears it for that org.
_CACHE_TTL_SECONDS = 5.0
_cache: dict[uuid.UUID, tuple[float, dict[str, str | None]]] = {}


def _parse_until(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        return None
    return until if until.tzinfo else until.replace(tzinfo=timezone.utc)


def _clean(prefs: dict | None, now: datetime | None = None) -> dict:
    """DND with an end time (``dnd_until``) switches itself off once that time passes;
    no end time means until the member turns it off."""
    prefs = prefs or {}
    until = _parse_until(prefs.get("dnd_until"))
    dnd = bool(prefs.get("dnd"))
    if dnd and until is not None and (now or datetime.now(timezone.utc)) >= until:
        dnd = False
    return {
        "dnd": dnd,
        "dnd_until": until.isoformat() if dnd and until is not None else None,
        "forward_to": prefs.get("forward_to") or None,
    }


async def dnd_users(session: AsyncSession, org_id: uuid.UUID) -> dict[str, str | None]:
    """{user id: forward target user id or None} for every member on DND in the org."""
    rows = (
        await session.execute(
            sa.select(OrgMembership.user_id, OrgMembership.call_prefs)
            .where(OrgMembership.org_id == org_id, OrgMembership.call_prefs.is_not(None))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    out: dict[str, str | None] = {}
    for user_id, prefs in rows:
        clean = _clean(prefs)
        if clean["dnd"]:
            out[str(user_id)] = clean["forward_to"]
    return out


async def dnd_users_cached(org_id: uuid.UUID) -> dict[str, str | None]:
    """dnd_users with its own short-lived session and a few seconds of caching."""
    from app.db.session import get_sessionmaker

    hit = _cache.get(org_id)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    try:
        async with get_sessionmaker()() as session:
            value = await dnd_users(session, org_id)
    except Exception:  # noqa: BLE001 - fail OPEN: a lookup error must never silence rings
        log.warning("call_prefs_dnd_lookup_failed", org_id=str(org_id), exc_info=True)
        return {}
    _cache[org_id] = (now, value)
    return value


def ring_visible(
    dnd: dict[str, str | None], user_id: uuid.UUID, ring_user_ids: list | None
) -> bool | None:
    """DND/forwarding verdict for one call.ring and one person.

    False = must not ring (on DND). True = rings because a DND teammate the ring is
    addressed to forwards to this person. None = no opinion (normal access rules apply).
    """
    me = str(user_id)
    if me in dnd:
        return False
    if ring_user_ids and any(dnd.get(str(u)) == me for u in ring_user_ids):
        return True
    return None


async def teammates(session: AsyncSession, org_id: uuid.UUID, *, exclude: uuid.UUID) -> list[dict]:
    """Members who may place calls - the only valid forward targets."""
    from app.models import User

    rows = (
        await session.execute(
            sa.select(User, Role)
            .select_from(OrgMembership)
            .join(User, User.id == OrgMembership.user_id)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.org_id == org_id)
        )
    ).all()
    out = []
    for user, role in rows:
        if user.id == exclude:
            continue
        perms = role.permissions or []
        if "calls:place" in perms or WILDCARD in perms:
            out.append({"user_id": str(user.id), "name": user.full_name or user.email})
    out.sort(key=lambda t: t["name"].casefold())
    return out


def get(membership: OrgMembership | None) -> dict:
    return _clean(membership.call_prefs if membership is not None else None)


async def set_prefs(
    session: AsyncSession,
    membership: OrgMembership,
    *,
    dnd: bool,
    forward_to: uuid.UUID | None,
    dnd_until: datetime | None = None,
) -> dict:
    """Validate and store. Does not commit."""
    if dnd_until is not None:
        if dnd_until.tzinfo is None:
            dnd_until = dnd_until.replace(tzinfo=timezone.utc)
        if dnd_until <= datetime.now(timezone.utc):
            raise ValidationFailedError("Do not disturb must end in the future")
    if forward_to is not None:
        if forward_to == membership.user_id:
            raise ValidationFailedError("You cannot forward calls to yourself")
        allowed = {
            t["user_id"]
            for t in await teammates(session, membership.org_id, exclude=membership.user_id)
        }
        if str(forward_to) not in allowed:
            raise ValidationFailedError("Calls can only be forwarded to a teammate who takes calls")
    prefs = {
        "dnd": bool(dnd),
        "dnd_until": dnd_until.isoformat() if dnd and dnd_until is not None else None,
        "forward_to": str(forward_to) if forward_to else None,
    }
    membership.call_prefs = prefs
    _cache.pop(membership.org_id, None)
    return _clean(prefs)
