"""Twilio error-code classification.

Twilio returns ``{"code": 21211, "message": ..., "status": 400}`` - codes arrive as JSON
INTEGERS (unlike Bandwidth's/Telnyx's string codes), so ``_extract`` coerces to ``str`` once
here and every comparison below works against that string form.

21610 gets its own branch rather than folding into the generic invalid-request set: it means
the recipient replied STOP *at the carrier*, which is a fact our own opt-out ledger may not
yet know. The detail string says so explicitly so a human reading a failed-send log does not
mistake it for an ordinary bad-request.
"""

from __future__ import annotations

from typing import Any

from app.providers.domain import CarrierError

# Toll-free/10DLC traffic sent from a number that is not registered for it.
_UNREGISTERED = {"30032", "30034", "30038"}
_INVALID_REQUEST = {"21211", "21212", "21606", "21614"}
_OPT_OUT_CODE = "21610"
_RATE_LIMIT_CODE = "20429"


def _extract(body: Any) -> tuple[str | None, str]:
    if not isinstance(body, dict):
        return None, ""
    raw_code = body.get("code")
    code = str(raw_code) if raw_code is not None else None
    detail = str(body.get("message") or "")[:255]
    return code, detail


def classify(status_code: int, body: Any) -> CarrierError:
    code, detail = _extract(body)

    if status_code in (401, 403):
        return CarrierError(
            "auth", code, retryable=False, detail=detail or "twilio rejected credentials"
        )

    if status_code == 429 or code == _RATE_LIMIT_CODE:
        return CarrierError("rate_limited", code, retryable=True, detail=detail or "rate limited")

    if code in _UNREGISTERED:
        return CarrierError(
            "unregistered",
            code,
            retryable=False,
            detail=detail or "number is not registered for toll-free/10DLC traffic",
        )

    if code == _OPT_OUT_CODE:
        return CarrierError(
            "invalid_request",
            code,
            retryable=False,
            detail=detail
            or (
                "the carrier holds an opt-out (STOP reply) for this recipient that our own "
                "ledger may not know about"
            ),
        )

    if status_code >= 500:
        return CarrierError(
            "carrier_transient", code, retryable=True, detail=detail or "twilio server error"
        )

    if code in _INVALID_REQUEST or 400 <= status_code < 500:
        return CarrierError(
            "invalid_request",
            code,
            retryable=False,
            detail=detail or "twilio rejected the request",
        )

    return CarrierError("carrier_transient", code, retryable=True, detail=detail)


def unreachable(detail: str) -> CarrierError:
    return CarrierError("carrier_unreachable", None, retryable=True, detail=detail[:255])
