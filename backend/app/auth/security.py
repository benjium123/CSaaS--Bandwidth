"""Password hashing, JWTs, and credential encryption.

argon2id via argon2-cffi and PyJWT directly. passlib (unmaintained) and python-jose (CVE
history) are deliberately not used.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.errors import UnauthenticatedError

_hasher = PasswordHasher()

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------------------
# API keys (P13 DR-3). SHA-256, not argon2: these are 256-bit random secrets verified on
# every machine request - brute-force resistance comes from entropy, not work factor, and
# argon2 per-request would be a self-inflicted DoS.
# --------------------------------------------------------------------------------------
API_KEY_TOKEN_PREFIX = "csk"


def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, key_hash). The full key is shown ONCE at creation."""
    prefix = _secrets.token_hex(4)
    secret = _secrets.token_urlsafe(32)
    full = f"{API_KEY_TOKEN_PREFIX}_{prefix}_{secret}"
    return full, prefix, hash_api_key(full)


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def api_key_hash_matches(full_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(full_key), stored_hash)


def parse_api_key_prefix(full_key: str) -> str | None:
    """The prefix segment of ``csk_<prefix>_<secret>``, or None if malformed."""
    parts = full_key.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_TOKEN_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1]


def create_access_token(user_id: uuid.UUID, secret: str, *, expire_hours: int = 24) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=expire_hours)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> uuid.UUID:
    """Return the subject, or raise UnauthenticatedError. Never leaks why.

    A token carrying ANY ``scope`` claim is rejected outright: scoped tokens (currently the
    5-minute 2FA-pending token) are not access tokens, and letting one through here would
    turn "password correct, second factor pending" into a full login.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        if payload.get("scope"):
            raise ValueError("scoped token is not an access token")
        return uuid.UUID(payload["sub"])
    except Exception as exc:  # expired, bad signature, malformed sub — all the same to callers
        raise UnauthenticatedError("Invalid or expired token") from exc


PENDING_2FA_SCOPE = "2fa-pending"


def create_pending_2fa_token(user_id: uuid.UUID, secret: str, *, expire_minutes: int = 5) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "scope": PENDING_2FA_SCOPE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_pending_2fa_token(token: str, secret: str) -> uuid.UUID:
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        if payload.get("scope") != PENDING_2FA_SCOPE:
            raise ValueError("not a pending-2fa token")
        return uuid.UUID(payload["sub"])
    except Exception as exc:
        raise UnauthenticatedError("Invalid or expired verification session") from exc


# --------------------------------------------------------------------------------------
# Credential encryption at rest. Available from P0; first consumed in P1 when we start
# storing per-org carrier credentials.
# --------------------------------------------------------------------------------------
def encrypt_credential(plaintext: str, key: str) -> str:
    return Fernet(key.encode()).encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str, key: str) -> str:
    try:
        return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Credential could not be decrypted with the configured key") from exc
