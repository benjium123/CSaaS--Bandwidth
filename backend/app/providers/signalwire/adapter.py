"""SignalWire messaging adapter, written against the Twilio-compatible (LaML) API.

Deliberately Twilio-shaped rather than SignalWire-shaped (phase-3b DR-5): the Compatibility
API *is* Twilio's, so the same adapter serves Twilio later by changing the base URL. Two
carriers for the cost of one, and the difference is a string.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import structlog

from app.providers.domain import (
    CarrierCapabilities,
    CarrierError,
    CarrierEvent,
    OutboundMessage,
    SendResult,
)
from app.providers.signalwire import webhooks

log = structlog.get_logger("carrier.signalwire")

# Twilio-compatible error codes worth naming. 21610 is the one that matters most: it means
# the recipient replied STOP at the CARRIER, which our own ledger may not know about.
_UNREGISTERED = {"30032", "30034", "30038"}  # toll-free/10DLC not registered
_INVALID_REQUEST = {"21211", "21212", "21606", "21610", "21614"}
_RATE_LIMITED = {"20429", "14107"}


def classify(status_code: int, body: object) -> CarrierError:
    code = None
    detail = ""
    if isinstance(body, dict):
        raw_code = body.get("code")
        code = str(raw_code) if raw_code is not None else None
        detail = str(body.get("message") or "")[:255]

    if status_code in (401, 403):
        return CarrierError("auth", code, retryable=False, detail=detail or "credentials rejected")
    if status_code == 429 or code in _RATE_LIMITED:
        return CarrierError("rate_limited", code, retryable=True, detail=detail or "rate limited")
    if code in _UNREGISTERED:
        return CarrierError(
            "unregistered",
            code,
            retryable=False,
            detail=detail or "number is not registered for this traffic",
        )
    if status_code >= 500:
        return CarrierError(
            "carrier_transient", code, retryable=True, detail=detail or "server error"
        )
    if code in _INVALID_REQUEST or 400 <= status_code < 500:
        return CarrierError(
            "invalid_request", code, retryable=False, detail=detail or "request rejected"
        )
    return CarrierError("carrier_transient", code, retryable=True, detail=detail)


class SignalWireMessagingCarrier:
    name = "signalwire"

    capabilities = CarrierCapabilities(
        supports_cancel=True,
        supports_scheduled_send=False,
        sync_delivery_status=True,  # the create response already carries a status
        max_media_bytes=5_000_000,
        group_mms_toll_free=False,
    )

    def __init__(
        self,
        *,
        project_id: str,
        api_token: str,
        space_url: str,
        webhook_url: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.project_id = project_id
        self._api_token = api_token
        # Accept "example.signalwire.com" or a full URL; normalise to a base.
        space = space_url.strip().rstrip("/")
        if not space.startswith("http"):
            space = f"https://{space}"
        self.base_url = f"{space}/api/laml/2010-04-01/Accounts/{project_id}"
        self._webhook_url = webhook_url
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
        form: list[tuple[str, str]] = [
            ("From", msg.from_),
            ("To", msg.to),
            ("Body", msg.text),
        ]
        # MediaUrl repeats; it is not a JSON array.
        form.extend(("MediaUrl", url) for url in msg.media)

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/Messages.json",
                data=form,
                auth=(self.project_id, self._api_token),
            )
        except httpx.TransportError as exc:
            log.warning("carrier_unreachable", error=str(exc))
            return SendResult(
                "rejected", None, CarrierError("carrier_unreachable", None, True, str(exc)[:255])
            )

        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text[:255]}

        if resp.status_code in (200, 201, 202):
            sid = payload.get("sid") if isinstance(payload, dict) else None
            return SendResult("accepted", str(sid) if sid else None, None)

        error = classify(resp.status_code, payload)
        if error.category == "unregistered":
            log.error(
                "carrier_rejected_unregistered",
                carrier_code=error.carrier_code,
                detail=error.detail,
            )
        else:
            log.warning(
                "carrier_rejected", status=resp.status_code, category=error.category
            )
        return SendResult("rejected", None, error)

    def media_auth(self, url: str) -> tuple[str, str] | None:
        """SignalWire media needs the project credentials - and ONLY on our own space.

        Host-checked for the same reason as Bandwidth: sending API credentials to whatever
        host a webhook payload names would be a credential-leak primitive.
        """
        try:
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        if host.endswith("signalwire.com"):
            return (self.project_id, self._api_token)
        return None

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return webhooks.verify(headers, self._api_token, self._webhook_url, raw_body)

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return webhooks.parse(raw_body)
