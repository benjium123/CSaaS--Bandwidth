"""Bandwidth error-code classification.

Bandwidth's codes are 4 digits: 1st = client(4)/server(5), 2nd = Bandwidth(1)/carrier(7).
See docs/research/bandwidth.md for the full taxonomy.
"""

from __future__ import annotations

from typing import Any

from app.providers.domain import CarrierError

# Codes that mean "this request is wrong; retrying changes nothing".
_INVALID_REQUEST = {"4302", "4403", "4411", "4720"}
# Not on a 10DLC campaign. This is the Track-R tripwire and is logged at ERROR.
_UNREGISTERED = {"4476"}
# Volume/velocity limits.
_RATE_LIMITED = {"4780"}


def _extract_code(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("type", "errorCode", "code"):
            val = body.get(key)
            if val is not None:
                return str(val)
        desc = body.get("description")
        if isinstance(desc, str):
            for token in desc.split():
                if token.isdigit() and len(token) == 4:
                    return token
    return None


def classify(status_code: int, body: Any) -> CarrierError:
    """Map an HTTP status + response body to our carrier-neutral error."""
    code = _extract_code(body)
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("description") or body.get("message") or "")[:255]

    if status_code in (401, 403):
        return CarrierError(
            "auth", code, retryable=False, detail=detail or "carrier rejected credentials"
        )

    if status_code == 429 or (code in _RATE_LIMITED):
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
            "carrier_transient", code, retryable=True, detail=detail or "carrier server error"
        )

    if code in _INVALID_REQUEST or 400 <= status_code < 500:
        return CarrierError(
            "invalid_request",
            code,
            retryable=False,
            detail=detail or "carrier rejected the request",
        )

    return CarrierError("carrier_transient", code, retryable=True, detail=detail)


def unreachable(detail: str) -> CarrierError:
    return CarrierError("carrier_unreachable", None, retryable=True, detail=detail[:255])
