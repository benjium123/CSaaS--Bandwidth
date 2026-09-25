"""Telnyx 10DLC registration transport.

The local registration state machine owns *when* a brand or campaign is submitted and
*what* changes locally as a result; this module owns only *how* the documented Telnyx
10DLC HTTP calls are made, how their replies are validated, and which of the app's
existing user-safe errors a failure becomes. It keeps no registration state of its own.

It is a standalone client rather than another mixin on ``TelnyxMessagingCarrier`` on
purpose: 10DLC submission is a rare, operator-driven batch workflow, and keeping it
separate lets the state machine depend on a narrow, injectable transport (an
``httpx.MockTransport`` in tests) instead of the whole messaging adapter. The httpx
client is injected the same way the adapter injects one, so the client is fully
testable offline.

Safety rules baked in here:

- Bearer keys, whole request bodies, EINs and raw carrier response text never reach logs
  or exception messages.
- A timed-out POST is never retried automatically - Telnyx may already have accepted it,
  so re-sending could double-submit a brand or campaign. This client does a single
  attempt for every request and reports the uncertainty to the caller instead.

Documented endpoints (base ``https://api.telnyx.com/v2``), whose success responses are
flat top-level JSON objects (NOT wrapped in a ``data`` envelope):
``POST /10dlc/brand``, ``GET /10dlc/brand/{brandId}``,
``POST /10dlc/campaignBuilder``, ``GET /10dlc/campaign/{campaignId}`` and
``POST /10dlc/phone_number_campaigns``.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
import structlog

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.telnyx import errors as tx_errors

log = structlog.get_logger("carrier.telnyx.registration")

DEFAULT_BASE_URL = "https://api.telnyx.com/v2"

# Documented 10DLC paths (https://developers.telnyx.com/api-reference).
_BRAND_PATH = "/10dlc/brand"
_CAMPAIGN_BUILDER_PATH = "/10dlc/campaignBuilder"
_CAMPAIGN_PATH = "/10dlc/campaign"
_PHONE_NUMBER_CAMPAIGN_PATH = "/10dlc/phone_number_campaigns"


def _timeout_message(operation: str, method: str) -> str:
    if method.upper() == "POST":
        # A timed-out write is ambiguous: the carrier may have applied it. Say so, and
        # do not retry - see the module docstring.
        return (
            f"Telnyx {operation} timed out; the request may or may not have been "
            "accepted and was not retried"
        )
    return f"Telnyx {operation} timed out"


def _require_payload(payload: object, operation: str) -> None:
    if not isinstance(payload, dict) or not payload:
        raise ValidationFailedError(f"Telnyx {operation} needs a non-empty request body")


def _safe_json(resp: httpx.Response) -> object:
    """Parse the carrier body for error classification, or return {} - never surface raw
    carrier text."""
    try:
        return resp.json()
    except ValueError:
        return {}


def _http_failure(operation: str, resp: httpx.Response) -> Exception:
    """Turn a non-2xx reply into one of the app's user-safe errors.

    The carrier's own ``detail``/``title`` is deliberately NOT echoed: it is raw carrier
    text and can contain an EIN, a phone number or a fragment of the request. Only the
    HTTP status and the short carrier ``code`` are reported.
    """
    carrier_error = tx_errors.classify(resp.status_code, _safe_json(resp))
    code = carrier_error.carrier_code
    suffix = f" (carrier code {code})" if code else ""
    log.warning(
        "telnyx_registration_http_error",
        operation=operation,
        status=resp.status_code,
        carrier_code=code,
        category=carrier_error.category,
    )
    message = f"Telnyx {operation} failed with HTTP {resp.status_code}{suffix}"
    if carrier_error.retryable:
        return FeatureUnavailableError(message)
    return ValidationFailedError(message)


def _parse_body(operation: str, resp: httpx.Response) -> dict:
    """Validate a 2xx 10DLC reply.

    10DLC success responses are FLAT top-level JSON objects (``brandId``, ``campaignId``,
    ... sit at the top level - there is no ``{"data": {...}}`` envelope). A body that is
    not JSON, or is JSON but not an object, is a response we cannot trust.
    """
    try:
        body = resp.json()
    except ValueError:
        raise FeatureUnavailableError(
            f"Telnyx {operation} returned an unexpected response"
        ) from None
    if not isinstance(body, dict):
        raise FeatureUnavailableError(f"Telnyx {operation} returned an unexpected response")
    return body


def _require_id(data: dict, keys: tuple[str, ...], label: str) -> dict:
    """A 2xx without the identifier we need to track is not a usable success."""
    for key in keys:
        if data.get(key):
            return data
    raise ValidationFailedError(f"Telnyx returned no {label} identifier in its response")


class TelnyxRegistrationClient:
    """Transport for the Telnyx 10DLC brand/campaign endpoints.

    Request bodies are supplied by the caller using Telnyx's documented keys and are
    forwarded verbatim - this client never invents a value for a field the caller did not
    send. Read methods return the flat validated response object; write methods
    additionally guarantee the tracked identifier is present.
    """

    name = "telnyx"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
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

    # -- brands ---------------------------------------------------------------

    async def create_brand(self, payload: dict) -> dict:
        """POST /10dlc/brand. Returns the flat brand object (contains ``brandId``)."""
        _require_payload(payload, "brand submission")
        data = await self._post(_BRAND_PATH, payload, "brand submission")
        return _require_id(data, ("brandId", "id"), "brand")

    async def get_brand(self, brand_id: str) -> dict:
        """GET /10dlc/brand/{brandId}. Returns the flat brand object (carries status)."""
        data = await self._get(
            f"{_BRAND_PATH}/{quote(str(brand_id), safe='')}", "brand lookup"
        )
        return _require_id(data, ("brandId", "id"), "brand")

    async def trigger_sms_otp(self, brand_id: str, *, pin_sms: str, success_sms: str) -> dict:
        """POST /10dlc/brand/{brandId}/smsOtp: text a sole proprietor their 6-digit PIN.
        Calling it again re-sends a fresh PIN (each lives 24 hours)."""
        return await self._post(
            f"{_BRAND_PATH}/{quote(str(brand_id), safe='')}/smsOtp",
            {"pinSms": pin_sms, "successSms": success_sms},
            "sole proprietor PIN",
        )

    async def verify_sms_otp(self, brand_id: str, pin: str) -> dict:
        """PUT /10dlc/brand/{brandId}/smsOtp: submit the PIN the owner received."""
        return await self._send(
            "PUT",
            f"{_BRAND_PATH}/{quote(str(brand_id), safe='')}/smsOtp",
            operation="sole proprietor PIN check",
            payload={"otpPin": pin},
        )

    async def list_brands(self, **filters: str) -> list[dict]:
        """GET /10dlc/brand with filters (e.g. displayName). Returns the records."""
        query = "&".join(f"{quote(k)}={quote(str(v), safe='')}" for k, v in filters.items())
        data = await self._get(f"{_BRAND_PATH}?recordsPerPage=50&{query}", "brand search")
        records = data.get("records")
        return records if isinstance(records, list) else []

    async def list_campaigns(self, brand_id: str) -> list[dict]:
        """GET /10dlc/campaign?brandId=... Returns the records."""
        data = await self._get(
            f"{_CAMPAIGN_PATH}?recordsPerPage=50&brandId={quote(str(brand_id), safe='')}",
            "campaign search",
        )
        records = data.get("records")
        return records if isinstance(records, list) else []

    # -- campaigns ------------------------------------------------------------

    async def create_campaign(self, payload: dict) -> dict:
        """POST /10dlc/campaignBuilder. Returns the flat campaign object."""
        _require_payload(payload, "campaign submission")
        data = await self._post(_CAMPAIGN_BUILDER_PATH, payload, "campaign submission")
        return _require_id(data, ("campaignId", "id"), "campaign")

    async def get_campaign(self, campaign_id: str) -> dict:
        """GET /10dlc/campaign/{campaignId}. Returns the flat campaign object."""
        data = await self._get(
            f"{_CAMPAIGN_PATH}/{quote(str(campaign_id), safe='')}", "campaign lookup"
        )
        return _require_id(data, ("campaignId", "id"), "campaign")

    # -- phone number campaigns ----------------------------------------------

    async def assign_phone_number(self, payload: dict) -> dict:
        """POST /10dlc/phone_number_campaigns.

        ``payload`` must carry the documented ``phoneNumber`` and ``campaignId`` keys;
        the caller supplies both. Returns the flat assignment object.
        """
        _require_payload(payload, "phone-number assignment")
        data = await self._post(
            _PHONE_NUMBER_CAMPAIGN_PATH, payload, "phone-number assignment"
        )
        return _require_id(data, ("phoneNumber", "phone_number"), "phone-number")

    # -- transport ------------------------------------------------------------

    async def _post(self, path: str, payload: dict, operation: str) -> dict:
        return await self._send("POST", path, operation=operation, payload=payload)

    async def _get(self, path: str, operation: str) -> dict:
        return await self._send("GET", path, operation=operation, payload=None)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        payload: dict | None,
    ) -> dict:
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self.api_key}"}
        kwargs: dict = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        try:
            resp = await client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            log.warning("telnyx_registration_timeout", operation=operation, method=method)
            raise FeatureUnavailableError(_timeout_message(operation, method)) from exc
        except httpx.TransportError as exc:
            log.warning(
                "telnyx_registration_unreachable",
                operation=operation,
                error=type(exc).__name__,
            )
            raise FeatureUnavailableError(f"Telnyx {operation} is unreachable") from exc

        if resp.status_code >= 400:
            raise _http_failure(operation, resp)
        return _parse_body(operation, resp)
