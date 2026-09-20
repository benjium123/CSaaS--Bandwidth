"""Telnyx toll-free verification transport.

Maps the documented calls to/from HTTP only; holds no state, never logs or surfaces the
bearer key, request body, EIN or raw carrier text, and never retries a timed-out POST
(Telnyx may already have accepted it). Success replies are flat JSON with an ``id``.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
import structlog

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.telnyx import errors as tx_errors

log = structlog.get_logger("carrier.telnyx.tollfree_verification")

DEFAULT_BASE_URL = "https://api.telnyx.com/v2"
_REQUESTS_PATH = "/messaging_tollfree/verification/requests"


def _failure(operation: str, resp: httpx.Response) -> Exception:
    try:
        failed = tx_errors.classify(resp.status_code, resp.json())
    except ValueError:
        failed = tx_errors.classify(resp.status_code, {})
    code = failed.carrier_code
    suffix = f" (carrier code {code})" if code else ""
    log.warning("telnyx_tollfree_verification_http_error", operation=operation,
                status=resp.status_code, carrier_code=code, category=failed.category)
    message = f"Telnyx {operation} failed with HTTP {resp.status_code}{suffix}"
    return (FeatureUnavailableError if failed.retryable else ValidationFailedError)(message)


def _validated(operation: str, resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise FeatureUnavailableError(f"Telnyx {operation} returned an unexpected response")
    if not body.get("id"):
        raise ValidationFailedError("Telnyx returned no request identifier in its response")
    return body


class TelnyxTollfreeVerificationClient:
    """Transport for the Telnyx toll-free verification endpoints."""

    def __init__(self, *, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def create(self, payload: dict) -> dict:
        """POST the verification request; returns the flat request object."""
        if not isinstance(payload, dict) or not payload:
            raise ValidationFailedError("Telnyx toll-free verification needs a request body")
        return await self._send("POST", _REQUESTS_PATH, "toll-free submission", payload)

    async def get(self, request_id: str) -> dict:
        """GET the verification request; returns the flat request object."""
        path = f"{_REQUESTS_PATH}/{quote(str(request_id), safe='')}"
        return await self._send("GET", path, "toll-free lookup", None)

    async def _send(self, method: str, path: str, operation: str, payload: dict | None) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        kwargs: dict = {"headers": {"Authorization": f"Bearer {self.api_key}"}}
        if payload is not None:
            kwargs["json"] = payload
        try:
            resp = await self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            log.warning("telnyx_tollfree_verification_timeout", operation=operation, method=method)
            note = "; not retried, outcome unknown" if method.upper() == "POST" else ""
            raise FeatureUnavailableError(f"Telnyx {operation} timed out{note}") from exc
        except httpx.TransportError as exc:
            log.warning("telnyx_tollfree_verification_unreachable",
                        operation=operation, error=type(exc).__name__)
            raise FeatureUnavailableError(f"Telnyx {operation} is unreachable") from exc
        if resp.status_code >= 400:
            raise _failure(operation, resp)
        return _validated(operation, resp)
