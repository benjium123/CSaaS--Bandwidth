"""Telnyx toll-free verification transport.

Maps the documented calls to/from HTTP only; holds no state, never logs or surfaces the
bearer key, request body, EIN or raw carrier text, and never retries a timed-out POST
(Telnyx may already have accepted it). Create and get replies are flat JSON with an
``id``; list replies are flat JSON with ``records`` and ``total_records``.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

import httpx
import structlog

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.telnyx import errors as tx_errors

log = structlog.get_logger("carrier.telnyx.tollfree_verification")

DEFAULT_BASE_URL = "https://api.telnyx.com/v2"
_REQUESTS_PATH = "/messaging_tollfree/verification/requests"
_LIST_FILTERS = ("date_start", "date_end", "status", "phone_number", "business_name")


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


def _validated_list(operation: str, resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise FeatureUnavailableError(f"Telnyx {operation} returned an unexpected response")
    records = body.get("records")
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValidationFailedError("Telnyx returned no verification records in its response")
    total_records = body.get("total_records")
    if isinstance(total_records, bool) or not isinstance(total_records, int) or total_records < 0:
        raise ValidationFailedError("Telnyx returned an invalid record count in its response")
    return {"records": records, "total_records": total_records}


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationFailedError(
            f"Telnyx toll-free verification {name} must be a positive integer")
    return value


def _clean_filters(values: dict[str, object]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValidationFailedError(
                f"Telnyx toll-free verification filter {name} must be a string")
        stripped = value.strip()
        if stripped:
            cleaned[name] = stripped
    return cleaned


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

    async def list_requests(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        date_start: str | None = None,
        date_end: str | None = None,
        status: str | None = None,
        phone_number: str | None = None,
        business_name: str | None = None,
    ) -> dict:
        """GET one page of verification requests; returns ``records``/``total_records``.

        Read-only reconciliation helper: exactly one authenticated request to the
        requests collection, with no retry and nothing about the filters logged.
        """
        params: dict[str, str | int] = {
            "page": _positive_int("page", page),
            "page_size": _positive_int("page_size", page_size),
        }
        params.update(_clean_filters({
            "date_start": date_start,
            "date_end": date_end,
            "status": status,
            "phone_number": phone_number,
            "business_name": business_name,
        }))
        return await self._send("GET", _REQUESTS_PATH, "toll-free listing", None,
                                params=params, validate=_validated_list)

    async def _send(
        self,
        method: str,
        path: str,
        operation: str,
        payload: dict | None,
        *,
        params: dict | None = None,
        validate: Callable[[str, httpx.Response], dict] = _validated,
    ) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        kwargs: dict = {"headers": {"Authorization": f"Bearer {self.api_key}"}}
        if payload is not None:
            kwargs["json"] = payload
        if params is not None:
            kwargs["params"] = params
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
        return validate(operation, resp)
