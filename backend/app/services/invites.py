"""Invite issue + redemption, and the gate that makes registration invite-only.

The bootstrap rule is the subtle part. Registration is open ONLY while the instance has
no users at all — that is first-run setup, where there is nobody to issue an invite and
refusing would brick the deployment. The instant a first account exists, the door shuts.
That check is a COUNT against the users table rather than a config flag on purpose: a flag
can be left true by accident and silently reopen the instance months later, whereas this
condition becomes false by itself and can never drift back.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.invites import Invite
from app.models.rbac import SYSTEM_ROLES, OrgMembership, Role
from app.models.user import User

log = structlog.get_logger("invites")

DEFAULT_TTL_HOURS = 168  # 7 days
TOKEN_BYTES = 32

#: An invite may never mint an owner. Ownership transfers are a deliberate, separate act -
#: an emailed link is the wrong instrument for handing over the account that can delete the
#: org and see the billing.
INVITABLE_ROLES = tuple(r for r in SYSTEM_ROLES if r != "owner")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def instance_has_users(session: AsyncSession) -> bool:
    """True once ANY account exists. Unscoped by necessity: this question is about the
    whole deployment, and it is asked before any org context can exist."""
    result = await session.execute(
        sa.select(sa.func.count())
        .select_from(User)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return int(result.scalar_one()) > 0


async def create_invite(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    role_name: str,
    created_by: uuid.UUID | None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> tuple[Invite, str]:
    """Issue an invite. Returns the row and the RAW token, which the caller must surface
    exactly once - it is not recoverable afterwards."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValidationFailedError("A valid email address is required")
    if role_name not in INVITABLE_ROLES:
        raise ValidationFailedError(
            f"Role must be one of: {', '.join(INVITABLE_ROLES)}. "
            "Ownership is transferred deliberately, never by invitation."
        )

    existing_user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if existing_user is not None:
        member = (
            await session.execute(
                sa.select(OrgMembership).where(
                    OrgMembership.org_id == org_id,
                    OrgMembership.user_id == existing_user.id,
                )
            )
        ).scalar_one_or_none()
        if member is not None:
            raise ConflictError(f"{email} is already a member of this organisation")

    raw = secrets.token_urlsafe(TOKEN_BYTES)
    invite = Invite(
        id=uuid.uuid4(),
        org_id=org_id,
        email=email,
        role_name=role_name,
        token_hash=hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        created_by=created_by,
    )
    session.add(invite)
    log.info("invite_created", org_id=str(org_id), email=email, role=role_name)
    return invite, raw


async def find_redeemable(session: AsyncSession, raw_token: str, email: str) -> Invite:
    """Resolve a raw token to a usable invite, or explain precisely why not.

    Looked up BY HASH, so an attacker who can read the table still cannot present a token.
    """
    invite = (
        await session.execute(
            sa.select(Invite)
            .where(Invite.token_hash == hash_token(raw_token))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()

    if invite is None:
        raise ValidationFailedError("That invitation is not valid.")
    if invite.accepted_at is not None:
        raise ValidationFailedError("That invitation has already been used.")
    if invite.revoked_at is not None:
        raise ValidationFailedError("That invitation was revoked.")

    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise ValidationFailedError("That invitation has expired. Ask for a new one.")

    if normalize_email(email) != invite.email:
        # Deliberately does NOT reveal the invited address - that would turn a leaked
        # token into a way to learn who was invited.
        raise ValidationFailedError(
            "That invitation was issued to a different email address."
        )
    return invite


async def redeem(session: AsyncSession, invite: Invite, user_id: uuid.UUID) -> None:
    """Attach the new user to the invite's org with the invite's role, and spend it."""
    session.info[ALLOW_UNSCOPED_KEY] = True
    try:
        role = (
            await session.execute(
                sa.select(Role).where(
                    Role.org_id == invite.org_id, Role.name == invite.role_name
                )
            )
        ).scalar_one_or_none()
        if role is None:
            raise NotFoundError(
                f"Role {invite.role_name!r} no longer exists in that organisation"
            )
        session.add(
            OrgMembership(
                id=uuid.uuid4(),
                org_id=invite.org_id,
                user_id=user_id,
                role_id=role.id,
            )
        )
        invite.accepted_at = datetime.now(timezone.utc)
        # Flush INSIDE the allow-unscoped window. The membership is the row that
        # establishes the tenant context, so it cannot itself be written under one;
        # popping the flag before the flush leaves the write to be rejected later by
        # the cross-tenant guard. Same shape as repositories/orgs.create_org_with_owner.
        await session.flush()
    finally:
        session.info.pop(ALLOW_UNSCOPED_KEY, None)
    log.info("invite_redeemed", org_id=str(invite.org_id), email=invite.email)
