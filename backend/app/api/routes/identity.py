"""P25 sessions, login history and org security policy.

``Session`` and ``LoginEvent`` are NOT TenantScoped, so every query in this module
scopes explicitly by user_id and/or org_id. The model is imported as
``IdentitySession`` to avoid colliding with SQLAlchemy's AsyncSession.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, get_current_user, require_permission
from app.db.session import get_session
from app.errors import NotFoundError, ValidationFailedError
from app.models import LoginEvent, Org, OrgMembership, User
from app.models import Session as IdentitySession
from app.services import audit as audit_svc
from app.services import credentials as credentials_svc
from app.services import identity as identity_svc
from app.services import session_cache

REQUIRE_2FA_GRACE_DAYS = 7

me_router = APIRouter(prefix="/api/v1/me", tags=["identity"])
org_router = APIRouter(prefix="/api/v1/orgs/current", tags=["identity"])


class SessionOut(BaseModel):
    id: uuid.UUID
    ip: str | None
    user_agent: str | None
    last_seen_at: datetime | None
    created_at: datetime
    expires_at: datetime
    current: bool = False


class LoginEventOut(BaseModel):
    id: uuid.UUID
    at: datetime
    ip: str | None
    user_agent: str | None
    outcome: str
    detail: str | None
    email: str
    user_id: uuid.UUID | None


class SsoOut(BaseModel):
    issuer: str
    client_id: str
    domain: str
    enforce: bool
    default_role_id: uuid.UUID | None
    client_secret_set: bool


class SecurityPolicyOut(BaseModel):
    require_2fa: bool
    require_2fa_grace_until: datetime | None
    ip_allowlist: list[str] | None
    sso: SsoOut | None


class SsoIn(BaseModel):
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    domain: str | None = None
    enforce: bool = False
    default_role_id: uuid.UUID | None = None


class SecurityPolicyIn(BaseModel):
    require_2fa: bool | None = None
    ip_allowlist: list[str] | None = None
    sso: SsoIn | None = None


def _request_session_id(request: Request) -> uuid.UUID | None:
    sid = getattr(request.state, "session_id", None)
    if sid is None:
        return None
    if isinstance(sid, uuid.UUID):
        return sid
    try:
        return uuid.UUID(str(sid))
    except (ValueError, AttributeError, TypeError):
        # A malformed value here means "no current session", never a 500.
        return None


def _login_event_out(row: LoginEvent) -> LoginEventOut:
    return LoginEventOut(
        id=row.id,
        at=row.at,
        ip=row.ip,
        user_agent=row.user_agent,
        outcome=row.outcome,
        detail=row.detail,
        email=row.email,
        user_id=row.user_id,
    )


def _sso_out(org: Org) -> SsoOut | None:
    if not org.sso:
        return None
    sso = org.sso
    default_role_id = sso.get("default_role_id")
    if default_role_id is not None and not isinstance(default_role_id, uuid.UUID):
        try:
            default_role_id = uuid.UUID(str(default_role_id))
        except (ValueError, AttributeError, TypeError):
            # Hand-edited JSON must not turn a settings GET into a 500.
            default_role_id = None
    return SsoOut(
        issuer=str(sso.get("issuer") or ""),
        client_id=str(sso.get("client_id") or ""),
        domain=str(sso.get("domain") or ""),
        enforce=bool(sso.get("enforce", False)),
        default_role_id=default_role_id,
        client_secret_set=bool(sso.get("client_secret_encrypted")),
    )


def _security_policy_out(org: Org) -> SecurityPolicyOut:
    return SecurityPolicyOut(
        require_2fa=org.require_2fa,
        require_2fa_grace_until=org.require_2fa_grace_until,
        ip_allowlist=org.ip_allowlist,
        sso=_sso_out(org),
    )


@me_router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SessionOut]:
    rows = await identity_svc.list_sessions_for_user(session, user.id)
    current_sid = _request_session_id(request)
    now = datetime.now(timezone.utc)
    out: list[SessionOut] = []
    current_row: IdentitySession | None = None

    for row in rows:
        is_current = current_sid is not None and row.id == current_sid
        if is_current:
            row.last_seen_at = now
            current_row = row
        out.append(
            SessionOut(
                id=row.id,
                ip=row.ip,
                user_agent=row.user_agent,
                last_seen_at=row.last_seen_at,
                created_at=row.created_at,
                expires_at=row.expires_at,
                current=is_current,
            )
        )

    if current_row is not None:
        await session.commit()

    return out


@me_router.delete("/sessions/{sid}", status_code=204)
async def revoke_session(
    sid: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    row = await session.get(IdentitySession, sid)
    # Do not leak which session ids exist: a foreign session is indistinguishable
    # from a missing one for the caller.
    if row is None or row.user_id != user.id:
        raise NotFoundError("Session not found")

    await identity_svc.revoke_session(session, row, revoked_by=user.id)
    await session.commit()
    await session_cache.mark_revoked(request.app.state.settings, sid)


@me_router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    keep_sid = _request_session_id(request)
    revoked_ids = await identity_svc.revoke_all_for_user(
        session, user.id, revoked_by=user.id, keep_sid=keep_sid
    )
    await session.commit()
    for sid in revoked_ids:
        await session_cache.mark_revoked(request.app.state.settings, sid)
    return {"revoked": len(revoked_ids)}


@me_router.get("/login-events", response_model=list[LoginEventOut])
async def list_login_events(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[LoginEventOut]:
    stmt = (
        sa.select(LoginEvent)
        .where(LoginEvent.user_id == user.id)
        .order_by(LoginEvent.at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_login_event_out(row) for row in rows]


@org_router.delete("/members/{user_id}/sessions")
async def revoke_member_sessions(
    user_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("members:update"))],
    request: Request,
) -> dict[str, int]:
    # Sessions are not tenant-scoped, so the membership check below is the explicit
    # org boundary: an admin of org A must not be able to revoke a stranger's sessions.
    membership_stmt = sa.select(OrgMembership).where(
        OrgMembership.user_id == user_id,
        OrgMembership.org_id == ctx.org.id,
    )
    membership = (await ctx.session.execute(membership_stmt)).scalars().first()
    if membership is None:
        raise NotFoundError("Member not found")

    revoked_ids = await identity_svc.revoke_all_for_user(
        ctx.session,
        user_id,
        revoked_by=ctx.actor_user_id,
        keep_sid=None,
    )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="member.sessions_revoked",
        target_type="user",
        target_id=str(user_id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"revoked": len(revoked_ids)},
    )
    await ctx.session.commit()

    for sid in revoked_ids:
        await session_cache.mark_revoked(request.app.state.settings, sid)

    return {"revoked": len(revoked_ids)}


# response_model=None is REQUIRED: the union return annotation (rows or a CSV Response)
# is not a model FastAPI can build, and without this the app fails at import time.
@org_router.get("/login-events", response_model=None)
async def list_org_login_events(
    ctx: Annotated[OrgContext, Depends(require_permission("members:read"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    format_: Annotated[str, Query(alias="format")] = "json",
) -> list[LoginEventOut] | Response:
    if format_ not in ("json", "csv"):
        raise ValidationFailedError("format must be 'json' or 'csv'")

    stmt = (
        sa.select(LoginEvent)
        .where(LoginEvent.org_id == ctx.org.id)
        .order_by(LoginEvent.at.desc())
        .limit(limit)
    )
    rows = (await ctx.session.execute(stmt)).scalars().all()

    if format_ == "csv":
        fieldnames = ["at", "email", "outcome", "ip", "user_agent", "detail"]
        records = [
            {
                "at": row.at,
                "email": row.email,
                "outcome": row.outcome,
                "ip": row.ip,
                "user_agent": row.user_agent,
                "detail": row.detail or "",
            }
            for row in rows
        ]
        return identity_svc.csv_response(records, fieldnames, "login-events.csv")

    return [_login_event_out(row) for row in rows]


@org_router.get("/security", response_model=SecurityPolicyOut)
async def get_security_policy(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> SecurityPolicyOut:
    return _security_policy_out(ctx.org)


@org_router.patch("/security", response_model=SecurityPolicyOut)
async def update_security_policy(
    payload: SecurityPolicyIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    request: Request,
) -> SecurityPolicyOut:
    settings = request.app.state.settings
    updates = payload.model_dump(exclude_unset=True)
    changed_fields: list[str] = []

    if "require_2fa" in updates:
        require_2fa = updates["require_2fa"]
        if require_2fa is None:
            raise ValidationFailedError("require_2fa must be true or false")
        # No-lockout guard, the twin of the ip_allowlist one below: whoever turns the
        # policy ON must already hold a second factor. Otherwise the grace period expires
        # and NOBODY can reach /orgs/current/security to turn it back off - the policy
        # would have eaten its own off switch. Requiring it here means enforcement needs
        # no escape hatch, which is what keeps the enforcement itself honest.
        if require_2fa and not ctx.org.require_2fa:
            actor = (
                await ctx.session.get(User, ctx.actor_user_id)
                if ctx.actor_user_id is not None
                else None
            )
            if actor is None or not actor.totp_enabled:
                raise ValidationFailedError(
                    "Set up two-factor authentication on your own account before "
                    "requiring it for everyone",
                    code="two_factor_required_for_actor",
                )

        if require_2fa and ctx.org.require_2fa_grace_until is None:
            ctx.org.require_2fa_grace_until = datetime.now(timezone.utc) + timedelta(
                days=REQUIRE_2FA_GRACE_DAYS
            )
        # Disabling clears neither the flag's grace deadline nor the deadline itself.
        # Re-enabling must not hand out a fresh grace period.
        ctx.org.require_2fa = bool(require_2fa)
        changed_fields.append("require_2fa")

    if "ip_allowlist" in updates:
        raw = updates["ip_allowlist"]
        new_ip_allowlist = None if raw is None else identity_svc.parse_cidrs(raw)

        # No-lockout guard: if the new list is non-empty, the caller's current IP
        # must remain inside it or the org could immediately lock itself out.
        if new_ip_allowlist and not identity_svc.ip_in_allowlist(
            identity_svc.client_ip(request), new_ip_allowlist
        ):
            raise ValidationFailedError(
                "Your current IP address is outside the ranges you are saving",
                code="ip_allowlist_would_lock_you_out",
            )

        # JSON columns are mutated in place; assign a NEW list so SQLAlchemy sees the change.
        ctx.org.ip_allowlist = new_ip_allowlist
        changed_fields.append("ip_allowlist")

    if "sso" in updates:
        if payload.sso is None:
            ctx.org.sso = None
            changed_fields.append("sso")
        else:
            sso_update = payload.sso.model_dump(exclude_unset=True)
            current_sso = dict(ctx.org.sso) if ctx.org.sso else {}
            merged: dict = dict(current_sso)

            for field in ("issuer", "client_id", "domain", "enforce", "default_role_id"):
                if field in sso_update and sso_update[field] is not None:
                    value = sso_update[field]
                    # default_role_id arrives as a uuid.UUID, which the JSON column
                    # cannot serialise - store the canonical string form.
                    merged[field] = str(value) if isinstance(value, uuid.UUID) else value

            if "client_secret" in sso_update:
                secret = sso_update["client_secret"]
                if secret:  # present and non-empty
                    merged["client_secret_encrypted"] = credentials_svc.encrypt(
                        settings, {"client_secret": secret}
                    )
                # Absent or empty keeps the previous ciphertext so a PATCH that edits
                # issuer/client_id must not wipe the secret. Never store plaintext.

            domain = merged.get("domain")
            if domain is not None:
                domain = str(domain).lower()
                if domain.startswith("@"):
                    domain = domain[1:]
                if not domain or any(ch.isspace() for ch in domain) or "/" in domain:
                    raise ValidationFailedError("SSO domain must be a bare domain")
                merged["domain"] = domain

            issuer = merged.get("issuer")
            if issuer is not None and not str(issuer).startswith("https://"):
                raise ValidationFailedError("SSO issuer must start with https://")

            if merged.get("enforce") and not (
                merged.get("issuer")
                and merged.get("client_id")
                and merged.get("domain")
                and merged.get("client_secret_encrypted")
            ):
                raise ValidationFailedError(
                    "Complete the single sign-on setup before enforcing it"
                )

            # Assign a NEW dict so SQLAlchemy sees the JSON column change.
            ctx.org.sso = merged
            changed_fields.append("sso")

    if changed_fields:
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            action="org.security_updated",
            target_type="org",
            target_id=str(ctx.org.id),
            actor_user_id=ctx.actor_user_id,
            actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
            detail={"fields": changed_fields},
        )
        await ctx.session.commit()

    return _security_policy_out(ctx.org)
