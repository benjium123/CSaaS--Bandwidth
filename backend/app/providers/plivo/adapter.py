"""Plivo messaging adapter.

Direct REST, no SDK (same reasoning as Bandwidth/Telnyx: the surface needed is a handful of
endpoints, and dragging a vendor SDK in just makes httpx.MockTransport tests harder).

Plivo is the CAL's proof carrier (phase-9b DR-3): Basic auth-id/auth-token credentials
(not a bearer token or username/password pair), a V3 signature scheme unlike either
sibling, its own XML dialect, and a prose-only error taxonomy. None of that required
touching app/providers/domain.py, numbers.py or voice.py - see voice.py's module
docstring for the two places composing against the frozen VoiceCarrier protocol was
genuinely awkward.
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
from app.providers.plivo import errors as pl_errors
from app.providers.plivo import webhooks
from app.providers.plivo.numbers import PlivoNumberProviderMixin
from app.providers.plivo.voice import PlivoVoiceMixin

log = structlog.get_logger("carrier.plivo")

DEFAULT_BASE_URL_TEMPLATE = "https://api.plivo.com/v1/Account/{auth_id}"


class PlivoMessagingCarrier(PlivoNumberProviderMixin, PlivoVoiceMixin):
    """Messaging + provisioning + voice. One auth-id/auth-token pair, one base URL."""

    name = "plivo"

    capabilities = CarrierCapabilities(
        supports_cancel=False,
        supports_scheduled_send=False,
        sync_delivery_status=False,
        max_media_bytes=5_000_000,
        group_mms_toll_free=False,
    )

    def __init__(
        self,
        *,
        auth_id: str,
        auth_token: str,
        powerpack_uuid: str = "",
        webhook_url: str = "",
        base_url: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.auth_id = auth_id
        self._auth_token = auth_token
        self._auth = (auth_id, auth_token)
        self.powerpack_uuid = powerpack_uuid
        self._webhook_url = webhook_url
        self.base_url = (
            base_url or DEFAULT_BASE_URL_TEMPLATE.format(auth_id=auth_id)
        ).rstrip("/")
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
            "dst": msg.to,
            "text": msg.text,
            "type": "mms" if msg.media else "sms",
        }
        if self.powerpack_uuid:
            # Powerpack pooled sending REPLACES an explicit sender - Plivo rejects a
            # request that sends both.
            body["powerpack_uuid"] = self.powerpack_uuid
        else:
            body["src"] = msg.from_
        if msg.media:
            body["media_urls"] = list(msg.media)
        if self._webhook_url:
            body["url"] = self._webhook_url

        client = await self._get_client()
        try:
            resp = await client.post(f"{self.base_url}/Message/", json=body, auth=self._auth)
        except httpx.TransportError as exc:
            log.warning("carrier_unreachable", error=str(exc))
            return SendResult("rejected", None, pl_errors.unreachable(str(exc)))

        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text[:255]}

        if resp.status_code in (200, 201, 202):
            message_uuid = payload.get("message_uuid") if isinstance(payload, dict) else None
            # Plivo's real shape: message_uuid is a LIST (one per recipient), even for a
            # single destination.
            if isinstance(message_uuid, list):
                provider_id = message_uuid[0] if message_uuid else None
            else:
                provider_id = message_uuid
            return SendResult("accepted", str(provider_id) if provider_id else None, None)

        error = pl_errors.classify(resp.status_code, payload)
        if error.category == "unregistered":
            log.error("carrier_rejected_unregistered", detail=error.detail)
        else:
            log.warning("carrier_rejected", status=resp.status_code, category=error.category)
        return SendResult("rejected", None, error)

    def media_auth(self, url: str) -> tuple[str, str] | None:
        """Plivo MMS media is hosted on `.plivo.com` and needs Basic auth to fetch - and
        ONLY on that host, checked (not assumed) for the same credential-leak reason as
        every sibling adapter."""
        try:
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        if host == "plivo.com" or host.endswith(".plivo.com"):
            return self._auth
        return None

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return webhooks.verify(headers, self._auth_token, self._webhook_url)

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return webhooks.parse(raw_body)
