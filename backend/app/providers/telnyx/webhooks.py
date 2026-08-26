"""Telnyx webhook verification and parsing.

Telnyx signs with **Ed25519** over ``timestamp|body``, which is a genuinely stronger scheme
than Bandwidth's Basic auth: it proves the payload came from Telnyx and has not been
altered, where Basic auth only proves the caller knew a shared password.

The timestamp is checked as well as the signature. A signature with no freshness window is
a replay primitive - an attacker who captures one valid callback could resubmit it forever.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from datetime import datetime, timezone

import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.providers.domain import CarrierEvent, DeliveryReceipt, InboundMessage, UnknownEvent

log = structlog.get_logger("carrier.telnyx.webhooks")

#: Telnyx's own recommendation. Beyond this a valid signature is treated as a replay.
TOLERANCE_SECONDS = 300

INBOUND_TYPE = "message.received"
DLR_TYPES = frozenset({"message.sent", "message.finalized", "message.failed"})

#: Telnyx per-recipient status -> our canonical event vocabulary.
_STATUS_TO_EVENT = {
    "queued": "message-sending",
    "sending": "message-sending",
    "sent": "message-sending",
    "delivered": "message-delivered",
    "sending_failed": "message-failed",
    "delivery_failed": "message-failed",
    "delivery_unconfirmed": "message-delivered",
    "expired": "message-failed",
}


def verify(headers: Mapping[str, str], public_key_b64: str, raw_body: bytes) -> bool:
    if not public_key_b64:
        return False

    lower = {k.lower(): v for k, v in headers.items()}
    signature_b64 = lower.get("telnyx-signature-ed25519", "")
    timestamp = lower.get("telnyx-timestamp", "")
    if not signature_b64 or not timestamp:
        return False

    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > TOLERANCE_SECONDS:
        log.warning("telnyx_webhook_stale", age_seconds=age)
        return False

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), f"{timestamp}|".encode() + raw_body)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse(raw_body: bytes) -> list[CarrierEvent]:
    """Telnyx posts ONE event per request, unlike Bandwidth's array."""
    import json

    try:
        envelope = json.loads(raw_body)
    except ValueError as exc:
        raise ValueError(f"telnyx webhook is not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("telnyx webhook must be a JSON object")

    data = envelope.get("data")
    if not isinstance(data, dict):
        raise ValueError("telnyx webhook has no data object")
    return [_parse_one(data)]


def _first_to(payload: dict) -> tuple[str, str]:
    """Return (number, status) for the first recipient.

    We never send group messages, so 'the first recipient' is 'the recipient'.
    """
    recipients = payload.get("to") or []
    if isinstance(recipients, list) and recipients and isinstance(recipients[0], dict):
        first = recipients[0]
        return str(first.get("phone_number") or ""), str(first.get("status") or "")
    return "", ""


def _parse_one(data: dict) -> CarrierEvent:
    event_type = str(data.get("event_type") or "")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return UnknownEvent(event_type, data)

    provider_id = str(payload.get("id") or "")
    if not provider_id:
        return UnknownEvent(event_type, data)

    if event_type == INBOUND_TYPE:
        sender = payload.get("from") or {}
        from_number = str(sender.get("phone_number") or "") if isinstance(sender, dict) else ""
        our_number, _ = _first_to(payload)
        media = tuple(
            str(m.get("url"))
            for m in (payload.get("media") or [])
            if isinstance(m, dict) and m.get("url")
        )
        return InboundMessage(
            provider_message_id=provider_id,
            from_=from_number,
            to=our_number,
            our_number=our_number,
            text=str(payload.get("text") or ""),
            media=media,
            segment_count=payload.get("parts") if isinstance(payload.get("parts"), int) else None,
            event_time=_parse_time(data.get("occurred_at")),
            raw=data,
        )

    if event_type in DLR_TYPES:
        _, status = _first_to(payload)
        canonical = _STATUS_TO_EVENT.get(status)
        if canonical is None:
            # An unmapped status must NOT be guessed into a terminal state. Surfacing it
            # as unknown dead-letters it, which is visible; guessing "delivered" is not.
            return UnknownEvent(f"{event_type}:{status or 'no-status'}", data)
        errors = payload.get("errors") or []
        detail = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = str(errors[0].get("detail") or errors[0].get("title") or "")
        code = None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            raw_code = errors[0].get("code")
            code = str(raw_code) if raw_code is not None else None
        return DeliveryReceipt(
            provider_message_id=provider_id,
            event_type=canonical,
            error_code=code,
            error_description=detail or None,
            segment_count=payload.get("parts") if isinstance(payload.get("parts"), int) else None,
            event_time=_parse_time(data.get("occurred_at")),
            raw=data,
        )

    return UnknownEvent(event_type, data)
