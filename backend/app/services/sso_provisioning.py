"""P42: one completion path for every single sign-on protocol (OIDC, SAML), plus verified
email domains.

After the identity provider has vouched for an email address, this module decides whether
that person may enter the workspace and finishes the sign-in through
``services/login_flow.py`` - so SSO sign-ins get the same risk checks, device memory,
alerts and cookie session as every other sign-in.

Linking rules
  - The email must be on the workspace's SSO domain.
  - When SSO_REQUIRE_VERIFIED_DOMAIN is on (production), the domain must be DNS-VERIFIED
    for this workspace before its identity provider can sign ANYONE in. An SSO session is a
    session for the whole account (every workspace the person belongs to), so a workspace
    that merely typed ``bank.com`` into its settings must never be able to vouch for
    ``someone@bank.com`` - not even one it invited.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
import structlog
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, PermissionDeniedError, ValidationFailedError
from app.models import Org, OrgDomain, OrgMembership, Role, SecurityAlert, User
from app.models.rbac import is_privileged_permissions
from app.services import audit as audit_svc
from app.services import identity as identity_svc
from app.services import login_flow

TXT_PREFIX = "_csaas-verify"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_domain(value: str) -> str:
    domain = (value or "").strip().lower().rstrip(".")
    if domain.startswith("@"):
        domain = domain[1:]
    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or any(not label or len(label) > 63 for label in labels)
        or not all(ch.isalnum() or ch in "-." for ch in domain)
    ):
        raise ValidationFailedError("Enter a domain like example.com")
    return domain


def txt_record(domain: OrgDomain) -> dict:
    return {
        "name": f"{TXT_PREFIX}.{domain.domain}",
        "type": "TXT",
        "value": f"csaas-verify={domain.verify_token}",
    }


async def add_domain(
    session: AsyncSession, org_id: uuid.UUID, value: str, *, created_by: uuid.UUID | None
) -> OrgDomain:
    domain = normalize_domain(value)
    existing = (
        await session.execute(
            sa.select(OrgDomain).where(OrgDomain.org_id == org_id, OrgDomain.domain == domain)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = OrgDomain(
        id=uuid.uuid4(),
        org_id=org_id,
        domain=domain,
        verify_token=secrets.token_hex(24),
        created_by=created_by,
    )
    session.add(row)
    return row


async def lookup_txt(
    settings: Settings, name: str, client: httpx.AsyncClient | None = None
) -> list[str]:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(
            settings.dns_over_https_url,
            params={"name": name, "type": "TXT"},
            headers={"accept": "application/dns-json"},
        )
        resp.raise_for_status()
        answers = resp.json().get("Answer") or []
    except (httpx.HTTPError, ValueError):
        return []
    finally:
        if owns:
            await client.aclose()
    return [str(a.get("data", "")).strip().strip('"') for a in answers if a.get("type") == 16]


async def verify_domain(
    session: AsyncSession,
    settings: Settings,
    row: OrgDomain,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    row.last_checked_at = _now()
    # JUSTIFIED allow_unscoped: a domain may be verified by only ONE workspace platform-wide.
    taken = (
        await session.execute(
            sa.select(OrgDomain.id)
            .where(
                OrgDomain.domain == row.domain,
                OrgDomain.org_id != row.org_id,
                OrgDomain.verified_at.is_not(None),
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).first()
    set_org_context(session, row.org_id)
    if taken is not None:
        raise ValidationFailedError(
            "This domain is already verified by another workspace", code="domain_taken"
        )
    values = await lookup_txt(settings, txt_record(row)["name"], client)
    if txt_record(row)["value"] in values:
        row.verified_at = row.verified_at or _now()
        return True
    return False


async def verified_domains(session: AsyncSession, org_id: uuid.UUID) -> set[str]:
    rows = (
        (
            await session.execute(
                # JUSTIFIED allow_unscoped: filtered by org_id explicitly; also called during
                # password login, before any tenant context exists.
                sa.select(OrgDomain.domain)
                .where(OrgDomain.org_id == org_id, OrgDomain.verified_at.is_not(None))
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def domain_is_trusted(
    session: AsyncSession, settings: Settings, org: Org, domain: str
) -> bool:
    """May this workspace bring NEW people in from ``domain``?"""
    if not settings.sso_require_verified_domain:
        return True
    set_org_context(session, org.id)
    return domain in await verified_domains(session, org.id)


async def _role_for_new_member(session: AsyncSession, org: Org, groups: list[str]) -> Role:
    set_org_context(session, org.id)
    sso = org.sso or {}
    mapping = sso.get("group_roles") or {}
    for group in groups:
        role_name = mapping.get(group)
        if role_name:
            role = (
                await session.execute(sa.select(Role).where(Role.name == role_name))
            ).scalar_one_or_none()
            # Group mapping may never hand out ownership.
            if role is not None and "*" not in (role.permissions or []):
                return role
    default_id = sso.get("default_role_id")
    if default_id:
        try:
            role = (
                await session.execute(sa.select(Role).where(Role.id == uuid.UUID(str(default_id))))
            ).scalar_one_or_none()
            # P43: never hand out ownership through SSO/SCIM, however it was configured.
            if role is not None and "*" not in (role.permissions or []):
                return role
        except ValueError:
            pass
    role = (
        await session.execute(sa.select(Role).where(Role.name == "agent", Role.is_system.is_(True)))
    ).scalar_one_or_none()
    if role is None:
        raise FeatureUnavailableError(
            "Single sign-on is not fully configured for this organization"
        )
    return role


async def complete_sso_login(
    session: AsyncSession,
    settings: Settings,
    request: Request,
    response: Response | None,
    *,
    org: Org,
    email: str,
    full_name: str,
    groups: list[str] | None = None,
    protocol: str,
) -> dict:
    email = email.strip().lower()
    sso_domain = str((org.sso or {}).get("domain") or "").strip().lower()
    email_domain = email.partition("@")[2]
    if not sso_domain or email_domain != sso_domain:
        identity_svc.record_login_event(
            session,
            email=email,
            outcome="bad_password",
            org_id=org.id,
            request=request,
            detail="sso_domain_mismatch",
        )
        await session.commit()
        raise PermissionDeniedError(
            "This account is not permitted to sign in to this organization",
            code="sso_domain_mismatch",
        )

    # JUSTIFIED allow_unscoped: users are global; matched by email before org context.
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email)
            .limit(1)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if user is not None and not user.is_active:
        identity_svc.record_login_event(
            session,
            email=email,
            outcome="locked",
            user_id=user.id,
            org_id=org.id,
            request=request,
            detail="sso_user_inactive",
        )
        await session.commit()
        raise PermissionDeniedError("This account is disabled", code="account_locked")

    trusted = await domain_is_trusted(session, settings, org, sso_domain)
    set_org_context(session, org.id)
    membership = None
    if user is not None:
        membership = (
            await session.execute(
                sa.select(OrgMembership).where(
                    OrgMembership.org_id == org.id, OrgMembership.user_id == user.id
                )
            )
        ).scalar_one_or_none()

    if not trusted:
        identity_svc.record_login_event(
            session,
            email=email,
            outcome="bad_password",
            user_id=user.id if user else None,
            org_id=org.id,
            request=request,
            detail="sso_domain_unverified",
        )
        await session.commit()
        raise PermissionDeniedError(
            "This workspace must verify its email domain before single sign-on can be used",
            code="sso_domain_unverified",
        )

    if user is None:
        # An unguessable random password: an SSO-created account is never password-loginable.
        user = User(
            id=uuid.uuid4(),
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            full_name=(full_name or "").strip()[:255],
            is_active=True,
        )
        session.add(user)
        await session.flush()
        audit_svc.record(
            session,
            org.id,
            action="sso.user_provisioned",
            target_type="user",
            target_id=str(user.id),
            detail={"email": email, "protocol": protocol},
        )
    if membership is None:
        role = await _role_for_new_member(session, org, groups or [])
        session.add(OrgMembership(org_id=org.id, user_id=user.id, role_id=role.id))
        audit_svc.record(
            session,
            org.id,
            action="sso.member_added",
            target_type="user",
            target_id=str(user.id),
            detail={"email": email, "role": role.name, "protocol": protocol},
        )
        # P43 (audit): mapping an IdP group to a privileged role is a legitimate thing for
        # an owner to configure (and changing that mapping already needs owner + 2FA +
        # selfie), but a directory sync must never mint that power SILENTLY. Ownership is
        # refused outright in _role_for_new_member; everything else is alerted here.
        if is_privileged_permissions(role.permissions):
            session.add(
                SecurityAlert(
                    id=uuid.uuid4(),
                    kind="sso_privileged_role_granted",
                    org_id=org.id,
                    user_id=user.id,
                    status="open",
                    detail={
                        "email": email,
                        "role": role.name,
                        "protocol": protocol,
                        "groups": list(groups or [])[:20],
                        "note": (
                            "Single sign-on added this person straight into a privileged "
                            "role because of the workspace's group mapping."
                        ),
                    },
                )
            )
            structlog.get_logger("sso").warning(
                "sso_privileged_role_granted", org_id=str(org.id), role=role.name, email=email
            )
    await session.flush()

    token = await login_flow.complete_login(
        session,
        settings,
        request,
        user,
        second_factor=False,
        response=response,
        auth_method="sso",
        org_id=org.id,
        event_outcome="sso",
    )
    return {"access_token": token, "token_type": "bearer", "org_id": str(org.id)}
