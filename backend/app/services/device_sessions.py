"""Ringlite apps P1: device sessions for the Android and desktop apps.

A device session is a `sessions` row with a non-web ``device_kind``. It carries:
- a HARD expiry (created + ``device_session_days``) that nothing ever extends;
- a single-use refresh secret (only its SHA-256 is stored), rotated on every refresh. A refresh
  secret presented twice means it leaked: the whole session is revoked;
- short Bearer access JWTs whose ``exp`` never passes the session expiry.

Revocation is the existing one (``revoked_at`` + session_cache), so removing, disabling or
password-resetting a member signs their devices out on the next API call.

Link codes: a signed-in web user creates a one-time code (shown as a QR). The app redeems it
for a device session of that user. Single use, short TTL, only stored hashed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import ALGORITHM
from app.config import Settings
from app.errors import ConflictError, UnauthenticatedError, ValidationFailedError
from app.models import DeviceLinkCode, Org, OrgMembership, User
from app.models import Session as IdentitySession
from app.services import identity as identity_svc
from app.services import session_cache

DEVICE_KINDS: frozenset[str] = frozenset({"android", "desktop", "ios"})
REFRESH_PREFIX = "rt1"
#: A refresh token replayed this soon after it was rotated is a race, not theft.
REFRESH_REPLAY_GRACE = timedelta(seconds=30)


@dataclass(frozen=True)
class DeviceInfo:
    kind: str
    name: str | None = None
    os: str | None = None
    app_version: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in DEVICE_KINDS:
            raise ValidationFailedError("Unknown device kind")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def apply_device(row: IdentitySession, settings: Settings, device: DeviceInfo) -> None:
    """Turn a freshly created (uncommitted) session row into a device session."""
    row.device_kind = device.kind
    row.device_name = (device.name or "")[:120] or None
    row.device_os = (device.os or "")[:60] or None
    row.app_version = (device.app_version or "")[:40] or None
    row.expires_at = _aware(row.last_seen_at or datetime.now(timezone.utc)) + timedelta(
        days=settings.device_session_days
    )
    row.token_hash = None  # never usable as a browser cookie
    row.refresh_generation = 0


def _new_refresh(row: IdentitySession) -> str:
    secret = secrets.token_urlsafe(32)
    row.refresh_hash = _hash(secret)
    return f"{REFRESH_PREFIX}.{row.id}.{secret}"


def _access_token(row: IdentitySession, settings: Settings) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    exp = min(now + timedelta(minutes=settings.device_access_minutes), _aware(row.expires_at))
    payload = {
        "sub": str(row.user_id),
        "sid": str(row.id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM), exp


def token_pair(row: IdentitySession, user: User, settings: Settings, refresh_token: str) -> dict:
    access, access_exp = _access_token(row, settings)
    return {
        "access_token": access,
        "token_type": "bearer",
        "access_expires_at": access_exp.isoformat(),
        "refresh_token": refresh_token,
        "session_expires_at": _aware(row.expires_at).isoformat(),
        "session_id": str(row.id),
        "user": {"id": str(user.id), "email": user.email, "name": getattr(user, "full_name", None)},
    }


def issue_refresh(row: IdentitySession) -> str:
    """Set the first refresh secret on a new device row (caller commits)."""
    return _new_refresh(row)


def _parse_refresh(token: str) -> tuple[uuid.UUID, str] | None:
    parts = (token or "").split(".", 2)
    if len(parts) != 3 or parts[0] != REFRESH_PREFIX or not parts[2]:
        return None
    try:
        return uuid.UUID(parts[1]), parts[2]
    except ValueError:
        return None


async def refresh(session: AsyncSession, settings: Settings, token: str) -> dict:
    """Rotate a refresh token. Never extends ``expires_at``. Reuse revokes the session."""
    invalid = UnauthenticatedError(
        "Your session has ended. Sign in again.", code="refresh_invalid"
    )
    parsed = _parse_refresh(token)
    if parsed is None:
        raise invalid
    sid, secret = parsed
    row = await session.get(IdentitySession, sid)
    if row is None or not row.is_device or row.revoked_at is not None:
        raise invalid
    now = datetime.now(timezone.utc)
    if _aware(row.expires_at) <= now:
        raise invalid
    # A workspace's maximum session age applies to devices too (auth/deps enforces it per
    # request); refusing the refresh here stops an app looping on refresh -> 401.
    max_hours = await _strictest_org_max_hours(session, row.user_id)
    if max_hours and now - _aware(row.created_at) > timedelta(hours=max_hours):
        await revoke(session, settings, row, revoked_by=row.user_id)
        raise invalid

    presented = _hash(secret)
    new_secret = secrets.token_urlsafe(32)
    # Compare-and-swap on the stored hash: two concurrent refreshes with the same token
    # cannot both win, and a stale (already rotated) token matches nothing.
    result = await session.execute(
        sa.update(IdentitySession)
        .where(
            IdentitySession.id == sid,
            IdentitySession.refresh_hash == presented,
            IdentitySession.revoked_at.is_(None),
        )
        .values(
            refresh_hash=_hash(new_secret),
            prev_refresh_hash=presented,
            refresh_rotated_at=now,
            refresh_generation=(row.refresh_generation or 0) + 1,
            last_seen_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        await session.refresh(row)
        if row.prev_refresh_hash is not None and presented == row.prev_refresh_hash:
            rotated = _aware(row.refresh_rotated_at) if row.refresh_rotated_at else None
            if rotated is not None and now - rotated <= REFRESH_REPLAY_GRACE:
                # Two refreshes raced (two windows, a retried request): the other one won.
                # The client re-reads its stored tokens; nothing is revoked.
                raise ConflictError(
                    "Your session was just refreshed. Try again.", code="refresh_in_progress"
                )
            # An already-rotated secret used later: it leaked. End the session.
            owner = await session.get(User, row.user_id)
            identity_svc.record_login_event(
                session,
                email=owner.email if owner else "",
                outcome="revoked",
                user_id=row.user_id,
                org_id=row.org_id,
                detail="refresh_reuse",
            )
            await revoke(session, settings, row, revoked_by=row.user_id)
        # An unknown secret proves nothing about the session (a sid is readable from any
        # access token), so it is refused without revoking anything.
        raise invalid
    await session.commit()
    await session.refresh(row)
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid
    return token_pair(row, user, settings, f"{REFRESH_PREFIX}.{row.id}.{new_secret}")


async def _strictest_org_max_hours(session: AsyncSession, user_id: uuid.UUID) -> int | None:
    rows = (
        await session.execute(
            sa.select(Org.session_max_hours)
            .join(OrgMembership, OrgMembership.org_id == Org.id)
            .where(OrgMembership.user_id == user_id, Org.session_max_hours.is_not(None))
            .execution_options(allow_unscoped=True)
        )
    ).scalars().all()
    values = [int(v) for v in rows if v]
    return min(values) if values else None


async def revoke(
    session: AsyncSession, settings: Settings, row: IdentitySession, *, revoked_by: uuid.UUID
) -> None:
    await identity_svc.revoke_session(session, row, revoked_by=revoked_by)
    await session.commit()
    await session_cache.mark_revoked(settings, row.id)


# --- link codes ---------------------------------------------------------------------------


async def create_link_code(
    session: AsyncSession, settings: Settings, web_row: IdentitySession
) -> tuple[str, datetime]:
    code = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.device_link_code_ttl_seconds)
    session.add(
        DeviceLinkCode(
            id=uuid.uuid4(),
            code_hash=_hash(code),
            user_id=web_row.user_id,
            org_id=web_row.org_id,
            created_by_sid=web_row.id,
            expires_at=expires,
        )
    )
    await session.commit()
    return code, expires


async def redeem_link_code(
    session: AsyncSession,
    settings: Settings,
    request: Request,
    code: str,
    device: DeviceInfo,
) -> dict:
    invalid = ValidationFailedError(
        "This code has expired or was already used. Show a new code.", code="link_code_invalid"
    )
    now = datetime.now(timezone.utc)
    code_hash = _hash(code or "")
    # Claim atomically (single use) before checking anything else.
    claimed = await session.execute(
        sa.update(DeviceLinkCode)
        .where(DeviceLinkCode.code_hash == code_hash, DeviceLinkCode.used_at.is_(None))
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise invalid
    link = (
        await session.execute(sa.select(DeviceLinkCode).where(DeviceLinkCode.code_hash == code_hash))
    ).scalar_one()
    await session.commit()  # the code is spent even if the rest fails
    if _aware(link.expires_at) <= now:
        raise invalid

    creator = await identity_svc.get_live_session(session, link.created_by_sid) if link.created_by_sid else None
    user = await session.get(User, link.user_id)
    if creator is None or user is None or not user.is_active:
        raise invalid

    row = await identity_svc.create_session(
        session, user_id=user.id, org_id=link.org_id, request=request, expire_hours=1
    )
    apply_device(row, settings, device)
    row.auth_method = "device_link"
    # The device inherits that the user HAS proved a factor, never a fresh proof: step-up
    # actions (password change, API keys...) still need a new 2FA from the device itself.
    if creator.second_factor_at is not None:
        stale = now - timedelta(minutes=settings.step_up_2fa_minutes, seconds=1)
        row.second_factor_at = min(_aware(creator.second_factor_at), stale)
    refresh_token = issue_refresh(row)
    identity_svc.record_login_event(
        session, email=user.email, outcome="ok", user_id=user.id, org_id=link.org_id,
        request=request, detail="device_link",
    )
    await session.commit()
    from app.services import account_security

    what = device.name or device.kind
    system = device.os or "unknown system"
    await account_security.notify_now(
        settings,
        user.email,
        "New device linked",
        f"A device was linked to your account with a QR code: {what} ({system}). "
        "You can sign it out from Settings > Security.",
    )
    return token_pair(row, user, settings, refresh_token)
