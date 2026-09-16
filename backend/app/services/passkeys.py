"""P41 passkeys (WebAuthn) - registration, second-factor login and step-up.

Challenges live in ``webauthn_challenges`` and are single-use: ``consume_challenge`` stamps
``consumed_at`` BEFORE verification runs, so a wrong attempt burns the challenge too and a
captured assertion can never be replayed against it.

The relying party id defaults to PUBLIC_WEB_URL's host and the expected origin is always
PUBLIC_WEB_URL: the browser signs the origin it is on, so accepting any other origin would
let a look-alike domain relay a user's passkey.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import NotFoundError, UnauthenticatedError, ValidationFailedError
from app.models import User, UserPasskey, WebauthnChallenge

CHALLENGE_TTL = timedelta(minutes=5)
MAX_PASSKEYS_PER_USER = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def rp_id(settings: Settings) -> str:
    if settings.webauthn_rp_id.strip():
        return settings.webauthn_rp_id.strip()
    return urlparse(settings.public_web_url).hostname or "localhost"


def expected_origin(settings: Settings) -> str:
    parsed = urlparse(settings.public_web_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _b64url(data: bytes) -> str:
    from webauthn.helpers import bytes_to_base64url

    return bytes_to_base64url(data)


async def _new_challenge(
    session: AsyncSession, user_id: uuid.UUID, purpose: str
) -> WebauthnChallenge:
    row = WebauthnChallenge(
        id=uuid.uuid4(),
        user_id=user_id,
        purpose=purpose,
        challenge=secrets.token_bytes(32),
        expires_at=_now() + CHALLENGE_TTL,
    )
    session.add(row)
    await session.flush()
    return row


async def consume_challenge(
    session: AsyncSession, challenge_id: uuid.UUID, *, user_id: uuid.UUID, purpose: str
) -> bytes:
    row = await session.get(WebauthnChallenge, challenge_id)
    if (
        row is None
        or row.user_id != user_id
        or row.purpose != purpose
        or row.consumed_at is not None
        or _aware(row.expires_at) <= _now()
    ):
        raise UnauthenticatedError("This passkey request has expired - try again")
    row.consumed_at = _now()
    return row.challenge


async def list_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[UserPasskey]:
    stmt = (
        sa.select(UserPasskey)
        .where(UserPasskey.user_id == user_id)
        .order_by(UserPasskey.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def registration_options(
    session: AsyncSession, settings: Settings, user: User
) -> tuple[uuid.UUID, dict]:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    existing = await list_for_user(session, user.id)
    if len(existing) >= MAX_PASSKEYS_PER_USER:
        raise ValidationFailedError(f"You can register at most {MAX_PASSKEYS_PER_USER} passkeys")
    challenge = await _new_challenge(session, user.id, "register")
    from webauthn.helpers import base64url_to_bytes

    options = generate_registration_options(
        rp_id=rp_id(settings),
        rp_name=settings.app_name,
        user_name=user.email,
        user_id=user.id.bytes,
        user_display_name=user.full_name or user.email,
        challenge=challenge.challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
            for p in existing
        ],
    )
    return challenge.id, json.loads(options_to_json(options))


async def register(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    challenge_id: uuid.UUID,
    credential: dict,
    name: str,
) -> UserPasskey:
    from webauthn import verify_registration_response

    expected = await consume_challenge(
        session, challenge_id, user_id=user.id, purpose="register"
    )
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=rp_id(settings),
            expected_origin=expected_origin(settings),
            require_user_verification=True,
        )
    except Exception as exc:
        raise ValidationFailedError("That passkey could not be verified") from exc

    credential_id = _b64url(verified.credential_id)
    taken = (
        await session.execute(
            sa.select(UserPasskey.id).where(UserPasskey.credential_id == credential_id)
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise ValidationFailedError("That passkey is already registered")

    transports = credential.get("response", {}).get("transports")
    row = UserPasskey(
        id=uuid.uuid4(),
        user_id=user.id,
        credential_id=credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        name=(name or "Passkey").strip()[:64] or "Passkey",
        transports=transports if isinstance(transports, list) else None,
    )
    session.add(row)
    user.has_passkey = True
    return row


async def authentication_options(
    session: AsyncSession, settings: Settings, user: User, *, purpose: str
) -> tuple[uuid.UUID, dict]:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    passkeys = await list_for_user(session, user.id)
    if not passkeys:
        raise ValidationFailedError("No passkey is registered for this account")
    challenge = await _new_challenge(session, user.id, purpose)
    options = generate_authentication_options(
        rp_id=rp_id(settings),
        challenge=challenge.challenge,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
            for p in passkeys
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return challenge.id, json.loads(options_to_json(options))


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    challenge_id: uuid.UUID,
    credential: dict,
    purpose: str,
) -> UserPasskey:
    """Verify an assertion from one of THIS user's passkeys, or raise 401."""
    from webauthn import verify_authentication_response

    expected = await consume_challenge(session, challenge_id, user_id=user.id, purpose=purpose)
    credential_id = str(credential.get("id") or credential.get("rawId") or "")
    passkey = (
        await session.execute(
            sa.select(UserPasskey).where(
                UserPasskey.user_id == user.id, UserPasskey.credential_id == credential_id
            )
        )
    ).scalar_one_or_none()
    if passkey is None:
        raise UnauthenticatedError("That passkey is not registered to this account")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=rp_id(settings),
            expected_origin=expected_origin(settings),
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        raise UnauthenticatedError("Passkey verification failed") from exc
    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = _now()
    return passkey


async def delete(
    session: AsyncSession, settings: Settings, user: User, passkey_id: uuid.UUID
) -> None:
    row = await session.get(UserPasskey, passkey_id)
    if row is None or row.user_id != user.id:
        raise NotFoundError("Passkey not found")
    remaining = [p for p in await list_for_user(session, user.id) if p.id != row.id]
    if settings.require_2fa_all_users and not remaining and not user.totp_enabled:
        raise ValidationFailedError(
            "This is your only second factor. Add an authenticator app or another passkey "
            "before removing it.",
            code="last_second_factor",
        )
    await session.delete(row)
    user.has_passkey = bool(remaining)
