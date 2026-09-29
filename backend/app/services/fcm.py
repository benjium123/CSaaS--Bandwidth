"""Ringlite apps P2: FCM HTTP v1 sender (no new dependencies).

Auth is a service-account JWT (RS256, signed with PyJWT) exchanged for a short-lived
OAuth access token at Google's token endpoint; the token is cached in-process until 5
minutes before it expires, guarded by an ``asyncio.Lock`` and keyed by ``client_email``.

``_transport`` may be replaced with an ``httpx.MockTransport`` so tests never touch the
network. ``send`` never raises to its caller and never logs the token value.
"""

from __future__ import annotations

import asyncio
import enum
import json
import time

import httpx
import jwt
import structlog

from app.config import Settings

log = structlog.get_logger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_BASE = "https://fcm.googleapis.com/v1/projects"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
#: Refresh the cached access token this many seconds before it actually expires.
_TOKEN_SAFETY_MARGIN = 300

#: Injectable transport: when set it is used for BOTH the token exchange and the FCM call.
_transport: httpx.AsyncBaseTransport | None = None
#: {client_email: (access_token, expiry_epoch_seconds)} guarded by ``_token_lock``.
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = asyncio.Lock()


class FcmResult(str, enum.Enum):
    OK = "ok"
    UNREGISTERED = "unregistered"
    RETRY = "retry"
    ERROR = "error"


def enabled(settings: Settings) -> bool:
    """FCM is on only when a service-account file is configured."""
    return bool(getattr(settings, "fcm_service_account_file", ""))


def _load_service_account(settings: Settings) -> dict | None:
    path = getattr(settings, "fcm_service_account_file", "")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("fcm.service_account_unreadable", error=str(exc))
        return None


def _fcm_url(account: dict) -> str:
    return f"{FCM_BASE}/{account['project_id']}/messages:send"


def _as_data(data: dict) -> dict[str, str]:
    """FCM data payloads are string-to-string; stringify everything."""
    out: dict[str, str] = {}
    for key, value in (data or {}).items():
        if isinstance(value, str):
            out[str(key)] = value
        elif isinstance(value, (dict, list)):
            out[str(key)] = json.dumps(value)
        else:
            out[str(key)] = str(value)
    return out


async def _get_access_token(account: dict) -> str | None:
    client_email = account.get("client_email", "")
    token_uri = account.get("token_uri") or DEFAULT_TOKEN_URI
    now = time.time()
    try:
        cached = _token_cache.get(client_email)
        if cached is not None and cached[1] - _TOKEN_SAFETY_MARGIN > now:
            return cached[0]
        async with _token_lock:
            cached = _token_cache.get(client_email)
            if cached is not None and cached[1] - _TOKEN_SAFETY_MARGIN > now:
                return cached[0]
            assertion = jwt.encode(
                {
                    "iss": client_email,
                    "scope": FCM_SCOPE,
                    "aud": token_uri,
                    "iat": int(now),
                    "exp": int(now) + 3600,
                },
                account["private_key"],
                algorithm="RS256",
            )
            async with httpx.AsyncClient(transport=_transport, timeout=10.0) as client:
                response = await client.post(
                    token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            if response.status_code != 200:
                log.warning("fcm.token_rejected", status=response.status_code)
                return None
            body = response.json()
            access_token = body.get("access_token")
            if not access_token:
                return None
            expires_in = int(body.get("expires_in", 3600))
            _token_cache[client_email] = (access_token, now + expires_in)
            return access_token
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        log.warning("fcm.token_error", error=str(exc))
        return None


def _is_unregistered(response: httpx.Response) -> bool:
    """A 400 means a dead token only when the body says so."""
    body = response.text or ""
    if "UNREGISTERED" in body:
        return True
    lowered = body.lower()
    return "INVALID_ARGUMENT" in body and (
        "registration token" in lowered or "registration_token" in lowered
    )


async def send(
    settings: Settings,
    token: str,
    data: dict[str, str],
    *,
    high_priority: bool,
    notification: dict | None = None,
    collapse_key: str | None = None,
    ttl_seconds: int | None = None,
) -> FcmResult:
    account = _load_service_account(settings)
    if account is None:
        return FcmResult.ERROR
    access_token = await _get_access_token(account)
    if access_token is None:
        return FcmResult.ERROR

    android: dict = {"priority": "HIGH" if high_priority else "NORMAL"}
    if ttl_seconds is not None:
        android["ttl"] = f"{int(ttl_seconds)}s"
    if collapse_key:
        android["collapse_key"] = collapse_key
    if notification:
        android["notification"] = notification

    message = {"token": token, "data": _as_data(data), "android": android}
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=10.0) as client:
            response = await client.post(
                _fcm_url(account),
                json={"message": message},
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except Exception as exc:  # noqa: BLE001 - network errors are transient
        log.warning("fcm.send_failed", error=str(exc))
        return FcmResult.RETRY

    status = response.status_code
    if status == 200:
        return FcmResult.OK
    if status == 404:
        return FcmResult.UNREGISTERED
    if status == 400 and _is_unregistered(response):
        return FcmResult.UNREGISTERED
    if status == 429 or status >= 500:
        return FcmResult.RETRY
    log.warning("fcm.send_error", status=status)
    return FcmResult.ERROR
