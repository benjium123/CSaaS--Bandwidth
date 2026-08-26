from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fastapi import Request

from app.errors import CarrierNotConfiguredError
from app.providers.domain import CarrierCapabilities, CarrierEvent, OutboundMessage, SendResult


@runtime_checkable
class MessagingCarrier(Protocol):
    """The messaging contract. Telnyx-shaped per ARCHITECTURE D2.

    Note: for MESSAGING, Bandwidth is not document-return — messaging callbacks want a bare
    2xx and ignore the body. Document-return is a VOICE constraint (P5 inherits it).
    """

    name: str
    capabilities: CarrierCapabilities

    async def send_message(self, msg: OutboundMessage) -> SendResult: ...

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool: ...

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]: ...


def build_carrier(settings) -> MessagingCarrier | None:  # noqa: ANN001
    """Build the configured carrier, or None when messaging is not usable.

    Returning None rather than raising is deliberate: the app must boot and serve /healthz
    with no carrier configured — that is the R1 reality today.
    """
    status = next((p for p in settings.provider_statuses() if p.name == "bandwidth"), None)
    if status is None or not status.enabled:
        return None
    if not settings.bandwidth_messaging_application_id.strip():
        return None

    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    return BandwidthMessagingCarrier(
        account_id=settings.bandwidth_account_id,
        api_username=settings.bandwidth_api_username,
        api_password=settings.bandwidth_api_password.get_secret_value(),
        application_id=settings.bandwidth_messaging_application_id,
        webhook_username=settings.bandwidth_webhook_username,
        webhook_password=settings.bandwidth_webhook_password.get_secret_value(),
    )


def get_carrier(request: Request) -> MessagingCarrier:
    carrier = getattr(request.app.state, "carrier", None)
    if carrier is None:
        raise CarrierNotConfiguredError(
            "No messaging carrier is configured. Set BANDWIDTH_* in .env and enable it."
        )
    return carrier
