"""P44f: port-in (with the hijack gate), Telnyx filing + import, port-out watch, port lock."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.errors import ConflictError, PermissionDeniedError
from app.models import KycPerson, KycProfile, Org, OrgNumber, PortRequest, SecurityAlert
from app.services import e911, porting
from app.storage.base import InMemoryObjectStore
from tests.conftest import make_settings

FORM = {
    "authorized_name": "Jane Q. Doe",
    "business_name": "Sabine Property Group, LLC",
    "account_number": "ACC-1",
    "pin": "4321",
    "billing_number": "+12145550100",
    "service_street": "100 Main St",
    "service_city": "Dallas",
    "service_state": "tx",
    "service_zip": "75201",
}
PDF = (b"%PDF-1.4 test", "application/pdf")


def _settings():
    return make_settings(credentials_master_key=Fernet.generate_key().decode())


class FakeTelnyx:
    def __init__(self, handler):
        self.base_url = "https://api.telnyx.test/v2"
        self.api_key = "k"
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _get_client(self):
        return self._client


async def _verified_org(session, *, status="approved") -> Org:
    org = Org(id=uuid.uuid4(), name="Port Org", slug=f"po-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    session.add(
        KycProfile(
            id=uuid.uuid4(), org_id=org.id, status=status, country="US",
            legal_name="Sabine Property Group LLC",
        )
    )
    session.add(KycPerson(id=uuid.uuid4(), org_id=org.id, role="owner", full_name="Jane Doe"))
    await session.commit()
    return org


def test_names_match():
    assert porting.names_match("Sabine Property Group, LLC", "sabine property group llc")
    assert porting.names_match("Doe Jane", "Jane Doe")
    assert not porting.names_match("John Smith", "Jane Doe")
    assert not porting.names_match("", "Jane Doe")


async def _request(session, org, store, **overrides):
    return await porting.create_port_in(
        session,
        _settings(),
        store,
        org.id,
        user_id=None,
        carrier=overrides.pop("carrier", "telnyx"),
        numbers=overrides.pop("numbers", ["(214) 555-0199"]),
        form={**FORM, **overrides.pop("form", {})},
        loa=PDF,
        invoice=PDF,
    )


async def test_unverified_business_cannot_port_in(session):
    org = await _verified_org(session, status="pending")
    with pytest.raises(PermissionDeniedError):
        await _request(session, org, InMemoryObjectStore())


async def test_name_must_match_the_verified_business(session):
    org = await _verified_org(session)
    with pytest.raises(PermissionDeniedError) as err:
        await _request(
            session, org, InMemoryObjectStore(),
            form={"authorized_name": "Mallory Thief", "business_name": "Other Co"},
        )
    assert err.value.code == "port_name_mismatch"


async def test_foreign_numbers_cannot_be_ported(session):
    org = await _verified_org(session)
    with pytest.raises(Exception, match="outside"):
        await _request(session, org, InMemoryObjectStore(), numbers=["+18765551234"])


async def test_number_already_on_the_platform_is_refused(session):
    org = await _verified_org(session)
    session.add(OrgNumber(id=uuid.uuid4(), org_id=org.id, e164="+12145550199", carrier="telnyx"))
    await session.commit()
    with pytest.raises(ConflictError):
        await _request(session, org, InMemoryObjectStore())


async def test_valid_request_waits_for_an_operator(session):
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _request(session, org, store)
    assert port.status == "awaiting_review"
    assert port.numbers == ["+12145550199"]
    assert port.secret_enc and "4321" not in port.secret_enc
    assert await store.exists(port.loa_media_key)
    alerts = (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "port_in_review"))
    ).scalars().all()
    assert len(alerts) == 1
    with pytest.raises(ConflictError):  # a second open request for the same number
        await _request(session, org, store)


async def test_approval_files_with_telnyx_and_the_sweeper_imports(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await porting.create_port_in(
        session, settings, store, org.id, user_id=None, carrier="telnyx",
        numbers=["+12145550199"], form=FORM, loa=PDF, invoice=PDF,
    )
    calls: list = []
    state = {"status": "in-process"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/documents"):
            return httpx.Response(200, json={"data": {"id": f"doc-{len(calls)}"}})
        if request.method == "POST" and path.endswith("/porting_orders"):
            return httpx.Response(200, json={"data": [{"id": "po-1"}]})
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body["end_user"]["admin"]["pin_passcode"] == "4321"
            assert "csaas" in body["phone_number_configuration"]["tags"]
            return httpx.Response(200, json={"data": {"id": "po-1"}})
        if path.endswith("/actions/confirm"):
            return httpx.Response(200, json={"data": {"id": "po-1"}})
        if request.method == "GET" and path.endswith("/porting_orders/po-1"):
            return httpx.Response(200, json={"data": {"status": {"value": state["status"]}}})
        if request.method == "GET" and path.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": [{"id": "tx-num-9"}]})
        return httpx.Response(404)

    registry = {"telnyx": FakeTelnyx(handler)}
    from app.repositories import users as users_repo

    operator = await users_repo.create_user(
        session, email=f"op-{uuid.uuid4().hex[:6]}@example.com", password="x" * 16
    )
    await session.commit()
    set_org_context(session, org.id)
    port = await porting.approve(session, settings, store, registry, port, operator.id)
    assert port.status == "submitted"
    assert port.carrier_ref == "po-1"
    assert ("POST", "/v2/porting_orders/po-1/actions/confirm") in calls

    assert await porting.poll_port_ins(session, settings, registry) == 1
    state["status"] = "ported"
    assert await porting.poll_port_ins(session, settings, registry) == 1
    set_org_context(session, org.id)
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == "+12145550199"))
    ).scalar_one()
    assert number.org_id == org.id and number.provider_ref == "tx-num-9"
    assert e911.status_of(number)["status"] == "missing"  # the 911 address is still required


async def test_port_out_request_alerts_and_releases_when_gone(session):
    settings = _settings()
    org = await _verified_org(session)
    number = OrgNumber(id=uuid.uuid4(), org_id=org.id, e164="+12145550123", carrier="telnyx")
    session.add(number)
    await session.commit()
    status = {"value": "pending"}

    def handler(request):
        return httpx.Response(
            200,
            json={"data": [
                {"id": "out-1", "phone_numbers": ["+12145550123"], "status": status["value"]},
                {"id": "out-2", "phone_numbers": ["+19999999999"], "status": "pending"},
            ]},
        )

    registry = {"telnyx": FakeTelnyx(handler)}
    assert await porting.poll_port_outs(session, settings, registry) == 1
    assert await porting.poll_port_outs(session, settings, registry) == 0  # alerted once
    set_org_context(session, org.id)
    alerts = (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "port_out_request"))
    ).scalars().all()
    assert len(alerts) == 1
    status["value"] = "ported"
    await porting.poll_port_outs(session, settings, registry)
    await session.refresh(number)
    assert number.is_active is False and number.status == "released"
    out = (
        await session.execute(sa.select(PortRequest).where(PortRequest.direction == "out"))
    ).scalar_one()
    assert out.status == "ported"
