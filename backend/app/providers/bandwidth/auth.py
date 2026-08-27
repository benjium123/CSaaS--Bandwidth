"""Bandwidth OAuth2: exchange a Client ID / Client Secret for a Bearer token.

Bandwidth moved to OAuth2 client-credentials. The pair issued in the dashboard
(`CLI-...` + secret) is NOT accepted as HTTP Basic on the Voice or Messaging APIs -
verified against a live account, where Basic returns 401 on every host while the same
pair mints a working token here. Older accounts still have API-user credentials that
DO work as Basic, so both modes are supported and the mode is configuration, not a guess.

Two properties matter:

* **The token is cached and shared.** It lasts an hour; fetching one per request would
  add a round trip to every call and invite rate limiting on the token endpoint.
* **A 401 invalidates it exactly once.** Tokens can die early - revoked, rotated, or the
  clock drifts - so a single forced refresh converts a mysterious mid-call 401 into a
  self-healing retry. Retrying forever would turn a genuinely bad credential into a hot
  loop against the identity server, so it is one retry, then the error stands.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog

log = structlog.get_logger("carrier.bandwidth.auth")

TOKEN_URL = "https://api.bandwidth.com/api/v1/oauth2/token"
#: Refresh this many seconds before the token actually expires, so a request in flight
#: never races the expiry.
EXPIRY_MARGIN_SECONDS = 120


class BandwidthAuthError(RuntimeError):
    """The token endpoint refused the client credentials."""


class BandwidthTokenProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_url: str = TOKEN_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Without the lock, N concurrent calls on a cold cache all fetch their own token.
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def invalidate(self) -> None:
        """Drop the cached token. The next call fetches a fresh one."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._expires_at:
            return self._token

        async with self._lock:
            # Another waiter may have refreshed while we queued.
            now = time.monotonic()
            if self._token and now < self._expires_at:
                return self._token

            client = await self._get_client()
            try:
                resp = await client.post(
                    self._token_url,
                    auth=(self._client_id, self._client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    content="grant_type=client_credentials",
                )
            except httpx.TransportError as exc:
                raise BandwidthAuthError(
                    f"Could not reach Bandwidth to authenticate: {exc}"
                ) from exc

            if resp.status_code != 200:
                detail = ""
                try:
                    body = resp.json()
                    detail = str(body.get("message") or body.get("error") or "")[:200]
                except ValueError:
                    detail = resp.text[:200]
                raise BandwidthAuthError(
                    f"Bandwidth rejected the client credentials (HTTP {resp.status_code}). "
                    f"{detail}"
                )

            payload = resp.json()
            token = payload.get("access_token")
            if not token:
                raise BandwidthAuthError("Bandwidth returned no access_token")

            expires_in = int(payload.get("expires_in") or 3600)
            self._token = str(token)
            self._expires_at = time.monotonic() + max(60, expires_in - EXPIRY_MARGIN_SECONDS)
            log.info("bandwidth_token_acquired", expires_in=expires_in)
            return self._token

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
