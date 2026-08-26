"""Number provisioning - a protocol SEPARATE from messaging (phase-4-plan DR-2).

The two capabilities genuinely come apart. The Bandwidth trial account can order numbers
but cannot message; a carrier may be trusted to send long before we let it provision. One
combined interface would force every adapter to implement methods it cannot honour, and
push the failure to runtime where a half-built order is already in flight.

Capability is asked for, never probed by trying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.errors import FeatureUnavailableError


@dataclass(frozen=True)
class AvailableNumber:
    e164: str
    number_type: str = "local"  # local | tollfree
    region: str = ""
    locality: str = ""
    monthly_cost: str = ""
    setup_cost: str = ""
    capabilities: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NumberSearch:
    area_code: str = ""
    contains: str = ""
    locality: str = ""
    region: str = ""
    number_type: str = "local"
    limit: int = 20


@dataclass(frozen=True)
class OrderResult:
    e164: str
    provider_ref: str
    #: "active" the moment the carrier says so, "pending" when the order is asynchronous.
    #: Never assume active: a number that is not yet routable will silently drop inbound.
    status: str = "active"
    capabilities: dict = field(default_factory=dict)


@runtime_checkable
class NumberProvider(Protocol):
    """Search, order, release. Implemented only by carriers that can actually do it."""

    name: str

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]: ...

    async def order_number(self, e164: str) -> OrderResult: ...

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None: ...


def as_provider(carrier: object) -> NumberProvider:
    """Narrow a carrier to a provisioning provider, or fail with a useful message."""
    if not isinstance(carrier, NumberProvider):
        raise FeatureUnavailableError(
            f"Carrier {getattr(carrier, 'name', '?')!r} cannot provision numbers on this "
            f"deployment. Add the number manually, or order it in the carrier's portal."
        )
    return carrier
