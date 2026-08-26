"""Per-carrier circuit breaker.

In-process and decaying by design (phase-3b DR-3). Carrier health changes on a timescale
of seconds; a database-backed view would add a write to every send and still be stale by
the time it was read.

The distinction that matters: **only the carrier's own failures open the breaker.** An
auth error or a malformed request is *our* bug. Tripping a breaker on those and shifting
traffic to another carrier would carry the bug along and take down the second carrier's
reputation as well as the first's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.providers.domain import CarrierError

log = structlog.get_logger("carrier.health")

State = Literal["closed", "open", "half_open"]

#: Consecutive carrier-side failures before we stop sending.
FAILURE_THRESHOLD = 5
#: How long an open breaker waits before allowing one probe through.
COOLDOWN_SECONDS = 30.0

#: Categories that mean "the carrier is unwell". Everything else means "we are unwell".
CARRIER_FAULT_CATEGORIES = frozenset({"carrier_transient", "carrier_unreachable", "rate_limited"})


def opens_breaker(error: CarrierError | None) -> bool:
    return error is not None and error.category in CARRIER_FAULT_CATEGORIES


@dataclass
class Breaker:
    """One carrier's health. Not thread-safe by intent - asyncio, single loop."""

    name: str
    consecutive_failures: int = 0
    opened_at: float | None = None
    _probing: bool = field(default=False, repr=False)

    def state(self, now: float | None = None) -> State:
        if self.opened_at is None:
            return "closed"
        moment = now if now is not None else time.monotonic()
        if moment - self.opened_at >= COOLDOWN_SECONDS:
            return "half_open"
        return "open"

    def allows_send(self, now: float | None = None) -> bool:
        """half_open lets exactly ONE probe through, not a thundering herd."""
        state = self.state(now)
        if state == "closed":
            return True
        if state == "open":
            return False
        if self._probing:
            return False
        self._probing = True
        return True

    def record_success(self) -> None:
        if self.opened_at is not None:
            log.info("carrier_breaker_closed", carrier=self.name)
        self.consecutive_failures = 0
        self.opened_at = None
        self._probing = False

    def record_failure(self, error: CarrierError | None, now: float | None = None) -> None:
        self._probing = False
        if not opens_breaker(error):
            # Our fault, not theirs. A run of invalid_request must never look like an
            # outage - moving that traffic elsewhere would just break a second carrier.
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAILURE_THRESHOLD and self.opened_at is None:
            self.opened_at = now if now is not None else time.monotonic()
            log.error(
                "carrier_breaker_opened",
                carrier=self.name,
                failures=self.consecutive_failures,
                category=error.category if error else None,
            )


class HealthRegistry:
    def __init__(self) -> None:
        self._breakers: dict[str, Breaker] = {}

    def breaker(self, name: str) -> Breaker:
        if name not in self._breakers:
            self._breakers[name] = Breaker(name)
        return self._breakers[name]

    def is_healthy(self, name: str, now: float | None = None) -> bool:
        return self.breaker(name).state(now) != "open"

    def snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "state": b.state(),
                "consecutive_failures": b.consecutive_failures,
            }
            for name, b in self._breakers.items()
        }
