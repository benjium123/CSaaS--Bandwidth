"""P44e: mandatory 911 address per number."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db.base import set_org_context
from app.errors import PermissionDeniedError
from app.models import EmergencyAddress, Org, OrgNumber
from app.services import e911
from tests.conftest import make_settings

ADDRESS = {
    "label": "HQ",
    "caller_name": "Sabine Property Group",
    "line1": "100 Main St",
    "line2": "Suite 5",
    "city": "Dallas",
    "state": "tx",
    "postal_code": "75201",
}


class FakeTelnyx:
    def __init__(self, handler):
        self.base_url = "https://api.telnyx.test/v2"
        self.api_key = "k"
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _get_client(self):
        return self._client


class FakeSignalWire:
    def __init__(self, handler):
        self.base_url = "https://space.signalwire.test/api/laml/2010-04-01/Accounts/proj"
        self.project_id = "proj"
        self._api_token = "tok"
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _get_client(self):
        return self._client


async def _org_with_number(session, carrier="telnyx", *, age=timedelta(days=0), status="none"):
    org = Org(id=uuid.uuid4(), name="E911 Org", slug=f"e9-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164=f"+1214555{uuid.uuid4().int % 10000:04d}",
        carrier=carrier,
        provider_ref="order-1" if carrier == "telnyx" else "num-1",  # Telnyx: the ORDER id
        e911_status=status,
        purchased_at=datetime.now(timezone.utc) - age,
    )
    session.add(number)
    await session.commit()
    return org, number


def _telnyx_ok(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path.endswith("/addresses/actions/validate"):
            return httpx.Response(200, json={"data": {"result": "valid"}})
        if path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr-tx-1"}})
        if path.endswith("/actions/enable_emergency"):
            return httpx.Response(
                202, json={"data": {"emergency": {"emergency_status": "provisioning"}}}
            )
        if request.method == "GET" and path.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": [{"id": "num-1"}]})
        if "order-1" in path:
            return httpx.Response(404)
        if path.endswith("/phone_numbers/num-1"):
            return httpx.Response(200, json={"data": {"emergency": {"emergency_status": "active"}}})
        return httpx.Response(404)

    return handler


async def test_telnyx_address_is_validated_created_and_bound(session):
    seen: list = []
    org, number = await _org_with_number(session)
    registry = {"telnyx": FakeTelnyx(_telnyx_ok(seen))}

    address = await e911.create_address(session, registry, org.id, ADDRESS, user_id=None)
    await session.commit()

    assert address.status == "valid"
    assert address.state == "TX"
    assert address.carrier_refs == {"telnyx": "addr-tx-1"}
    await session.refresh(number)
    assert number.emergency_address_id == address.id
    assert number.e911_status == "pending"
    enable = [s for s in seen if s[1].endswith("/actions/enable_emergency")][0]
    assert json.loads(enable[2]) == {"emergency_enabled": True, "emergency_address_id": "addr-tx-1"}

    # The sweeper follows provisioning to active.
    assert await e911.refresh_pending(session, registry) == 1
    await session.refresh(number)
    assert number.e911_status == "active"


async def test_unverifiable_address_comes_back_with_suggestions(session):
    org, _number = await _org_with_number(session)

    def handler(request):
        return httpx.Response(
            200,
            json={"data": {"result": "invalid", "suggested": {"street_address": "100 N Main St"}}},
        )

    with pytest.raises(e911.CarrierAddressRefused) as err:
        await e911.create_address(
            session, {"telnyx": FakeTelnyx(handler)}, org.id, ADDRESS, user_id=None
        )
    assert err.value.suggestions == [{"street_address": "100 N Main St"}]


async def test_signalwire_address_splits_the_street_and_binds(session):
    seen: list = []
    org, number = await _org_with_number(session, carrier="signalwire")

    def handler(request):
        seen.append((request.url.path, request.content))
        if request.url.path == "/api/relay/rest/addresses":
            return httpx.Response(201, json={"id": "sw-addr-1"})
        if request.url.path.endswith("/e911_address"):
            return httpx.Response(200, json={"id": "num-1"})
        return httpx.Response(404)

    await e911.create_address(
        session, {"signalwire": FakeSignalWire(handler)}, org.id, ADDRESS, user_id=None
    )
    await session.commit()
    created = json.loads(seen[0][1])
    assert created["street_number"] == "100" and created["street_name"] == "Main St"
    assert created["address_number"] == "Suite 5"
    await session.refresh(number)
    assert number.e911_status == "pending"


async def test_non_us_address_is_refused(session):
    org, _ = await _org_with_number(session)
    with pytest.raises(Exception, match="US numbers only"):
        await e911.create_address(session, {}, org.id, {**ADDRESS, "country": "GB"}, user_id=None)


async def test_calls_need_911_after_the_grace_period(session):
    settings = make_settings(
        e911_enforced=True, e911_enforcement_start="2020-01-01", e911_grace_days=7
    )
    # A number bought a day ago still has its own 7-day grace.
    org, fresh = await _org_with_number(session, age=timedelta(days=1))
    await e911.require_e911(session, settings, org.id, fresh.e164, "+12145550000")
    org, number = await _org_with_number(session, age=timedelta(days=10))
    with pytest.raises(PermissionDeniedError) as err:
        await e911.require_e911(session, settings, org.id, number.e164, "+12145550000")
    assert err.value.code == "e911_required"
    # 911 itself is never refused.
    await e911.require_e911(session, settings, org.id, number.e164, "911")
    # Provisioning (pending) may call.
    number.e911_status = "pending"
    await session.commit()
    await e911.require_e911(session, settings, org.id, number.e164, "+12145550000")


async def test_old_numbers_get_the_grace_period(session):
    start = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    settings = make_settings(e911_enforced=True, e911_enforcement_start=start, e911_grace_days=7)
    org, number = await _org_with_number(session, age=timedelta(days=300))
    await e911.require_e911(session, settings, org.id, number.e164, "+12145550000")


async def test_address_of_another_workspace_cannot_be_bound(session):
    org_a, number_a = await _org_with_number(session)
    org_b, _ = await _org_with_number(session)
    set_org_context(session, org_b.id)
    foreign = EmergencyAddress(
        id=uuid.uuid4(), org_id=org_b.id, caller_name="B", line1="1 A St", city="Dallas",
        state="TX", postal_code="75201", status="valid",
    )
    session.add(foreign)
    await session.commit()
    with pytest.raises(PermissionDeniedError):
        await e911.assign(session, {}, number_a, foreign)
