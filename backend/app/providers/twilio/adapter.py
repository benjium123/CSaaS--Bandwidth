"""Twilio messaging adapter.

Twilio gets its OWN package (phase-9b DR-2). SignalWire's adapter (`providers/signalwire/`)
was deliberately built against the Twilio-compatible (LaML) API so the same shape could
serve Twilio later "by changing the base URL" - but Twilio does not subclass or import
SignalWire. Twilio's own error table, signature verification and capabilities live here so a
future Twilio-only fix can never become a SignalWire regression, and vice versa: Twilio is
the original API, SignalWire is the clone, and only the request/response SHAPE is shared.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

import httpx
import structlog

from app.providers.domain import (
    CarrierCapabilities,
    CarrierEvent,
    OutboundMessage,
    SendResult,
)
from app.providers.twilio import errors as tw_errors
from app.providers.twilio import webhooks
from app.providers.twilio.numbers import TwilioNumberProviderMixin
from app.providers.twilio.voice import TwilioVoiceMixin

log = structlog.get_logger("carrier.twilio")

DEFAULT_BASE_URL = "https://api.twilio.com/2010-04-01"


class TwilioMessagingCarrier(TwilioNumberProviderMixin, TwilioVoiceMixin):
    """Messaging + provisioning + voice. One account SID / auth token - splitting them into
    separate objects would mean two holders of the same credential for no gain (same
    reasoning as `TelnyxMessagingCarrier`)."""

    name = "twilio"

    capabilities = CarrierCapabilities(
        supports_cancel=True,
        supports_scheduled_send=True,
        sync_delivery_status=False,
        max_media_bytes=5_000_000,
        group_mms_toll_free=False,
    )

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        messaging_service_sid: str = "",
        default_number: str = "",
        webhook_url: str = "",
        voice_webhook_url: str = "",
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_sid = account_sid
        self._auth_token = auth_token
        self._auth = (account_sid, auth_token)
        self.messaging_service_sid = messaging_service_sid
        self.default_number = default_number
        # verify_webhook/verify_voice_webhook sign against a REGISTERED url, never a
        # reconstructed Host header (same reasoning as SignalWire's webhooks module).
        self._webhook_url = webhook_url
        self._voice_webhook_url = voice_webhook_url or webhook_url
        self.base_url = f"{base_url.rstrip('/')}/Accounts/{account_sid}"
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
        form: list[tuple[str, str]] = [("To", msg.to)]
        if self.messaging_service_sid:
            # MessagingServiceSid and From are mutually exclusive on Twilio's API; prefer
            # the messaging service when one is configured so number selection is Twilio's.
            form.append(("MessagingServiceSid", self.messaging_service_sid))
        else:
            form.append(("From", msg.from_))
        form.append(("Body", msg.text))
        form.extend(("MediaUrl", url) for url in msg.media)

        client = await self._get_client()
        try:
            # Encoded by hand (rather than httpx's `data=`) so a repeated key - MediaUrl,
            # once per attachment - survives: `data=` collapses a plain list-of-tuples body
            # incorrectly on this httpx version, and a dict cannot express a repeated key.
            resp = await client.post(
                f"{self.base_url}/Messages.json",
                content=urlencode(form),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            log.warning("carrier_unreachable", error=str(exc))
            return SendResult("rejected", None, tw_errors.unreachable(str(exc)))

        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text[:255]}

        if resp.status_code in (200, 201):
            sid = payload.get("sid") if isinstance(payload, dict) else None
            return SendResult("accepted", str(sid) if sid else None, None)

        error = tw_errors.classify(resp.status_code, payload)
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
        """Twilio media/recording URLs need Basic auth - and ONLY on Twilio's own host.

        Host-checked, never trusted from the payload: attaching our credentials to whatever
        host a webhook happened to name would be a credential-leak primitive.
        """
        try:
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        return self._auth if (host == "twilio.com" or host.endswith(".twilio.com")) else None

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return webhooks.verify(headers, self._auth_token, raw_body, self._webhook_url)

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return webhooks.parse(raw_body)
