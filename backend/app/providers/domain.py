"""Carrier-neutral domain objects.

Frozen dataclasses, zero SQLAlchemy imports. These are the vocabulary every caller outside
``app/providers/`` speaks; no caller ever sees a Bandwidth or Telnyx shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ErrorCategory = Literal[
    "invalid_request",
    "unregistered",
    "rate_limited",
    "carrier_transient",
    "carrier_unreachable",
    "auth",
]


@dataclass(frozen=True)
class CarrierError:
    category: ErrorCategory
    carrier_code: str | None
    retryable: bool
    detail: str = ""


@dataclass(frozen=True)
class CarrierCapabilities:
    """What an adapter can and cannot do. Declared, never discovered by trial."""

    #: False for a carrier this deployment holds VOICE-only credentials for. Bandwidth
    #: sells voice and messaging as separate products with separate application ids, so a
    #: perfectly healthy account can dial and never be allowed to text. Declared, because
    #: discovering it from a failed send means the failure is already on a brand's record.
    supports_messaging: bool = True
    supports_cancel: bool = False
    supports_scheduled_send: bool = False
    sync_delivery_status: bool = False
    max_media_bytes: int = 3_750_000
    group_mms_toll_free: bool = False


@dataclass(frozen=True)
class OutboundMessage:
    to: str
    from_: str
    text: str
    media: tuple[str, ...] = ()
    tag: str = ""  # OUR correlation id (str(message.id)); the carrier echoes it back


@dataclass(frozen=True)
class SendResult:
    """Deliberately has NO "delivered" member.

    Bandwidth answers 202 Accepted; delivery is only ever learned from a webhook. Making
    delivery unrepresentable here means no caller can mistakenly believe a send succeeded.
    """

    status: Literal["accepted", "rejected"]
    provider_message_id: str | None = None
    error: CarrierError | None = None


@dataclass(frozen=True)
class InboundMessage:
    provider_message_id: str
    from_: str
    to: str
    our_number: str
    text: str
    media: tuple[str, ...] = ()
    segment_count: int | None = None
    event_time: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    provider_message_id: str
    event_type: str  # message-sending | message-delivered | message-failed
    error_code: str | None = None
    error_description: str | None = None
    segment_count: int | None = None
    event_time: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UnknownEvent:
    """An event type we do not model. The service dead-letters these; retrying cannot help."""

    event_type: str
    raw: dict = field(default_factory=dict)


CarrierEvent = InboundMessage | DeliveryReceipt | UnknownEvent
