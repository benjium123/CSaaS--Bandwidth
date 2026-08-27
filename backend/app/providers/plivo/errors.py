"""Plivo error classification.

Plivo returns ``{"error": "<message>"}`` with a meaningful HTTP status, but - unlike
Bandwidth's 4-digit codes or Telnyx's numeric code table - it has NO stable machine-readable
error code at all. Registration problems (10DLC / campaign rejections) show up ONLY as
prose in that message. This classifier is text-based ON PURPOSE: there is nothing else to
match on. When Plivo changes its wording, update ``_UNREGISTERED_PHRASES`` below - that is
the whole maintenance surface of this module.
"""

from __future__ import annotations

from typing import Any

from app.providers.domain import CarrierError

#: Case-insensitive substrings that mean "this number/sender is not on a compliant 10DLC
#: campaign". Matched against the carrier's free-text error message.
_UNREGISTERED_PHRASES = ("campaign", "10dlc", "not registered", "unregistered")


def _extract_message(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("error", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def classify(status_code: int, body: Any) -> CarrierError:
    """Map an HTTP status + response body to our carrier-neutral error.

    Plivo never gives us a carrier_code - CarrierError.carrier_code is always None here.
    """
    detail = _extract_message(body)[:255]
    lower = detail.lower()

    if status_code in (401, 403):
        return CarrierError(
            "auth", None, retryable=False, detail=detail or "carrier rejected credentials"
        )

    if status_code == 429:
        return CarrierError("rate_limited", None, retryable=True, detail=detail or "rate limited")

    # Checked before the generic status buckets below: Plivo reports a campaign/10DLC
    # rejection with an ordinary 4xx status, and the prose is the only signal we have.
    if any(phrase in lower for phrase in _UNREGISTERED_PHRASES):
        return CarrierError(
            "unregistered",
            None,
            retryable=False,
            detail=detail or "number is not attached to a 10DLC campaign",
        )

    if status_code >= 500:
        return CarrierError(
            "carrier_transient", None, retryable=True, detail=detail or "carrier server error"
        )

    if status_code in (400, 404, 422):
        return CarrierError(
            "invalid_request",
            None,
            retryable=False,
            detail=detail or "carrier rejected the request",
        )

    return CarrierError("carrier_transient", None, retryable=True, detail=detail)


def unreachable(detail: str) -> CarrierError:
    return CarrierError("carrier_unreachable", None, retryable=True, detail=detail[:255])
