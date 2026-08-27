"""Plivo webhook verification and parsing. PURE - no DB, no I/O.

Signature scheme (V3, the interesting part - phase-9b DR-3): Plivo signs
``registered_url + nonce`` with HMAC-SHA256, base64-encodes it, and sends it in
``X-Plivo-Signature-V3`` alongside the nonce in ``X-Plivo-Signature-V3-Nonce``. The header
may carry MULTIPLE comma-separated candidate signatures (Plivo rotates signing behaviour
across API versions); we accept if ANY candidate matches. Note what this scheme does NOT
cover: the request body. Unlike Bandwidth (Basic auth, no body coverage either) or Telnyx
(Ed25519 over timestamp|body), Plivo's signature proves the URL+nonce pair was signed by
someone holding the auth token - it says nothing about body integrity. `verify` still
accepts `raw_body`/is called from a `verify_webhook(headers, raw_body)` surface because
that is the shape the CAL requires of every carrier; Plivo's adapter simply does not need
raw_body to answer the question.

Body shape: Plivo webhooks are FORM-ENCODED (``application/x-www-form-urlencoded``), not
JSON, unlike Bandwidth/Telnyx.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from urllib.parse import parse_qsl

import structlog

from app.providers.domain import CarrierEvent, DeliveryReceipt, InboundMessage

log = structlog.get_logger("carrier.plivo.webhooks")

#: Plivo's per-recipient delivery status -> our canonical event vocabulary. Same canonical
#: names Bandwidth/Telnyx emit (app/providers/domain.py DeliveryReceipt.event_type).
_STATUS_TO_EVENT = {
    "queued": "message-sending",
    "sent": "message-sending",
    "delivered": "message-delivered",
    "undelivered": "message-failed",
    "failed": "message-failed",
    "rejected": "message-failed",
}


def _compute_signature(auth_token: str, url: str, nonce: str) -> str:
    mac = hmac.new(auth_token.encode("utf-8"), (url + nonce).encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def verify(headers: Mapping[str, str], auth_token: str, url: str) -> bool:
    """Check the V3 signature. `url` is the URL WE REGISTERED with Plivo for this callback
    - never reconstructed from an attacker-controllable Host header (same rule as every
    other carrier's webhook verification in this codebase)."""
    lower = {k.lower(): v for k, v in headers.items()}
    signature_header = lower.get("x-plivo-signature-v3", "")
    nonce = lower.get("x-plivo-signature-v3-nonce", "")
    if not signature_header or not nonce or not url:
        return False

    expected = _compute_signature(auth_token, url, nonce)
    candidates = [c.strip() for c in signature_header.split(",") if c.strip()]
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)


def _extract_media(fields: dict[str, str]) -> tuple[str, ...]:
    """Plivo shapes inbound MMS media two different ways depending on account/version:
    either a `MediaUrls` JSON array, or individual `Media0`, `Media1`, ... keys. Handle
    both defensively rather than assuming one."""
    media_urls_raw = fields.get("MediaUrls")
    if media_urls_raw:
        try:
            parsed = json.loads(media_urls_raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return tuple(str(u) for u in parsed if u)

    media: list[str] = []
    index = 0
    while True:
        key = f"Media{index}"
        if key not in fields:
            break
        if fields[key]:
            media.append(fields[key])
        index += 1
    return tuple(media)


def parse_form(raw_body: bytes) -> dict[str, str] | None:
    """Public: the voice mixin (a different Plivo submodule, same webhook body shape and
    signature scheme) reuses this rather than re-implementing form parsing."""
    try:
        decoded = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        pairs = parse_qsl(decoded, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    return dict(pairs)


def parse(raw_body: bytes) -> list[CarrierEvent]:
    """Malformed input (undecodable / not form-encoded / missing the id) -> []. Unlike
    Bandwidth (raises ValueError for the caller to dead-letter) this returns an empty list,
    matching the task contract: "Malformed -> []"."""
    fields = parse_form(raw_body)
    if fields is None:
        return []

    message_uuid = fields.get("MessageUUID", "")
    if not message_uuid:
        return []

    status = fields.get("Status")
    if status is not None:
        canonical = _STATUS_TO_EVENT.get(status.strip().lower())
        if canonical is None:
            log.warning("plivo_unknown_message_status", status=status)
            return []
        return [
            DeliveryReceipt(
                provider_message_id=message_uuid,
                event_type=canonical,
                raw=fields,
            )
        ]

    from_ = fields.get("From", "")
    to = fields.get("To", "")
    if not from_ and not to:
        return []

    return [
        InboundMessage(
            provider_message_id=message_uuid,
            from_=from_,
            to=to,
            our_number=to,
            text=fields.get("Text", ""),
            media=_extract_media(fields),
            raw=fields,
        )
    ]
