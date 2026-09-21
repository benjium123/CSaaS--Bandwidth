"""Issue and consume platform-scoped admin invitations.

Security rules enforced here:

* The plaintext token is generated with ``secrets.token_urlsafe(32)`` and returned to the
  caller exactly once. Only its SHA-256 hex digest is persisted.
* ``consume`` performs a single guarded ``UPDATE ... RETURNING`` so two concurrent
  requests cannot both accept the same invitation, and an expired or wrong-email token
  can never be consumed.
* Neither function commits. The caller owns the transaction, which is what lets a
  signup flow roll back the consumption if user creation or password hashing fails.
* Nothing here grants operator rights. The caller is responsible for checking that the
  acting user is active and authorised.

Audit: issuance and acceptance are recorded as ``LoginEvent`` rows with outcome
``admin_invite``. These are ADMINISTRATIVE audit records, not authentication successes -
``LOGIN_OUTCOMES`` is deliberately left untouched, and the ``outcome`` column is a free
String(16) with no DB enum or check constraint.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ValidationFailedError
from app.models.admin_invite import AdminInvite
from app.models.identity import LoginEvent

#: Bounds on the requested lifetime. One hour is the shortest useful window; a week is the
#: longest we are willing to leave an unused operator invitation alive.
MIN_EXPIRES_HOURS = 1
MAX_EXPIRES_HOURS = 168

#: A token_urlsafe(32) is 43 characters. Anything wildly longer is not one of ours, so we
#: reject it before hashing rather than doing work on attacker-controlled input.
MAX_TOKEN_LENGTH = 512

#: Generic message for every consume failure. Never reveal whether the token existed, was
#: expired, or was bound to a different email.
_INVALID_INVITE_MESSAGE = "This invitation is invalid or has expired"


def _normalize_email(email: str) -> str:
    """Validate and normalise an email without any network lookup."""
    if not isinstance(email, str) or not email.strip():
        raise ValidationFailedError("A valid email address is required")
    try:
        result = validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValidationFailedError("A valid email address is required") from exc
    return result.normalized.lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invalid_invite() -> ValidationFailedError:
    return ValidationFailedError(_INVALID_INVITE_MESSAGE, code="invalid_admin_invite")


def _record_admin_invite_event(
    session: AsyncSession,
    *,
    email: str,
    detail: str,
    user_id: uuid.UUID | None = None,
) -> LoginEvent:
    """Append an administrative audit row. No commit here.

    This is NOT a login outcome: it records that an operator invitation was issued or
    accepted. ``detail`` never carries the token, its hash, or a password.
    """
    row = LoginEvent(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=None,
        email=email,
        at=datetime.now(timezone.utc),
        ip=None,
        user_agent=None,
        outcome="admin_invite",
        detail=detail[:255],
    )
    session.add(row)
    return row


async def issue(
    session: AsyncSession, *, email: str, expires_hours: int = 24
) -> tuple[AdminInvite, str]:
    """Create an invitation and return ``(row, plaintext_token)``.

    The caller commits. The plaintext token is returned once and never stored.
    """
    normalized = _normalize_email(email)

    if not isinstance(expires_hours, int) or isinstance(expires_hours, bool):
        raise ValidationFailedError("expires_hours must be a whole number of hours")
    if not (MIN_EXPIRES_HOURS <= expires_hours <= MAX_EXPIRES_HOURS):
        raise ValidationFailedError(
            f"expires_hours must be between {MIN_EXPIRES_HOURS} and {MAX_EXPIRES_HOURS}"
        )

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    row = AdminInvite(
        id=uuid.uuid4(),
        email=normalized,
        token_hash=_hash_token(token),
        expires_at=now + timedelta(hours=expires_hours),
        consumed_at=None,
        consumed_by=None,
        issued_via="trusted_cli",
    )
    session.add(row)
    await session.flush()

    _record_admin_invite_event(
        session,
        email=normalized,
        detail=f"issued:trusted_cli:{row.id}",
    )
    return row, token


async def consume(
    session: AsyncSession,
    *,
    token: str,
    email: str,
    user_id: uuid.UUID | None = None,
) -> AdminInvite:
    """Atomically consume an invitation and return the updated row.

    The caller commits. ``user_id`` is recorded as ``consumed_by`` when the accepting user
    already exists; a signup flow may pass ``None`` and set ``consumed_by`` itself before
    committing.

    ``populate_existing=True`` is required alongside ``synchronize_session=False``: the
    UPDATE ... RETURNING hands back an ORM instance, and without it SQLAlchemy would reuse
    a stale identity-map row (old ``consumed_at``/``consumed_by``) instead of the values
    the database just wrote.
    """
    if not isinstance(token, str) or not token.strip():
        raise _invalid_invite()
    if len(token) > MAX_TOKEN_LENGTH:
        raise _invalid_invite()

    normalized = _normalize_email(email)
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)

    stmt = (
        sa.update(AdminInvite)
        .where(
            AdminInvite.token_hash == token_hash,
            AdminInvite.email == normalized,
            AdminInvite.expires_at > now,
            AdminInvite.consumed_at.is_(None),
        )
        .values(consumed_at=now, consumed_by=user_id)
        .returning(AdminInvite)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _invalid_invite()

    _record_admin_invite_event(
        session,
        email=normalized,
        user_id=user_id,
        detail=f"accepted:{row.id}",
    )
    return row
