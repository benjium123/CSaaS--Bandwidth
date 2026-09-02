from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.errors import CarrierNotConfiguredError

#: A malformed master key (wrong length/encoding), a rotated key that no longer matches
#: what a row was encrypted with, or ciphertext that simply is not a valid Fernet token
#: must all surface the SAME 503 as "no key configured" - never a 500. A 500 here would
#: mean an operator's key rotation (or a hand-edited row) turns every GET/PATCH on
#: provider-accounts into an unhandled-exception page instead of the same "credential
#: storage not configured" the routes already handle everywhere else.
_KEY_OR_TOKEN_ERRORS = (ValueError, InvalidToken, TypeError)


def master_key_present(settings: Settings) -> bool:
    return bool(settings.credentials_master_key.get_secret_value().strip())


def _fernet(settings: Settings) -> Fernet:
    try:
        return Fernet(settings.credentials_master_key.get_secret_value().encode())
    except _KEY_OR_TOKEN_ERRORS as exc:
        raise CarrierNotConfiguredError(
            "credential storage key is invalid or does not match stored credentials"
        ) from exc


def encrypt(settings: Settings, data: dict) -> str:
    if not master_key_present(settings):
        raise CarrierNotConfiguredError("credential storage not configured")
    fernet = _fernet(settings)
    return fernet.encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")


def decrypt(settings: Settings, token: str) -> dict:
    if not master_key_present(settings):
        raise CarrierNotConfiguredError("credential storage not configured")
    fernet = _fernet(settings)
    try:
        return json.loads(fernet.decrypt(token.encode("utf-8")).decode("utf-8"))
    except _KEY_OR_TOKEN_ERRORS as exc:
        raise CarrierNotConfiguredError(
            "credential storage key is invalid or does not match stored credentials"
        ) from exc
