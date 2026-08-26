"""SignalWire (Twilio-compatible) webhook verification and parsing.

Three things here are unlike the other two adapters and each one bites if assumed away.

**The body is form-encoded, not JSON.** Twilio-compatible callbacks post
``application/x-www-form-urlencoded``.

**The signature covers the URL, not just the body.** It is
``base64(HMAC-SHA1(auth_token, url + concat(sorted(k + v))))``. That is why this module
takes an explicitly *configured* URL rather than reconstructing one from the request's
Host header: Host is attacker-controllable, and a verifier that lets the caller choose part
of the signed string is not a verifier. We registered the URL with SignalWire, so we know
what it is.

**One request carries one event**, and inbound vs status is distinguished by which fields
are present rather than by an explicit type.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import parse_qsl

from app.providers.domain import CarrierEvent, DeliveryReceipt, InboundMessage, UnknownEvent

#: Twilio-compatible MessageStatus -> our canonical event vocabulary.
_STATUS_TO_EVENT = {
    "accepted": "message-sending",
    "queued": "message-sending",
    "sending": "message-sending",
    "sent": "message-sending",
    "delivered": "message-delivered",
    "undelivered": "message-failed",
    "failed": "message-failed",
}


def _form(raw_body: bytes) -> dict[str, str]:
    return dict(parse_qsl(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True))


def expected_signature(url: str, params: Mapping[str, str], auth_token: str) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify(headers: Mapping[str, str], auth_token: str, url: str, raw_body: bytes) -> bool:
    if not auth_token or not url:
        return False
    lower = {k.lower(): v for k, v in headers.items()}
    provided = lower.get("x-signalwire-signature") or lower.get("x-twilio-signature") or ""
    if not provided:
        return False
    return hmac.compare_digest(expected_signature(url, _form(raw_body), auth_token), provided)


def parse(raw_body: bytes) -> list[CarrierEvent]:
    params = _form(raw_body)
    if not params:
        raise ValueError("signalwire webhook body is empty or not form-encoded")

    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    if not sid:
        raise ValueError("signalwire webhook has no MessageSid")

    status = params.get("MessageStatus") or params.get("SmsStatus") or ""

    # A status callback carries MessageStatus; an inbound message carries Body/From/To
    # WITHOUT a delivery status. "received" is what inbound posts look like.
    if status and status != "received":
        canonical = _STATUS_TO_EVENT.get(status)
        if canonical is None:
            # Never guess an unmapped status into a terminal state - dead-letter it, which
            # is visible, rather than inventing a "delivered" nobody can audit.
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
