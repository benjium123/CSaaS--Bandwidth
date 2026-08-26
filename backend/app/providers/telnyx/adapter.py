"""Telnyx messaging adapter.

Direct REST, no SDK - same reasoning as Bandwidth (phase-1-plan DR-3).

Telnyx is the carrier the CAL was *shaped* after (ARCHITECTURE D2), so this adapter is the
thinnest of the three: event in, async command out, no document return.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import structlog

from app.providers.domain import (
    CarrierCapabilities,
    CarrierEvent,
    OutboundMessage,
    SendResult,
)
from app.providers.telnyx import errors as tx_errors
from app.providers.telnyx import webhooks

log = structlog.get_logger("carrier.telnyx")

DEFAULT_BASE_URL = "https://api.telnyx.com/v2"


class TelnyxMessagingCarrier:
    name = "telnyx"

    # Telnyx accepts a scheduled send and can cancel a queued message - Bandwidth cannot.
    # Declared, so the router can prefer it for scheduled work instead of discovering the
    # difference at 3am.
    capabilities = CarrierCapabilities(
        supports_cancel=True,
        supports_scheduled_send=True,
        sync_delivery_status=False,
        max_media_bytes=1_000_000,
        group_mms_toll_free=False,
    )

    def __init__(
        self,
        *,
        api_key: str,
        messaging_profile_id: str = "",
        public_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.messaging_profile_id = messaging_profile_id
        self._public_key = public_key
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send_message(self, msg: OutboundMessage) -> SendResult:
        body: dict = {
            "from": msg.from_,
            "to": msg.to,  # Telnyx takes a bare string, NOT Bandwidth's list
            "text": msg.text,
        }
        if self.messaging_profile_id:
            body["messaging_profile_id"] = self.messaging_profile_id
        if msg.media:
            body["media_urls"] = list(msg.media)

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/messages",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.TransportError as exc:
            log.warning("carrier_unreachable", error=str(exc))
            return SendResult("rejected", None, tx_errors.unreachable(str(exc)))

        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text[:255]}

        if resp.status_code in (200, 201, 202):
            data = payload.get("data") if isinstance(payload, dict) else None
            provider_id = data.get("id") if isinstance(data, dict) else None
            return SendResult("accepted", str(provider_id) if provider_id else None, None)

        error = tx_errors.classify(resp.status_code, payload)
        if error.category == "unregistered":
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

    def media_auth(self, url: str) -> tuple[str, str] | None:
        """Telnyx hosts inbound MMS on public, unauthenticated URLs.

        Returning None is the whole answer, and it is the SAFE answer: never attach
        credentials to a fetch just because a webhook payload named a host.
        """
        return None

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return webhooks.verify(headers, self._public_key, raw_body)

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return webhooks.parse(raw_body)
