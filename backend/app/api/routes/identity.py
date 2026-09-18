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
from app.db.base import set_org_context
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
    #: P42: "oidc" (default) or "saml".
    protocol: str = "oidc"
    idp_entity_id: str = ""
    idp_sso_url: str = ""
    idp_cert_set: bool = False
    #: P43 (audit): the pinned IdP certificate is NOT validated at sign-in (that would lock
    #: the workspace out), so its expiry is surfaced here and as a security alert instead.
    idp_cert_expires_at: datetime | None = None
    idp_cert_expired: bool = False
    group_roles: dict[str, str] = {}


def _cert_expiry(value) -> datetime | None:  # noqa: ANN001
    from app.services import saml as saml_svc

    return saml_svc.certificate_valid_until(str(value)) if value else None


def _cert_expired(value) -> bool:  # noqa: ANN001
    from app.services import saml as saml_svc

    return saml_svc.certificate_expired(str(value)) if value else False


class SecurityPolicyOut(BaseModel):
    require_2fa: bool
    require_2fa_grace_until: datetime | None
    ip_allowlist: list[str] | None
    sso: SsoOut | None
    #: P42: stricter session timeouts for this workspace (None = platform default).
    session_idle_minutes: int | None = None
    session_max_hours: int | None = None
    trust_idp_mfa: bool = False


class SsoIn(BaseModel):
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    domain: str | None = None
    enforce: bool = False
    default_role_id: uuid.UUID | None = None
    protocol: str | None = None
    idp_entity_id: str | None = None
    idp_sso_url: str | None = None
    idp_x509_cert: str | None = None
    #: IdP group name -> workspace role name, applied when SSO adds someone new.
    group_roles: dict[str, str] | None = None


class SecurityPolicyIn(BaseModel):
    require_2fa: bool | None = None
    ip_allowlist: list[str] | None = None
    sso: SsoIn | None = None
    session_idle_minutes: int | None = None
    session_max_hours: int | None = None
    trust_idp_mfa: bool | None = None


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
        protocol=str(sso.get("protocol") or "oidc"),
        idp_entity_id=str(sso.get("idp_entity_id") or ""),
        idp_sso_url=str(sso.get("idp_sso_url") or ""),
        idp_cert_set=bool(sso.get("idp_x509_cert")),
        idp_cert_expires_at=_cert_expiry(sso.get("idp_x509_cert")),
        idp_cert_expired=_cert_expired(sso.get("idp_x509_cert")),
        group_roles={
            str(k): str(v) for k, v in (sso.get("group_roles") or {}).items()
        }
        if isinstance(sso.get("group_roles"), dict)
        else {},
    )


