"""Bandwidth messaging adapter.

Direct REST, no SDK (phase-1-plan DR-3): the surface P1 needs is exactly one endpoint, and
the unified bandwidth-sdk would sit between us and httpx.MockTransport while dragging a
wildly version-skewed dependency along for one call.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

import httpx
import structlog

from app.providers.bandwidth import errors as bw_errors
from app.providers.bandwidth import webhooks
from app.providers.bandwidth.auth import BandwidthTokenProvider
from app.providers.bandwidth.numbers import BandwidthNumberProviderMixin
from app.providers.bandwidth.voice import BandwidthVoiceMixin
from app.providers.domain import (
    CarrierCapabilities,
    CarrierEvent,
    OutboundMessage,
    SendResult,
)

log = structlog.get_logger("carrier.bandwidth")

DEFAULT_BASE_URL = "https://messaging.bandwidth.com/api/v2"


class BandwidthMessagingCarrier(BandwidthVoiceMixin, BandwidthNumberProviderMixin):
    name = "bandwidth"

    def __init__(
        self,
        *,
        account_id: str,
        api_username: str,
        api_password: str,
        application_id: str,
        # Constructor default is BASIC so directly-constructed adapters keep their
        # existing behaviour. Production never relies on this default: build_registry
        # always passes settings.bandwidth_auth_mode, whose default is "oauth2" - the
        # model current Bandwidth accounts actually use.
        auth_mode: str = "basic",
        webhook_username: str = "",
        webhook_password: str = "",
        base_url: str = DEFAULT_BASE_URL,
        # P18: the Bandwidth Dashboard/IRIS "Site" (sub-account) id a number order is
        # placed under - required by BandwidthNumberProviderMixin.order_number, not by
        # messaging. Defaults empty; the caller (build_registry for the env carrier, or
        # the P17 per-org registry for a DB-backed one) is responsible for passing the
        # configured/credentialed value.
        site_id: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_id = account_id
        self.application_id = application_id
        self.base_url = base_url.rstrip("/")
        self._webhook_username = webhook_username
        self._webhook_password = webhook_password
        self._auth = (api_username, api_password)
        self.site_id = site_id
        # OAuth2 when the dashboard issued a Client ID / Client Secret (the current
        # Bandwidth model); Basic for legacy API-user credentials. Explicit config, not a
        # guess: sniffing the credential shape would silently change how we authenticate
        # the day Bandwidth changes its id format.
        self.auth_mode = auth_mode
        self._tokens = (
            # Share the adapter's client so an injected transport (tests, and any future
            # proxy/timeouts policy) also governs the token exchange. A provider with its
            # own client would quietly make REAL network calls under a mocked adapter.
            BandwidthTokenProvider(api_username, api_password, client=client)
            if auth_mode == "oauth2"
            else None
        )
        self._client = client
        self._owns_client = client is None

    # -- capabilities are DECLARED, never discovered by trial -----------------------
    # INSTANCE level, not class level: whether this deployment may send MESSAGES depends
    # on whether it was given a messaging application id, which differs per account. A
    # Bandwidth trial that only has voice is a real and supported configuration.
    @property
    def capabilities(self) -> CarrierCapabilities:
        return CarrierCapabilities(
            supports_messaging=bool((self.application_id or "").strip()),
            supports_cancel=False,
            supports_scheduled_send=False,
            sync_delivery_status=False,
            max_media_bytes=3_750_000,
            group_mms_toll_free=False,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                auth=self._auth if self._tokens is None else None, timeout=10.0
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send_message(self, msg: OutboundMessage) -> SendResult:
        url = f"{self.base_url}/users/{self.account_id}/messages"
        body: dict = {
            "to": [msg.to],  # Bandwidth wants a LIST even for one recipient
            "from": msg.from_,
            "text": msg.text,
            "applicationId": self.application_id,
            "tag": msg.tag,
        }
        if msg.media:
            body["media"] = list(msg.media)

        client = await self._get_client()
        try:
            resp = await client.post(url, json=body, **(await self.auth_kwargs()))
            if resp.status_code == 401 and self._tokens is not None:
                # The token may have been revoked or rotated. Refresh once - never loop,
                # or a genuinely bad credential becomes a hot loop on the token endpoint.
                self.invalidate_token()
                resp = await client.post(url, json=body, **(await self.auth_kwargs()))
        except httpx.TransportError as exc:
            log.warning("carrier_unreachable", error=str(exc))
            return SendResult("rejected", None, bw_errors.unreachable(str(exc)))

        if resp.status_code == 202:
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            provider_id = payload.get("id") if isinstance(payload, dict) else None
            return SendResult("accepted", str(provider_id) if provider_id else None, None)

        try:
            payload = resp.json()
        except ValueError:
            payload = {"description": resp.text[:255]}

        error = bw_errors.classify(resp.status_code, payload)
        if error.category == "unregistered":
            # Track-R tripwire: the number is not attached to a 10DLC campaign.
            log.error(
                "carrier_rejected_unregistered",
                carrier_code=error.carrier_code,
                detail=error.detail,
            )
        else:
            log.warning(
                "carrier_rejected",
                status=resp.status_code,
                category=error.category,
                carrier_code=error.carrier_code,
            )
        return SendResult("rejected", None, error)

    async def auth_kwargs(self) -> dict:
        """How to authenticate ONE outbound request: Bearer header or Basic tuple."""
        if self._tokens is None:
            return {"auth": self._auth}
        if self._tokens._client is None:
            self._tokens._client = await self._get_client()
            self._tokens._owns_client = False
        return {"headers": {"Authorization": f"Bearer {await self._tokens.token()}"}}

    def invalidate_token(self) -> None:
        if self._tokens is not None:
            self._tokens.invalidate()

    def media_auth(self, url: str) -> tuple[str, str] | None:
        """Credentials for fetching carrier-hosted media - and ONLY for Bandwidth hosts.

        Inbound MMS media on Bandwidth needs Basic auth. Sending our API credentials to a
        foreign host because a payload said so would be a credential-leak primitive, so the
        host is checked rather than trusted.
        """
        try:
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        if not host.endswith("bandwidth.com"):
            return None
        # Under OAuth2 there is no Basic pair to hand back; media_headers() carries the
        # Bearer token instead, and the fetcher prefers it when present.
        return self._auth if self._tokens is None else None

    async def media_headers(self, url: str) -> dict | None:
        host = (urlparse(url).hostname or "").lower()
        if not host.endswith("bandwidth.com") or self._tokens is None:
            return None
        return {"Authorization": f"Bearer {await self._tokens.token()}"}

    # -- webhook surface delegates to the pure module ------------------------------
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return webhooks.verify(headers, self._webhook_username, self._webhook_password)

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return webhooks.parse(raw_body)
