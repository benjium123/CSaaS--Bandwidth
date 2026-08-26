from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.compliance.npa_tz import ALL_US_ZONES, NPA_TZ, zones_for_npa
from app.compliance.quiet_hours import evaluate, npa_of, resolve_zones


def test_every_zone_in_the_table_actually_loads():
    """A typo'd IANA name would silently become 'unknown zone' at runtime."""
    for npa, zones in NPA_TZ.items():
        assert zones, f"{npa} has no zones"
        for zone in zones:
            ZoneInfo(zone)  # raises if the name is wrong
    for zone in ALL_US_ZONES:
        ZoneInfo(zone)


@pytest.mark.parametrize(
    "npa,expected",
    [
        ("212", "America/New_York"),
        ("214", "America/Chicago"),
        ("469", "America/Chicago"),
        ("972", "America/Chicago"),
        ("415", "America/Los_Angeles"),
        ("602", "America/Phoenix"),
        ("808", "Pacific/Honolulu"),
        ("907", "America/Anchorage"),
    ],
)
def test_spot_checks(npa, expected):
    assert expected in zones_for_npa(npa)


def test_unknown_and_tollfree_fall_back_conservatively():
    assert zones_for_npa("999") == ALL_US_ZONES
    for tf in ("800", "833", "844", "855", "866", "877", "888"):
        assert zones_for_npa(tf) == ALL_US_ZONES, "toll-free carries no geography"


def test_npa_extraction():
    assert npa_of("+12145550100") == "214"
    assert npa_of("+442071234567") is None
    assert npa_of("garbage") is None


def test_explicit_contact_timezone_wins():
    """Area code is not location: a 214 number can live in Berlin."""
    assert resolve_zones("+12145550100", "Europe/Berlin") == ("Europe/Berlin",)
    # Garbage in the field must not crash a send; it falls through to inference.
    assert resolve_zones("+12145550100", "Not/AZone") == ("America/Chicago",)


def test_allowed_inside_window():
    # 18:00Z == 13:00 CDT
    now = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    assert evaluate("+12145550100", now=now).allowed


def test_blocked_late_evening_and_reopens_at_local_0800():
    # 03:00Z on the 16th == 22:00 CDT on the 15th.
    now = datetime(2026, 6, 16, 3, 0, tzinfo=timezone.utc)
    result = evaluate("+12145550100", now=now)
    assert not result.allowed
    opens_local = result.not_before.astimezone(ZoneInfo("America/Chicago"))
    assert (opens_local.hour, opens_local.minute) == (8, 0)


def test_multizone_npa_must_be_legal_everywhere():
    """605 spans Central and Mountain. 07:30 MT is 08:30 CT - legal in one, not the other,
    so the send must wait."""
    now = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # 08:30 CDT / 07:30 MDT
    assert "America/Denver" in zones_for_npa("605")
    result = evaluate("+16055550100", now=now)
    assert not result.allowed, "an NPA spanning zones must satisfy the strictest one"


def test_unknown_npa_is_maximally_conservative():
    """Hawaii is the constraint: 08:00 HST is 18:00Z."""
    too_early = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)  # 07:00 HST
    assert not evaluate("+19995550100", now=too_early).allowed
    ok = datetime(2026, 6, 15, 18, 30, tzinfo=timezone.utc)  # 08:30 HST
    assert evaluate("+19995550100", now=ok).allowed


def test_arizona_ignores_dst():
    """Phoenix does not shift. In summer it is UTC-7, same as Denver's DST offset; in
    winter it is UTC-7 while Denver is UTC-7... the point is we never do the arithmetic."""
    summer = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)  # 07:30 MST (Phoenix)
    winter = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)  # 07:30 MST (Phoenix)
    assert not evaluate("+16025550100", now=summer).allowed
    assert not evaluate("+16025550100", now=winter).allowed
    assert evaluate("+16025550100", now=summer.replace(hour=15, minute=30)).allowed


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc),   # US spring-forward day
        datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),  # US fall-back day
    ],
)
def test_dst_transition_days_do_not_explode(instant):
    """zoneinfo handles the transition; we must never do offset arithmetic ourselves."""
    result = evaluate("+12145550100", now=instant)
    assert isinstance(result.allowed, bool)
    if not result.allowed:
        local = result.not_before.astimezone(ZoneInfo("America/Chicago"))
        assert (local.hour, local.minute) == (8, 0)


def test_narrower_window_is_respected():
    now = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # 08:30 CDT
    assert evaluate("+12145550100", now=now).allowed
    assert not evaluate(
        "+12145550100", window_start="09:00", window_end="17:00", now=now
    ).allowed
