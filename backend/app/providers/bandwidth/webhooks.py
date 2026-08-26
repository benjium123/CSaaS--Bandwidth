"""Bandwidth messaging webhook parsing and auth. PURE — no DB, no I/O.

Two facts from docs/research/bandwidth.md drive this module:
  1. Inbound events arrive as a JSON **array**, even for a single event.
  2. Bandwidth retries any non-2xx for 24 h, UNORDERED and in parallel with in-flight
     retries. Nothing here may assume ordering or uniqueness.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone

from app.providers.domain import CarrierEvent, DeliveryReceipt, InboundMessage, UnknownEvent

DLR_TYPES = {"message-sending", "message-delivered", "message-failed"}
INBOUND_TYPE = "message-received"


def verify(headers: Mapping[str, str], expected_user: str, expected_pass: str) -> bool:
    """Constant-time HTTP Basic check.

    Both comparisons ALWAYS run and are combined with ``&``, never ``and`` — short-circuit
    evaluation would leak, via timing, whether the username was correct.
    """
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, _, password = decoded.partition(":")

    user_ok = hmac.compare_digest(user, expected_user)
    pass_ok = hmac.compare_digest(password, expected_pass)
    return bool(user_ok & pass_ok)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse(raw_body: bytes) -> list[CarrierEvent]:
    """Parse a Bandwidth messaging callback body.

    Raises ValueError for anything that is not a JSON array of event objects — the caller
    dead-letters those, because retrying malformed input cannot fix it.
    """
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("body is not valid JSON") from exc

    if not isinstance(payload, list):
        raise ValueError("body is not a JSON array")

    events: list[CarrierEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("array element is not an object")
        events.append(_parse_one(item))
    return events


def _parse_one(item: dict) -> CarrierEvent:
    event_type = item.get("type")
    message = item.get("message") or {}
    if not isinstance(message, dict):
        return UnknownEvent(str(event_type), item)

    provider_id = message.get("id")
    if event_type == INBOUND_TYPE and provider_id:
        media = message.get("media") or []
        return InboundMessage(
            provider_message_id=str(provider_id),
            from_=str(message.get("from") or ""),
            to=str(item.get("to") or (message.get("to") or [""])[0]),
            # `owner` is OUR number on the account; `from` is the contact.
            our_number=str(message.get("owner") or item.get("to") or ""),
            text=str(message.get("text") or ""),
            media=tuple(str(m) for m in media),
            segment_count=message.get("segmentCount"),
            event_time=_parse_time(item.get("time") or message.get("time")),
            raw=item,
        )

    if event_type in DLR_TYPES and provider_id:
        return DeliveryReceipt(
            provider_message_id=str(provider_id),
            event_type=str(event_type),
            error_code=str(item["errorCode"]) if item.get("errorCode") is not None else None,
            error_description=str(item.get("description") or "")[:255] or None,
            segment_count=message.get("segmentCount"),
            event_time=_parse_time(item.get("time") or message.get("time")),
            raw=item,
        )

    return UnknownEvent(str(event_type), item)
