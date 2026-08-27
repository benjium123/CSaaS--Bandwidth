"""Twilio webhook verification and parsing.

Twilio's REST/LaML API is the ORIGINAL that SignalWire's Compatibility API cloned
(phase-3b DR-5); the signature scheme here is the one SignalWire copied, not the other way
around. It is ``base64(HMAC-SHA1(auth_token, url + concat(sorted(k + v))))``, which is why
this module takes an explicitly *configured* URL rather than reconstructing one from the
request's Host header - Host is attacker-controllable, and a verifier that lets the caller
choose part of the signed string is not a verifier. We registered the URL with Twilio, so we
know what it is.

The body is form-encoded, not JSON. A malformed or empty body, or one missing the id Twilio
always sends, is reported as no events rather than raised: a webhook endpoint that 500s on a
garbled request is a worse failure mode than one that just drops it and moves on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import parse_qsl

import structlog

from app.providers.domain import CarrierEvent, DeliveryReceipt, InboundMessage, UnknownEvent

log = structlog.get_logger("carrier.twilio.webhooks")

#: Twilio MessageStatus -> our canonical event vocabulary.
_STATUS_TO_EVENT = {
    "queued": "message-sending",
    "sending": "message-sending",
    "sent": "message-sending",
    "delivered": "message-delivered",
    "undelivered": "message-failed",
    "failed": "message-failed",
}


def _form(raw_body: bytes) -> dict[str, str]:
    try:
        decoded = raw_body.decode("utf-8", errors="replace")
    except Exception:
        return {}
    return dict(parse_qsl(decoded, keep_blank_values=True))


def expected_signature(url: str, params: Mapping[str, str], auth_token: str) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify(headers: Mapping[str, str], auth_token: str, raw_body: bytes, url: str) -> bool:
    if not auth_token or not url:
        return False
    lower = {k.lower(): v for k, v in headers.items()}
    provided = lower.get("x-twilio-signature", "")
    if not provided:
        return False
    return hmac.compare_digest(expected_signature(url, _form(raw_body), auth_token), provided)


def parse(raw_body: bytes) -> list[CarrierEvent]:
    """Twilio posts ONE event per request, form-encoded. Unparseable -> no events."""
    params = _form(raw_body)
    if not params:
        return []

    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    if not sid:
        return []

    status = params.get("MessageStatus") or params.get("SmsStatus") or ""

    if status:
        canonical = _STATUS_TO_EVENT.get(status)
        if canonical is None:
            # Never guess an unmapped status into a terminal state - dead-letter it, which is
            # visible, rather than inventing a "delivered" nobody can audit.
            log.warning("twilio_unknown_message_status", status=status)
            return [UnknownEvent(f"status:{status}", params)]
        code = params.get("ErrorCode") or None
        return [
            DeliveryReceipt(
                provider_message_id=sid,
                event_type=canonical,
                error_code=code,
                error_description=params.get("ErrorMessage") or None,
                raw=params,
            )
        ]

    our_number = params.get("To") or ""
    try:
        num_media = int(params.get("NumMedia") or "0")
    except ValueError:
        num_media = 0
    # MediaUrl0, MediaUrl1, ... repeat as distinct form keys; NEVER an array.
    media = tuple(
        params[f"MediaUrl{i}"] for i in range(num_media) if params.get(f"MediaUrl{i}")
    )

    try:
        segments = int(params["NumSegments"]) if params.get("NumSegments") else None
    except ValueError:
        segments = None

    return [
        InboundMessage(
            provider_message_id=sid,
            from_=params.get("From") or "",
            to=our_number,
            our_number=our_number,
            text=params.get("Body") or "",
            media=media,
            segment_count=segments,
            raw=params,
        )
    ]
