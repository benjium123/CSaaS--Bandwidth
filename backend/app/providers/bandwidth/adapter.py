"""Bandwidth messaging adapter.

Direct REST, no SDK (phase-1-plan DR-3): the surface P1 needs is exactly one endpoint, and
the unified bandwidth-sdk would sit between us and httpx.MockTransport while dragging a
wildly version-skewed dependency along for one call.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import structlog

from app.providers.bandwidth import errors as bw_errors
from app.providers.bandwidth import webhooks
from app.providers.bandwidth.voice import BandwidthVoiceMixin
from app.providers.domain import (
    CarrierCapabilities,
    CarrierEvent,
    OutboundMessage,
    SendResult,
)

log = structlog.get_logger("carrier.bandwidth")

DEFAULT_BASE_URL = "https://messaging.bandwidth.com/api/v2"


class BandwidthMessagingCarrier(BandwidthVoiceMixin):
    name = "bandwidth"

    def __init__(
        self,
        *,
        account_id: str,
        api_username: str,
        api_password: str,
        application_id: str,
        webhook_username: str = "",
        webhook_password: str = "",
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_id = account_id
        self.application_id = application_id
        self.base_url = base_url.rstrip("/")
        self._webhook_username = webhook_username
        self._webhook_password = webhook_password
        self._auth = (api_username, api_password)
        self._client = client
        self._owns_client = client is None

    # -- capabilities are DECLARED, never discovered by trial -----------------------
    capabilities = CarrierCapabilities(
        supports_cancel=False,
        supports_scheduled_send=False,
        sync_delivery_status=False,
        max_media_bytes=3_750_000,
        group_mms_toll_free=False,
    )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(auth=self._auth, timeout=10.0)
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
            resp = await client.post(url, json=body, auth=self._auth)
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

    # NOTE: BandwidthMessagingCarrier deliberately does NOT implement NumberProvider.
    # Bandwidth ordering runs through the IRIS/Dashboard XML API and needs a SiteId and
    # SipPeerId that this account has not been given, plus credentials that currently
    # return 401 (blocker R1). Writing an integration we cannot execute even once would
    # produce code that looks finished and fails on first contact - `as_provider` raises a
    # clear FeatureUnavailableError instead, and numbers can be added by hand meanwhile.

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
        return self._auth if host.endswith("bandwidth.com") else None

    # -- webhook surface delegates to the pure module ------------------------------
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return webhooks.verify(headers, self._webhook_username, self._webhook_password)

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return webhooks.parse(raw_body)
