"""Quiet hours, evaluated in the RECIPIENT's local time.

Two rules make this safe rather than merely present:

1. **Multi-candidate, all-must-pass.** When we are not certain which timezone a number is
   in, we evaluate every plausible zone and permit the send only if it is legal in *all* of
   them. An incomplete NPA table can therefore only ever make us send less — never at 3 a.m.
2. **No offset arithmetic anywhere.** We convert an aware UTC instant into each zone with
   ``zoneinfo`` at that instant, so DST is handled by the tz database rather than by us
   remembering that the US changes clocks on different dates than Europe.

``_now()`` is module-level so tests can freeze it. CI runs at arbitrary wall-clock times;
without a frozen clock, every send test in the suite would pass or fail depending on the
hour it happened to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.compliance.npa_tz import ALL_US_ZONES, zones_for_npa

DEFAULT_WINDOW_START = "08:00"
DEFAULT_WINDOW_END = "21:00"


def _now() -> datetime:
    """Patched in tests. Always tz-aware UTC."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QuietHoursResult:
    allowed: bool
    #: Earliest instant the send becomes legal in EVERY candidate zone. None when allowed.
    not_before: datetime | None = None
    zones: tuple[str, ...] = ()


def parse_hhmm(value: str, fallback: str) -> time:
    try:
        hh, _, mm = value.partition(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        hh, _, mm = fallback.partition(":")
        return time(int(hh), int(mm))


def npa_of(e164: str) -> str | None:
    if not e164 or not e164.startswith("+1"):
        return None
    digits = e164[2:]
    return digits[:3] if len(digits) == 10 and digits.isdigit() else None


def resolve_zones(e164: str, contact_timezone: str | None = None) -> tuple[str, ...]:
    """Candidate zones, most trusted source first.

    An explicit contact timezone always wins: **area code is not location.** A Dallas 214
    number can belong to someone living in Berlin, and quiet hours protect the human.
    """
    if contact_timezone:
        try:
            ZoneInfo(contact_timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass  # bad data falls through to inference rather than crashing a send
        else:
            return (contact_timezone,)

    npa = npa_of(e164)
    if npa is None:
        # Non-NANP or malformed: we know nothing, so require legality everywhere.
        return ALL_US_ZONES
    return zones_for_npa(npa)


def _next_open(instant: datetime, zone: str, start: time, end: time) -> datetime | None:
    """None when the instant is already inside the window for this zone; otherwise the
    next instant that is inside it."""
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        # An unknown zone must not open a hole: treat it as closed until tomorrow's start.
        return instant + timedelta(days=1)

    local = instant.astimezone(tz)
    if start <= local.time() < end:
        return None

    day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    # Constructing the local wall time and attaching the zone lets zoneinfo pick the right
    # offset for that date - which is exactly what makes DST transitions correct.
    return datetime.combine(day, start, tzinfo=tz).astimezone(timezone.utc)


def evaluate(
    to_e164: str,
    *,
    contact_timezone: str | None = None,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    now: datetime | None = None,
) -> QuietHoursResult:
    """Is it legal to send to ``to_e164`` right now, in every candidate zone?"""
    instant = now or _now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)

    start = parse_hhmm(window_start, DEFAULT_WINDOW_START)
    end = parse_hhmm(window_end, DEFAULT_WINDOW_END)
    zones = resolve_zones(to_e164, contact_timezone)

    opens: list[datetime] = []
    for zone in zones:
        nxt = _next_open(instant, zone, start, end)
        if nxt is not None:
            opens.append(nxt)

    if not opens:
        return QuietHoursResult(True, None, zones)

    # The send may only go once EVERY zone is open, so take the latest opening time.
    return QuietHoursResult(False, max(opens), zones)
