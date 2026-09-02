"""Number provisioning - a protocol SEPARATE from messaging (phase-4-plan DR-2).

The two capabilities genuinely come apart. The Bandwidth trial account can order numbers
but cannot message; a carrier may be trusted to send long before we let it provision. One
combined interface would force every adapter to implement methods it cannot honour, and
push the failure to runtime where a half-built order is already in flight.

Capability is asked for, never probed by trying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from app.errors import FeatureUnavailableError


#: A per-number price above this is not a real carrier quote - either a parsing
#: accident (e.g. "1e999") or a corrupt/hostile value. $10,000,000.00.
_MAX_COST_CENTS = 1_000_000_000


def parse_cost_cents(raw: str | None) -> int | None:
    """Parse a decimal-dollar cost string (e.g. "1.00") into integer cents.

    P18: shared by every adapter's numbers.py so "1.00" -> 100 parses identically
    everywhere. None/blank/unparseable input returns None - a carrier that does not
    report a cost must never be recorded as costing exactly $0.00. A negative amount or
    one outside a sane magnitude also returns None rather than being stored as-is - a
    cost field feeds real money math (purchase_cost_cents/monthly_cost_cents), so a
    parsing accident must fail closed, not silently produce a huge or negative price.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        cents = int(round(Decimal(text) * 100))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if cents < 0 or cents > _MAX_COST_CENTS:
        return None
    return cents


@dataclass(frozen=True)
class AvailableNumber:
    e164: str
    number_type: str = "local"  # local | tollfree
    region: str = ""
    locality: str = ""
    monthly_cost: str = ""
    setup_cost: str = ""
    capabilities: dict = field(default_factory=dict)
    #: P18: parsed cents, when the carrier reports a machine-readable price. None means
    #: the carrier did not report one - never assume $0.00.
    monthly_cost_cents: int | None = None
    setup_cost_cents: int | None = None


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
    #: P18: same cents convention as AvailableNumber above.
    monthly_cost_cents: int | None = None
    setup_cost_cents: int | None = None


@runtime_checkable
class NumberProvider(Protocol):
    """Search, order, release. Implemented only by carriers that can actually do it.

    P18: a carrier MAY also implement an ``order_status(provider_ref) -> object``
    coroutine returning an object with a ``status`` attribute (and optional ``detail``)
    for asynchronous orders (Bandwidth). It is deliberately NOT part of this Protocol:
    this Protocol is ``@runtime_checkable``, and adding a required member here would
    make ``isinstance(carrier, NumberProvider)`` start rejecting every carrier that
    orders synchronously (Telnyx/Twilio/Plivo/SignalWire), which do not and need not
    define it. Callers (the sweeper's ``services/number_orders.py``) hasattr-check for
    it instead.
    """

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
