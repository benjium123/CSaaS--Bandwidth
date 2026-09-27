"""Individual accounts text only from a registered number, on every carrier (hard gate)."""

from __future__ import annotations

import uuid

from app.compliance import registration
from app.db.base import set_org_context
from app.models import Org, OrgNumber


async def _org_with_number(session, account_type: str, carrier: str) -> tuple[Org, OrgNumber]:
    org = Org(id=uuid.uuid4(), name=f"{account_type} co", slug=uuid.uuid4().hex)
    org.account_type = account_type
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164=f"+1214555{uuid.uuid4().int % 10_000:04d}",
        carrier=carrier,
        status="active",
        is_active=True,
    )
    session.add(number)
    await session.commit()
    return org, number


async def test_individual_unregistered_number_is_refused_on_every_carrier(session):
    for carrier in ("bandwidth", "signalwire"):
        org, number = await _org_with_number(session, "individual", carrier)
        allowed, reason = await registration.check_number_may_send(session, org.id, number)
        assert not allowed and "Individual accounts" in reason
        ok, refused = await registration.partition_by_eligibility(session, [number])
        assert ok == [] and "Individual accounts" in refused[number.e164]


async def test_business_unregistered_number_is_still_allowed_with_a_warning(session):
    org, number = await _org_with_number(session, "business", "bandwidth")
    allowed, _ = await registration.check_number_may_send(session, org.id, number)
    assert allowed
    ok, refused = await registration.partition_by_eligibility(session, [number])
    assert ok == [number] and refused == {}
