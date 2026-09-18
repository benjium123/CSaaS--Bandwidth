# ruff: noqa: E501
"""P43 (audit finding 8): a bare national number belongs to the WORKSPACE's country.

Parsing everything as US refused a UK customer's ordinary numbers ("020 7946 0958",
"07911123456") and silently turned "2079460958" - what a spreadsheet export produces when
it strips the trunk zero - into +1 207 946 0958, a real number in Maine. Importing a UK
contact list then texted strangers who never consented.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.base import set_org_context
from app.models import KycProfile, Org
from app.services import phone_region
from app.services.list_parsing import normalize_phone


@pytest.fixture(autouse=True)
def _clean_cache():
    phone_region.forget()
    yield
    phone_region.forget()


async def _org(session, country: str | None) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Region", slug=f"region-{org_id.hex[:8]}"))
    await session.commit()
    set_org_context(session, org_id)
    if country is not None:
        session.add(KycProfile(id=uuid.uuid4(), org_id=org_id, status="approved", country=country))
        await session.commit()
    return org_id


async def test_region_comes_from_the_verified_country(session):
    assert await phone_region.for_org(session, await _org(session, "GB")) == "GB"
    assert await phone_region.for_org(session, await _org(session, "US")) == "US"
    # Not verified yet: the platform default, not a crash.
    assert await phone_region.for_org(session, await _org(session, None)) == "US"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("020 7946 0958", "+442079460958"),
        ("07911 123456", "+447911123456"),
        # The dangerous one: valid as a US number too, and it used to become +1 207 946 0958.
        ("2079460958", "+442079460958"),
        ("+447911123456", "+447911123456"),
    ],
)
async def test_uk_workspace_reads_uk_numbers(session, raw, expected):
    region = await phone_region.for_org(session, await _org(session, "GB"))
    e164, reason = normalize_phone(raw, region)
    assert (e164, reason) == (expected, None)


async def test_us_workspace_is_unchanged(session):
    region = await phone_region.for_org(session, await _org(session, "US"))
    assert normalize_phone("512 555 0100", region) == ("+15125550100", None)
    assert normalize_phone("2079460958", region) == ("+12079460958", None)


async def test_the_cache_does_not_outlive_a_country_change(session):
    org_id = await _org(session, "US")
    assert await phone_region.for_org(session, org_id) == "US"
    set_org_context(session, org_id)
    import sqlalchemy as sa

    profile = (await session.execute(sa.select(KycProfile))).scalars().first()
    profile.country = "GB"
    await session.commit()
    phone_region.forget(org_id)  # what a country change must do
    assert await phone_region.for_org(session, org_id) == "GB"