def _security_policy_out(org: Org) -> SecurityPolicyOut:
    return SecurityPolicyOut(
        require_2fa=org.require_2fa,
        require_2fa_grace_until=org.require_2fa_grace_until,
        ip_allowlist=org.ip_allowlist,
        sso=_sso_out(org),
        session_idle_minutes=org.session_idle_minutes,
        session_max_hours=org.session_max_hours,
        trust_idp_mfa=org.trust_idp_mfa,
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

    # P42: password / passkey sign-ins happen before a workspace is chosen, so they carry
    # no org_id - an admin must still see their members' sign-ins (and failures).
    members = sa.select(OrgMembership.user_id).where(OrgMembership.org_id == ctx.org.id)
    # LoginEvent, Session and SecurityAlert all carry an org_id WITHOUT being TenantScoped,
    # so db/base.py's automatic filter never applies to them and this WHERE is the ONLY org
    # boundary. The membership clause must therefore be restricted to events that have no
    # org of their own: org_id IS NULL is exactly "signed in before a workspace was chosen"
    # (every complete_login caller omits org_id except the SSO one). Without that guard an
    # admin here also sees a shared member's sign-ins to OTHER workspaces - ip, user agent
    # and risk flags - in JSON and in bulk through ?format=csv.
    stmt = (
        sa.select(LoginEvent)
        .where(
            sa.or_(
                LoginEvent.org_id == ctx.org.id,
                sa.and_(LoginEvent.org_id.is_(None), LoginEvent.user_id.in_(members)),
            )
        )
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


async def _require_owner_step_up(request: Request, ctx: OrgContext) -> None:
    from app.auth.deps import check_org_selfie_step_up, check_step_up
    from app.errors import PermissionDeniedError

    if ctx.api_key is not None or ctx.actor_user_id is None:
        raise PermissionDeniedError(
            "Single sign-on settings can only be changed by the owner, signed in"
        )
    if "*" not in (ctx.role.permissions or []):
        raise PermissionDeniedError("Only the workspace owner can change single sign-on")
    actor = await ctx.session.get(User, ctx.actor_user_id)
    await check_step_up(request, ctx.session, actor, kind="recent_2fa", action="sso_change")
    await check_org_selfie_step_up(request, ctx, action="admin_grant")
    set_org_context(ctx.session, ctx.org.id)


async def _require_non_owner_role(ctx: OrgContext, role_id: object) -> None:
    from app.models import Role

    try:
        rid = uuid.UUID(str(role_id))
    except ValueError as exc:
        raise ValidationFailedError("Default role not found") from exc
    role = (
        await ctx.session.execute(sa.select(Role).where(Role.id == rid))
    ).scalar_one_or_none()
    if role is None:
        raise ValidationFailedError("Default role not found")
    if "*" in (role.permissions or []):
        raise ValidationFailedError(
            "Single sign-on cannot make new people owners", code="sso_owner_role"
        )


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

    # P43: whoever controls single sign-on can sign in AS any member on the domain, owner
    # included. Only the owner may change it - in person (no API key), with a fresh second
    # factor and, once businesses are verified, their ID step-up.
    if "sso" in updates or updates.get("trust_idp_mfa") is not None:
        await _require_owner_step_up(request, ctx)

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
            if actor is None or not actor.has_second_factor:
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

    settings = request.app.state.settings
    for field, platform_value, lowest in (
        ("session_idle_minutes", settings.session_idle_minutes, 5),
        ("session_max_hours", settings.session_max_hours, 1),
    ):
        if field not in updates:
            continue
        value = updates[field]
        if value is not None and not lowest <= value <= platform_value:
            raise ValidationFailedError(
                f"{field.replace('_', ' ')} must be between {lowest} and the platform "
                f"maximum of {platform_value}"
            )
        setattr(ctx.org, field, value)
        changed_fields.append(field)

    if updates.get("trust_idp_mfa") is not None:
        ctx.org.trust_idp_mfa = bool(updates["trust_idp_mfa"])
        changed_fields.append("trust_idp_mfa")

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

            for field in (
                "issuer",
                "client_id",
                "domain",
                "enforce",
                "default_role_id",
                "protocol",
                "idp_entity_id",
                "idp_sso_url",
            ):
                if field in sso_update and sso_update[field] is not None:
                    value = sso_update[field]
                    # default_role_id arrives as a uuid.UUID, which the JSON column
                    # cannot serialise - store the canonical string form.
                    merged[field] = str(value) if isinstance(value, uuid.UUID) else value

            if merged.get("default_role_id"):
                await _require_non_owner_role(ctx, merged["default_role_id"])

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

            # P42 SAML settings.
            protocol = str(merged.get("protocol") or "oidc")
            if protocol not in ("oidc", "saml"):
                raise ValidationFailedError("SSO protocol must be oidc or saml")
            merged["protocol"] = protocol
            idp_sso_url = merged.get("idp_sso_url")
            if idp_sso_url and not str(idp_sso_url).startswith("https://"):
                raise ValidationFailedError("SAML sign-in URL must start with https://")
            if sso_update.get("idp_x509_cert"):
                from app.services import saml as saml_svc

                # Refused here, where the admin can paste a fresh one - never at sign-in,
                # which would lock the workspace out (services/saml.py explains the split).
                saml_svc.check_certificate_usable(sso_update["idp_x509_cert"])
                merged["idp_x509_cert"] = saml_svc.certificate_pem(sso_update["idp_x509_cert"])
            if "group_roles" in sso_update and sso_update["group_roles"] is not None:
                merged["group_roles"] = {
                    str(k).strip()[:255]: str(v).strip()[:64]
                    for k, v in sso_update["group_roles"].items()
                    if str(k).strip() and str(v).strip()
                }

            if protocol == "saml":
                from app.services import saml as saml_svc

                complete = saml_svc.is_configured(merged)
            else:
                complete = bool(
                    merged.get("issuer")
                    and merged.get("client_id")
                    and merged.get("domain")
                    and merged.get("client_secret_encrypted")
                )
            if merged.get("enforce") and not complete:
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
