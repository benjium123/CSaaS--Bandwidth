"""P42: platform operators must sign in with a passkey.

Customer owners, admins and billing staff are NOT held to this: they choose their second
factor (email code, authenticator app or passkey), per the product owner. Only the operator
console (auth/deps.py require_operator) calls ``enforce``, always with ``org=None``. The
rules below are what apply there; the org-role wording is historical.

Passkeys cannot be phished: they only answer for our real domain. An authenticator code can
be typed into a fake login page, which is exactly how privileged accounts get taken over.

Rules
  - "Privileged" = a role holding ``*``, ``members:update`` or ``org:billing`` in the
    workspace being used, or an active platform operator.
  - The session must have been established (or stepped up) with a passkey. A workspace that
    sets ``trust_idp_mfa`` also accepts its SSO sessions (the identity provider is then
    responsible for phishing-resistant MFA).
  - Grace: the first time an account is seen as privileged, ``users.passkey_required_since``
    is stamped; for PASSKEY_GRACE_DAYS after that, other sign-in methods still work so people
    can register a passkey without being locked out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import PermissionDeniedError
from app.models import Org, Role, User
from app.models import Session as IdentitySession

PRIVILEGED_PERMISSIONS = frozenset({"*", "members:update", "org:billing"})
#: Paths a privileged person without a passkey session can still use: sign-in, passkey
#: registration and step-up, and their own sessions.
EXEMPT_PREFIXES = ("/api/v1/auth/", "/api/v1/me/sessions", "/api/v1/me/login-events")


def is_privileged_role(role: Role | None) -> bool:
    return role is not None and bool(set(role.permissions or []) & PRIVILEGED_PERMISSIONS)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def grace_until(settings: Settings, user: User) -> datetime | None:
    since = _aware(user.passkey_required_since)
    return since + timedelta(days=settings.passkey_grace_days) if since else None


def session_satisfies(row: IdentitySession | None, org: Org | None) -> bool:
    if row is None:
        return False
    if row.auth_method == "passkey":
        return True
    # P43: only THIS workspace's identity provider can vouch for MFA in this workspace.
    return (
        row.auth_method == "sso"
        and org is not None
        and bool(org.trust_idp_mfa)
        and getattr(row, "org_id", None) == org.id
    )


async def enforce(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    org: Org | None,
    privileged: bool,
) -> None:
    if not settings.require_passkey_for_privileged or not privileged:
        return
    if any(request.url.path.startswith(p) for p in EXEMPT_PREFIXES):
        return
    now = datetime.now(timezone.utc)
    if user.passkey_required_since is None:
        user.passkey_required_since = now
        await session.commit()
    sid = getattr(request.state, "session_id", None)
    row = await session.get(IdentitySession, sid) if sid is not None else None
    if session_satisfies(row, org):
        return
    until = grace_until(settings, user)
    if until is not None and now < until:
        return
    raise PermissionDeniedError(
        "Owners, admins and billing staff must sign in with a passkey."
        if user.has_passkey
        else "Add a passkey to keep using admin and billing features.",
        code="passkey_required",
    )
