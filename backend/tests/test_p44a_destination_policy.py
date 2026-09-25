"""P44a: the destination firewall (IRSF, Wangiri, premium, out-of-country)."""

from __future__ import annotations

import pytest

from app.services import destination_policy as dp

BLOCKED = dp.BLOCKED
NOT_ALLOWED = dp.NOT_ALLOWED


@pytest.mark.parametrize(
    ("e164", "home", "expected"),
    [
        # US workspace: contiguous 48 only.
        ("+12145551234", "US", None),
        ("+19729612810", "US", None),
        ("+18005551234", "US", None),  # US toll-free is domestic
        ("+15555550100", "US", None),  # unassigned NPA: domestic, cannot reach a premium carrier
        ("+19075551234", "US", NOT_ALLOWED),  # Alaska
        ("+18085551234", "US", NOT_ALLOWED),  # Hawaii
        ("+14165551234", "US", NOT_ALLOWED),  # Canada
        ("+17875551234", "US", NOT_ALLOWED),  # Puerto Rico
        ("+13405551234", "US", NOT_ALLOWED),  # USVI
        ("+16715551234", "US", NOT_ALLOWED),  # Guam
        ("+442079460958", "US", NOT_ALLOWED),  # UK from a US workspace
        # Hard blocks, whatever the home region.
        ("+19005551234", "US", BLOCKED),  # US premium rate
        ("+18765551234", "US", BLOCKED),  # Jamaica (Wangiri)
        ("+18095551234", "US", BLOCKED),  # Dominican Republic
        ("+882161234567", "US", BLOCKED),  # international networks
        ("+8810123456", "US", BLOCKED),  # satellite
        ("+870773123456", "US", BLOCKED),  # Inmarsat
        ("+5355512345", "US", BLOCKED),  # Cuba
        ("+2521234567", "US", BLOCKED),  # Somalia
        ("+6881234", "US", BLOCKED),  # Tuvalu
        ("+not-a-number", "US", BLOCKED),
        # UK workspace: GB only.
        ("+442079460958", "GB", None),
        ("+447400123456", "GB", None),  # GB mobile
        ("+12145551234", "GB", NOT_ALLOWED),
        ("+449098790000", "GB", BLOCKED),  # 09 premium
        ("+448712345678", "GB", BLOCKED),  # 087 revenue share
        ("+447012345678", "GB", BLOCKED),  # 070 personal number
        ("+447612345678", "GB", BLOCKED),  # 076 pager
        ("+447911123456", "GB", NOT_ALLOWED),  # a Guernsey mobile range shares +44
        # Emergency numbers are never blocked.
        ("911", "US", None),
        ("+1911", "US", None),
        ("999", "GB", None),
    ],
)
def test_decide(e164, home, expected):
    assert dp.decide(e164, home) == expected


def test_alaska_and_hawaii_are_their_own_region():
    assert dp.destination_region("+19075551234") == "US-AK"
    assert dp.destination_region("+18085551234") == "US-HI"
    assert dp.destination_region("+12145551234") == "US"


def test_policy_can_be_switched_off_for_local_debugging():
    class _S:
        destination_policy_enforced = False

    import asyncio

    assert asyncio.run(dp.check(None, _S(), None, "+882161234567")) is None
