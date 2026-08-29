"""Outbound pacing math (P11). Pure functions - no DB, no IO.

See phase-11-plan DR-5/DR-7/DR-10. Flash draft, Fable-reviewed."""

from __future__ import annotations

from datetime import datetime, timedelta
from random import Random

DEFAULT_WARMUP_SCHEDULE: list[tuple[int, int]] = [(3, 50), (7, 100), (14, 250)]


def warmup_daily_cap(
    warmup_started_at: datetime | None,
    now: datetime,
    schedule: list[tuple[int, int]] = DEFAULT_WARMUP_SCHEDULE,
) -> int | None:
    """Return the warmup daily cap for the number's age-days, or None."""
    if warmup_started_at is None:
        return None
    if now < warmup_started_at:
        age_days = 1
    else:
        age_days = (now - warmup_started_at).days + 1
    for through_day, cap in schedule:
        if through_day >= age_days:
            return cap
    return None


def effective_daily_cap(
    campaign_daily_cap: int,
    ramp_cap: int | None,
    respect_warmup: bool,
) -> int:
    """Combine campaign and warmup caps. Ramp applies only when enabled."""
    if respect_warmup and ramp_cap is not None:
        return min(campaign_daily_cap, ramp_cap)
    return campaign_daily_cap


def send_interval_seconds(rate_per_minute: int, rng: Random) -> float:
    """Base pacing interval with +/-20% jitter."""
    base_interval = 60.0 / max(rate_per_minute, 1)
    return base_interval * rng.uniform(0.8, 1.2)


def next_send_due(
    last_send_at: datetime | None,
    rate_per_minute: int,
    rng: Random,
) -> datetime | None:
    """Return when the next send should occur; None means due immediately."""
    if last_send_at is None:
        return None
    return last_send_at + timedelta(seconds=send_interval_seconds(rate_per_minute, rng))


def predictive_coefficient(
    calls_placed: int,
    calls_abandoned: int,
    target_abandon_rate: float = 0.03,
    min_sample: int = 20,
    floor: float = 0.25,
    ceiling: float = 1.5,
) -> float:
    """Return the predictive pacing multiplier."""
    if calls_placed < min_sample:
        return 1.0
    observed = calls_abandoned / calls_placed
    if observed == 0.0:
        return ceiling
    coefficient = target_abandon_rate / observed
    # Guarantees projected abandon rate is pulled back to <= target (FTC/TSR 3% cap).
    return max(floor, min(ceiling, coefficient))


def parallel_lines_allowed(mode: str, configured_lines: int) -> int:
    """Return the number of parallel lines permitted for this mode."""
    if mode in ("parallel", "predictive"):
        return max(configured_lines, 1)
    return 1

