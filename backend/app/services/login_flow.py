"""P41: the one place a successful sign-in becomes a Session.

Password login (routes/auth.py), TOTP verify (routes/twofa.py) and passkey verify
(routes/passkeys.py) all finish here, so risk assessment, device memory, the operator alert
and the owner email cannot drift apart between the three paths.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import create_access_token
from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import OrgMembership, Role, User
from app.services import identity as identity_svc
from app.services import login_risk, mailer

#: Strong refs for fire-and-forget alert emails (a bare create_task can be GC'd mid-flight).
_pending_emails: set[asyncio.Task] = set()


async def owner_emails_for_user(session: AsyncSession, user: User) -> list[str]:
    """Emails of the owners of every workspace this user belongs to, plus the user."""
    # JUSTIFIED allow_unscoped: sign-in precedes any org context, and the alert must reach
    # the owners of EVERY workspace this account can act in.
    org_ids = (
        await session.execute(
            sa.select(OrgMembership.org_id)
            .where(OrgMembership.user_id == user.id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    emails = {user.email}
    if org_ids:
        rows = (
            await session.execute(
                sa.select(User.email)
                .join(OrgMembership, OrgMembership.user_id == User.id)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id.in_(org_ids), Role.name == "owner")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
        emails.update(rows)
    return sorted(emails)


def _alert_body(settings: Settings, user: User, risk: login_risk.LoginRisk) -> str:
    reasons = "\n".join(f"  - {reason}" for reason in risk.describe())
    where = risk.country or "an unknown location"
    return (
        f"The account {user.email} just signed in to {settings.app_name}.\n\n"
        f"This sign-in was flagged because it:\n{reasons}\n\n"
        f"Location: {where}\nNetwork: {risk.asn_org or 'unknown'}\nIP address: {risk.ip}\n\n"
        "The person signing in passed the authenticator app or passkey check.\n"
        "If this was not you or someone on your team, sign out all sessions from "
        "Settings > Security right away and contact support."
    )


async def complete_login(
    session: AsyncSession,
    settings: Settings,
    request: Request,
    user: User,
    *,
    second_factor: bool,
    extra_flags: list[str] | None = None,
) -> str:
    """Create the Session, record the event, handle risk. Commits. Returns the access token."""
    risk = await login_risk.assess(session, settings, request, user)
    # P42: recovery sign-ins are always flagged, whatever the network looks like.
    for flag in extra_flags or []:
        if flag not in risk.flags:
            risk.flags.append(flag)
    now = datetime.now(timezone.utc)

    identity_session = await identity_svc.create_session(
        session,
        user_id=user.id,
        org_id=None,
        request=request,
        expire_hours=settings.jwt_expire_hours,
    )
    identity_session.risk_flags = list(risk.flags)
    identity_session.country = risk.country
    identity_session.second_factor_at = now if second_factor else None
    await session.flush()

    if second_factor:
        await login_risk.remember_device(session, user, risk)

    identity_svc.record_login_event(
        session,
        email=user.email,
        outcome="ok",
        user_id=user.id,
        request=request,
        detail=("risk:" + ",".join(risk.flags)) if risk.flagged else None,
    )
    recipients: list[str] = []
    if risk.flagged:
        login_risk.open_alert(session, user, risk, request)
        recipients = await owner_emails_for_user(session, user)

    token = create_access_token(
        user.id,
        settings.jwt_secret.get_secret_value(),
        expire_hours=settings.jwt_expire_hours,
        sid=identity_session.id,
    )
    await session.commit()

    if recipients:
        task = asyncio.create_task(
            mailer.send(
                settings,
                recipients,
                f"Security alert: unusual sign-in to {settings.app_name}",
                _alert_body(settings, user, risk),
            )
        )
        _pending_emails.add(task)
        task.add_done_callback(_pending_emails.discard)
        if settings.app_env == "test":
            await task
    return token


def second_factor_methods(user: User) -> list[str]:
    methods: list[str] = []
    if user.totp_enabled:
        methods.append("totp")
    if user.has_passkey:
        methods.append("passkey")
    return methods
