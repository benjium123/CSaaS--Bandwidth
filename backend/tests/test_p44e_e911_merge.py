"""P44e on top of the E911 base: SignalWire numbers, auto-registering numbers that have no
address, and the call gate (no working 911 address -> no ordinary outbound calls)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import PermissionDeniedError
from app.models import OrgNumber
from app.services import e911
from tests.conftest import make_settings
from tests.test_e911 import ADDRESS, _org_with_number, app_with_dial_log, telnyx  # noqa: F401


def _settings(**kw):
    return make_settings(
        telnyx_api_key="telnyx-test-key",
        signalwire_project_id="proj",
        signalwire_api_token="tok",
        signalwire_space_url="example.signalwire.com",
        **kw,
    )


class FakeSignalWire:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/addresses/actions/validate"):  # Telnyx validation
            return httpx.Response(200, json={"data": {"result": "valid"}})
        if "api.telnyx.com" in str(request.url) and path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr_123"}})
        if path.endswith("/api/relay/rest/addresses"):
            return httpx.Response(201, json={"id": "sw_addr_1"})
        if path.endswith("/e911_address"):
            return httpx.Response(200, json={"id": "sw-num-1"})
        if path.endswith("/api/relay/rest/phone_numbers/sw-num-1"):
            return httpx.Response(200, json={"id": "sw-num-1", "e911_status": "active"})
        return httpx.Response(404, json={})

    def paths(self, suffix):
        return [r for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture
async def signalwire():
    fake = FakeSignalWire()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        fake.client = client
        yield fake


async def _sw_number(session):
    org, number = await _org_with_number(session, carrier="signalwire")
    number.provider_ref = "sw-num-1"
    await session.commit()
    return org, number


async def test_a_signalwire_number_registers_its_address_there(session, signalwire):
    org, number = await _sw_number(session)
    address = await e911.create_address(
        session, _settings(), org.id, ADDRESS, client=signalwire.client
    )
    state = await e911.enable(session, _settings(), number, address, client=signalwire.client)
    assert state["status"] == "provisioning"
    assert address.signalwire_address_id == "sw_addr_1"
    sent = json.loads(signalwire.paths("/api/relay/rest/addresses")[0].content)
    assert (sent["street_number"], sent["street_name"]) == ("600", "Congress Ave")
    assert sent["address_number"] == "Suite 200"
    bound = json.loads(signalwire.paths("/e911_address")[0].content)
    assert bound == {"e911_address_id": "sw_addr_1"}

    # A second number reuses the SignalWire address; the sweeper follows both to active.
    await e911.enable(session, _settings(), number, address, client=signalwire.client)
    assert len(signalwire.paths("/api/relay/rest/addresses")) == 1
    await e911.tick(get_sessionmaker(), _settings(), client=signalwire.client)
    async with get_sessionmaker()() as s:
        set_org_context(s, org.id)
        assert e911.status_of(await s.get(OrgNumber, number.id))["status"] == "active"


async def test_the_sweeper_registers_numbers_that_have_no_address(session, telnyx):  # noqa: F811
    org, number = await _org_with_number(session)
    await e911.create_address(session, _settings(), org.id, ADDRESS, client=telnyx.client)
    await session.commit()
    telnyx.enable_status = 200
    counts = await e911.tick(get_sessionmaker(), _settings(), client=telnyx.client)
    assert counts["assigned"] == 1
    async with get_sessionmaker()() as s:
        set_org_context(s, org.id)
        assert e911.status_of(await s.get(OrgNumber, number.id))["status"] in (
            "provisioning",
            "active",
        )


async def _gate(session, settings, number, to="+15125550000"):
    await e911.require_e911(session, settings, number.org_id, number.e164, to)


async def test_calls_need_a_working_911_address_after_the_grace_period(session):
    org, number = await _org_with_number(session)
    past = datetime.now(timezone.utc) - timedelta(days=60)
    number.purchased_at = past
    await session.commit()
    enforced = _settings(e911_enforced=True, e911_enforcement_start=past.date().isoformat())

    with pytest.raises(PermissionDeniedError) as err:
        await _gate(session, enforced, number)
    assert err.value.code == "e911_required"
    await _gate(session, enforced, number, to="911")  # emergency calls always go
    await _gate(session, _settings(e911_enforced=False), number)  # the off switch

    e911._set(number, status="active", address_id="x")
    await session.commit()
    await _gate(session, enforced, number)


async def test_a_new_number_gets_its_own_grace_period(session):
    org, number = await _org_with_number(session)
    number.purchased_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.commit()
    long_ago = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    await _gate(session, _settings(e911_enforced=True, e911_enforcement_start=long_ago), number)


async def test_an_api_key_cannot_dial_911(app_with_dial_log, session):  # noqa: F811
    from tests.conftest import auth_headers
    from tests.test_bugfix_area5 import _api_key
    from tests.test_e911 import _room_org

    client, dials = app_with_dial_log
    token, org, _ = await _room_org(session, client, "e911-api@example.com", "E911 API Org")
    key = await _api_key(client, token, org["id"], ["calls:place"])
    r = await client.post(
        "/api/v1/calls",
        json={"to": "911", "via": "carrier"},
        headers={"Authorization": f"Bearer {key}"},
    )
    # POST /calls needs a signed-in person; a program's key is refused before any dial.
    assert r.status_code == 401, r.text
    assert dials == []
