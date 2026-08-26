"""Telnyx error classification.

Telnyx returns ``{"errors": [{"code": "40300", "title": ..., "detail": ...}]}``. Codes are
strings, and the 10DLC/registration family is what Track R watches.
"""

from __future__ import annotations

from typing import Any

from app.providers.domain import CarrierError

# "This request is wrong; retrying changes nothing."
_INVALID_REQUEST = {"40001", "40002", "40003", "40005", "20001"}
# Not registered to a campaign / brand. The Track-R tripwire.
_UNREGISTERED = {"40300", "40301", "40302", "40008"}
_RATE_LIMITED = {"40004", "10002"}


def _extract(body: Any) -> tuple[str | None, str]:
    if not isinstance(body, dict):
        return None, ""
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        code = first.get("code")
        detail = str(first.get("detail") or first.get("title") or "")[:255]
        return (str(code) if code is not None else None), detail
    return None, str(body.get("message") or "")[:255]


def classify(status_code: int, body: Any) -> CarrierError:
    code, detail = _extract(body)

    if status_code in (401, 403):
        return CarrierError(
            "auth", code, retryable=False, detail=detail or "telnyx rejected credentials"
        )

    if status_code == 429 or code in _RATE_LIMITED:
        return CarrierError("rate_limited", code, retryable=True, detail=detail or "rate limited")

    if code in _UNREGISTERED:
        return CarrierError(
            "unregistered",
            code,
            retryable=False,
            detail=detail or "number is not attached to a 10DLC campaign",
        )

    if status_code >= 500:
        return CarrierError(
            "carrier_transient", code, retryable=True, detail=detail or "telnyx server error"
        )

    if code in _INVALID_REQUEST or 400 <= status_code < 500:
        return CarrierError(
            "invalid_request", code, retryable=False, detail=detail or "telnyx rejected the request"
        )

    return CarrierError("carrier_transient", code, retryable=True, detail=detail)


def unreachable(detail: str) -> CarrierError:
    return CarrierError("carrier_unreachable", None, retryable=True, detail=detail[:255])
