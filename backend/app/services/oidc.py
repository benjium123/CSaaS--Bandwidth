"""OIDC single-sign-on helpers.

This module deliberately hand-rolls the small slice of OIDC we need. The auth surface
should have as few dependencies as possible: adding a full OIDC client library brings
attack surface we do not use (dynamic registration, refresh flows, encryption keys, etc.)
and hides exactly the checks below.

Every id_token must pass, in order:
  1. ``alg`` is in an explicit RSA/ES allowlist and a ``kid`` is present.
  2. The JWK with that ``kid`` can be resolved from the IdP JWKS document.
  3. `jwt.decode` verifies signature, issuer, audience, expiry, iat and sub presence.
  4. The ``nonce`` claim matches the nonce issued for this login attempt.
  5. If ``azp`` is present it must equal the configured client_id.
  6. ``email`` is present, normalised, and ``email_verified`` is truthy when present.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from collections import OrderedDict
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import structlog
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from jwt.exceptions import PyJWTError

from app.config import Settings
from app.errors import UnauthenticatedError, ValidationFailedError

logger = structlog.get_logger("oidc")

# Explicit allowlist. Passing the token's OWN ``alg`` to ``jwt.decode`` is the classic
# algorithm-confusion hole. ``none`` and every HS* are refused because an attacker who
# knows the public JWKS could otherwise sign an HS256 token with it.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")

STATE_TTL_SECONDS = 600
DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600
HTTP_TIMEOUT = 10.0


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)


_client_factory = _default_client_factory


def set_client_factory(factory) -> None:
    """Install a test HTTP client factory (e.g. ``httpx.MockTransport``)."""
    global _client_factory
    _client_factory = factory


def reset_client_factory() -> None:
    global _client_factory
    _client_factory = _default_client_factory


def _client() -> httpx.AsyncClient:
    return _client_factory()


def _require_https(url: str, *, allow_insecure: bool = False) -> None:
    if allow_insecure:
        return
    if urlparse(url).scheme != "https":
        raise ValidationFailedError("SSO endpoints must use https")


# --- discovery and JWKS caches ------------------------------------------------

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}


async def discover(issuer: str) -> dict:
    """Fetch and cache the IdP discovery document.

    The response's own ``issuer`` must exactly equal the configured issuer. A mismatch is
    an IdP-substitution attack, not a discovery failure.
    """
    _require_https(issuer)

    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached is not None and cached[0] > now:
        return cached[1]

    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    async with _client() as client:
        response = await client.get(discovery_url)
        if response.status_code != 200:
            raise ValidationFailedError("IdP discovery failed")
        try:
            document = response.json()
        except ValueError as exc:
            raise ValidationFailedError("IdP discovery response is invalid") from exc

    if not isinstance(document, dict):
        raise ValidationFailedError("IdP discovery response is invalid")
    if document.get("issuer") != issuer:
        raise ValidationFailedError("IdP discovery did not match configured issuer")

    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = document.get(field)
        if not isinstance(value, str) or not value:
            raise ValidationFailedError(
                "IdP discovery response is missing required endpoints"
            )
        _require_https(value)

    _discovery_cache[issuer] = (time.monotonic() + DISCOVERY_TTL_SECONDS, document)
    return document


async def fetch_jwks(
    jwks_uri: str,
    *,
    force: bool = False,
    allow_insecure: bool = False,
) -> dict:
    """Fetch and cache the IdP JWKS document."""
    _require_https(jwks_uri, allow_insecure=allow_insecure)

    now = time.monotonic()
    if not force:
        cached = _jwks_cache.get(jwks_uri)
        if cached is not None and cached[0] > now:
            return cached[1]

    async with _client() as client:
        response = await client.get(jwks_uri)
        if response.status_code != 200:
            raise ValidationFailedError("IdP JWKS request failed")
        try:
            document = response.json()
        except ValueError as exc:
            raise ValidationFailedError("IdP JWKS response is invalid") from exc

    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValidationFailedError("IdP JWKS response is invalid")

    _jwks_cache[jwks_uri] = (time.monotonic() + JWKS_TTL_SECONDS, document)
    return document


def reset_caches() -> None:
    """Clear discovery, JWKS and in-process state caches. Tests call this."""
    _discovery_cache.clear()
    _jwks_cache.clear()
    _state_memory_cache.clear()


# --- state/nonce --------------------------------------------------------------

class _StateMemoryCache:
    """Small TTL cache for SSO state. Mirrors ``app/services/session_cache.py``."""

    def __init__(self, max_keys: int = 50_000) -> None:
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = Lock()
        self._max_keys = max_keys

    def get(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._store[key] = (now + ttl, value)
            self._store.move_to_end(key)
            self._sweep(now)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _sweep(self, now: float) -> None:
        while self._store:
            _key, (expires_at, _value) = next(iter(self._store.items()))
            if expires_at <= now or len(self._store) > self._max_keys:
                self._store.popitem(last=False)
            else:
                break


_state_memory_cache = _StateMemoryCache()

_resolved_state_redis: tuple[str, Any] | None = None
_state_redis_lock = Lock()


def _state_redis_client(settings: Settings) -> Any | None:
    """Lazily create a Redis client for SSO state, mirroring session_cache."""
    global _resolved_state_redis

    url = settings.redis_url
    if not url:
        return None

    if _resolved_state_redis is not None and _resolved_state_redis[0] == url:
        return _resolved_state_redis[1]

    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return None

    with _state_redis_lock:
        if _resolved_state_redis is not None and _resolved_state_redis[0] == url:
            return _resolved_state_redis[1]
        client = redis_asyncio.from_url(url, decode_responses=True)
        _resolved_state_redis = (url, client)

    return _resolved_state_redis[1]


async def issue_state(
    settings: Settings,
    *,
    org_id,
    nonce: str,
    redirect_to: str | None = None,
) -> str:
    state = secrets.token_urlsafe(32)
    payload = {"org_id": str(org_id), "nonce": nonce, "redirect_to": redirect_to}
    raw = json.dumps(payload)
    key = f"sso:state:{state}"

    client = _state_redis_client(settings)
    if client is None:
        _state_memory_cache.set(key, raw, STATE_TTL_SECONDS)
    else:
        await client.set(key, raw, ex=STATE_TTL_SECONDS)

    return state


async def consume_state(settings: Settings, state: str) -> dict | None:
    """Read AND delete state. Unknown/expired/replayed states return None.

    Unlike ``session_cache``, this store fails CLOSED. A Redis outage must reject the
    login attempt rather than silently accepting an unverified state.
    """
    key = f"sso:state:{state}"
    try:
        client = _state_redis_client(settings)
        if client is None:
            raw = _state_memory_cache.get(key)
            if raw is None:
                return None
            _state_memory_cache.delete(key)
        else:
            raw = await client.get(key)
            if raw is None:
                return None
            await client.delete(key)

        return json.loads(raw)
    except Exception as exc:
        logger.warning("sso_state_unavailable", error=type(exc).__name__)
        return None


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


# --- id_token verification ----------------------------------------------------

def _jwk_by_kid(jwks: dict, kid: str) -> dict | None:
    for key in jwks.get("keys", []):
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    return None


async def verify_id_token(
    id_token: str,
    *,
    issuer: str,
    client_id: str,
    nonce: str,
    jwks_uri: str,
    allow_insecure: bool = False,
) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except PyJWTError as exc:
        raise UnauthenticatedError("Invalid or expired id_token") from exc

    if header.get("alg") not in ALLOWED_ALGORITHMS:
        raise UnauthenticatedError("Invalid or expired id_token")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise UnauthenticatedError("Invalid or expired id_token")

    jwks = await fetch_jwks(jwks_uri, allow_insecure=allow_insecure)
    jwk = _jwk_by_kid(jwks, kid)
    if jwk is None:
        # Key rotation: a kid miss invalidates the cache for this URI and refetches once.
        jwks = await fetch_jwks(jwks_uri, force=True, allow_insecure=allow_insecure)
        jwk = _jwk_by_kid(jwks, kid)

    if jwk is None:
        raise UnauthenticatedError("Invalid or expired id_token")

    try:
        kty = jwk.get("kty")
        if kty == "RSA":
            key = RSAAlgorithm.from_jwk(jwk)
        elif kty == "EC":
            key = ECAlgorithm.from_jwk(jwk)
        else:
            raise UnauthenticatedError("Invalid or expired id_token")

        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=client_id,
            issuer=issuer,
            options={
                "require": ["exp", "iat", "iss", "aud", "sub"],
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
            leeway=60,
        )
    except UnauthenticatedError:
        raise
    except (PyJWTError, ValueError, TypeError) as exc:
        raise UnauthenticatedError("Invalid or expired id_token") from exc

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise UnauthenticatedError("Invalid or expired id_token")

    if claims.get("azp") is not None and claims.get("azp") != client_id:
        raise UnauthenticatedError("Invalid or expired id_token")

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip() or len(email.strip()) > 320:
        raise UnauthenticatedError("Invalid or expired id_token")

    email_verified = claims.get("email_verified")
    if email_verified is not None and not email_verified:
        raise UnauthenticatedError("Invalid or expired id_token")

    claims["email"] = email.strip().lower()
    return claims


def email_domain(email: str) -> str:
    """Return the lowercased part after the last ``@``."""
    return email.rsplit("@", 1)[-1].lower()


# --- token exchange -----------------------------------------------------------

async def exchange_code(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    allow_insecure: bool = False,
) -> dict:
    """Exchange an authorization code for tokens using client_secret_basic.

    The secret goes in the ``Authorization: Basic`` header, never in a query string or a
    log line.
    """
    _require_https(token_endpoint, allow_insecure=allow_insecure)

    async with _client() as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=httpx.BasicAuth(client_id, client_secret),
        )

    if response.status_code < 200 or response.status_code >= 300:
        error = None
        try:
            error = response.json().get("error")
        except Exception:
            pass
        logger.warning(
            "sso_token_exchange_failed",
            status_code=response.status_code,
            error=error,
        )
        raise UnauthenticatedError("Single sign-on failed")

    try:
        payload = response.json()
    except ValueError as exc:
        raise UnauthenticatedError("Single sign-on failed") from exc

    if not isinstance(payload, dict) or not payload.get("id_token"):
        raise UnauthenticatedError("Single sign-on failed")

    return payload
