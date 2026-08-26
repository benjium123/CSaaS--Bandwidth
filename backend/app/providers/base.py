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

    def media_auth(self, url: str) -> tuple[str, str] | None:
        """Credentials for fetching carrier-hosted media, or None. Host-checked."""
        ...


def build_carrier(settings) -> MessagingCarrier | None:  # noqa: ANN001
    """The PRIMARY carrier, or None when messaging is not usable.

    Kept as a shim over the registry (phase-3b DR-4). P1/P2 seam tests are written against
    this function; they are the evidence the carrier abstraction held, so they keep working
    unmodified rather than being rewritten to suit a later design.

    Returning None rather than raising is deliberate: the app must boot and serve /healthz
    with no carrier configured - that is the R1 reality today.
    """
    from app.providers.registry import build_registry

    return build_registry(settings).primary()


def get_carrier(request: Request) -> MessagingCarrier:
    carrier = getattr(request.app.state, "carrier", None)
    if carrier is None:
        raise CarrierNotConfiguredError(
            "No messaging carrier is configured. Set BANDWIDTH_* in .env and enable it."
        )
    return carrier
