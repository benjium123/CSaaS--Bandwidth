"""A six-digit code emailed to the account address: the email second factor.

One live code per user. Only an HMAC of it is stored, keyed by the JWT secret and bound to
the user and to the PURPOSE it was sent for, so a code mailed for enrolment cannot finish a
sign-in and a sign-in code cannot pass a step-up. A code dies on first use, after
EMAIL_CODE_TTL, or after MAX_ATTEMPTS wrong guesses (a million codes, five guesses).
Re-sending is throttled so the endpoint cannot be used to flood someone's inbox.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.config import Settings
from app.errors import UnauthenticatedError, ValidationFailedError
from app.models import User
from app.services import mailer

Purpose = Literal["enrol", "login", "step_up"]

EMAIL_CODE_TTL = timedelta(minutes=10)
RESEND_AFTER = timedelta(seconds=30)
MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _digest(settings: Settings, user: User, purpose: str, code: str) -> str:
    key = settings.jwt_secret.get_secret_value().encode()
    return hmac.new(key, f"{user.id}:{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def _body(settings: Settings, code: str, purpose: Purpose) -> tuple[str, str]:
    what = {
        "enrol": "turn on email codes for signing in",
        "login": "finish signing in",
        "step_up": "confirm a sensitive change",
    }[purpose]
    subject = f"{code} is your {settings.app_name} code"
    body = (
        f"Your {settings.app_name} code is {code}\n\n"
        f"Enter it to {what}. It expires in {int(EMAIL_CODE_TTL.total_seconds() // 60)} minutes "
        "and works once.\n\n"
        "If you did not ask for this, someone may know your password. Change it from "
        "Settings > Security and contact support."
    )
    return subject, body


async def issue(settings: Settings, user: User, purpose: Purpose) -> None:
    """Mint a fresh code for ``purpose``, store its digest on ``user`` and email it.

    The caller commits. Raises when the previous code was sent under RESEND_AFTER ago.
    """
    now = _now()
    expires = _aware(user.email_code_expires_at)
    if expires is not None and expires - EMAIL_CODE_TTL + RESEND_AFTER > now:
        raise ValidationFailedError(
            "A code was just sent. Wait a few seconds before asking for another.",
            code="email_code_throttled",
        )
    code = f"{secrets.randbelow(1_000_000):06d}"
    user.email_code_hash = _digest(settings, user, purpose, code)
    user.email_code_purpose = purpose
    user.email_code_expires_at = now + EMAIL_CODE_TTL
    user.email_code_attempts = 0
    subject, body = _body(settings, code, purpose)
    await mailer.send(settings, [user.email], subject, body)


def check(settings: Settings, user: User, purpose: Purpose, code: str) -> None:
    """Accept ``code`` for ``purpose`` and burn it, or count the miss and raise.

    The caller commits in BOTH cases: a wrong guess must be persisted even though the
    request fails, or the attempt cap would reset on every try.
    """
    code = code.strip()
    expires = _aware(user.email_code_expires_at)
    if (
        not user.email_code_hash
        or user.email_code_purpose != purpose
        or expires is None
        or expires <= _now()
        or user.email_code_attempts >= MAX_ATTEMPTS
    ):
        raise UnauthenticatedError(
            "That code has expired. Send a new one.", code="email_code_expired"
        )
    if not hmac.compare_digest(user.email_code_hash, _digest(settings, user, purpose, code)):
        user.email_code_attempts += 1
        raise UnauthenticatedError("Invalid verification code")
    clear(user)


def clear(user: User) -> None:
    user.email_code_hash = None
    user.email_code_purpose = None
    user.email_code_expires_at = None
    user.email_code_attempts = 0
