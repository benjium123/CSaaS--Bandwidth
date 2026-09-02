"""Phase 18: number provisioning for Bandwidth and SignalWire, plus async poll service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, ValidationFailedError
from app.main import create_app
from app.models import Org, OrgMembership, OrgNumber, ProviderAccount, Role
from app.providers.bandwidth.numbers import BandwidthNumberProviderMixin, _xml_escape
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult, parse_cost_cents
from app.providers.plivo.numbers import PlivoNumberProviderMixin
from app.providers.probes import ProbeResult
from app.providers.registry import CarrierRegistry, build_registry
from app.providers.signalwire.numbers import SignalWireNumberProviderMixin
from app.providers.telnyx.adapter import TelnyxMessagingCarrier
from app.providers.telnyx.numbers import TelnyxNumberProviderMixin
from app.providers.registry_org import build_registry_for_org
from app.repositories import users as users_repo
from app.services import credentials as credentials_svc
from app.services import provider_accounts as provider_accounts_svc
from app.services import sweeper as sweeper_svc
from app.services.number_orders import poll_pending_number_orders
from tests.conftest import auth_headers, create_org, make_settings, register_and_login


class BandwidthHarness(BandwidthNumberProviderMixin):
    """Minimal composing class for the Bandwidth provisioning mixin."""

    name = "bandwidth"

    def __init__(self, client: httpx.AsyncClient):
        self.account_id = "acct-123"
        self._auth = ("api_user", "api_pass")
        self.site_id = "site-1"
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._client


class SignalWireHarness(SignalWireNumberProviderMixin):
    """Minimal composing class for the SignalWire provisioning mixin."""

    name = "signalwire"

    def __init__(self, client: httpx.AsyncClient):
        self.project_id = "proj-1"
        self._api_token = "token-1"
        self.base_url = "https://example.signalwire.com/api/laml/2010-04-01/Accounts/proj-1"
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._client


_DETAIL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<SearchResult>
  <TelephoneNumberDetailList>
    <TelephoneNumberDetail>
      <FullNumber>2145550100</FullNumber>
      <City>Dallas</City>
      <State>TX</State>
    </TelephoneNumberDetail>
  </TelephoneNumberDetailList>
</SearchResult>
"""

_LIST_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<SearchResult>
  <TelephoneNumberList>
    <TelephoneNumber>8175550111</TelephoneNumber>
  </TelephoneNumberList>
</SearchResult>
"""


async def test_bandwidth_search_parses_both_xml_shapes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, content=_DETAIL_XML, headers={"Content-Type": "application/xml"})
        return httpx.Response(200, content=_LIST_XML, headers={"Content-Type": "application/xml"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        first = await carrier.search_numbers(NumberSearch(area_code="214"))
        second = await carrier.search_numbers(NumberSearch(area_code="817"))

    assert len(calls) == 2
    assert len(first) == 1
    assert first[0].e164 == "+12145550100"
    assert first[0].region == "TX"
    assert first[0].locality == "Dallas"
    assert first[0].capabilities == {"sms": True, "mms": True, "voice": True}
    assert first[0].monthly_cost_cents is None
    assert first[0].setup_cost_cents is None

    assert len(second) == 1
    assert second[0].e164 == "+18175550111"


async def test_bandwidth_search_drops_empty_or_malformed_numbers():
    """A FullNumber/TelephoneNumber entry with no real 10-digit number (blank, or junk
    with too few digits) must be dropped, never turned into a synthetic "+1..." result
    with fewer than 10 digits after it."""
    xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<SearchResult>
  <TelephoneNumberDetailList>
    <TelephoneNumberDetail>
      <FullNumber></FullNumber>
      <City>Nowhere</City>
    </TelephoneNumberDetail>
    <TelephoneNumberDetail>
      <FullNumber>2145550177</FullNumber>
      <City>Dallas</City>
      <State>TX</State>
    </TelephoneNumberDetail>
  </TelephoneNumberDetailList>
  <TelephoneNumberList>
    <TelephoneNumber>N/A</TelephoneNumber>
    <TelephoneNumber>8175550188</TelephoneNumber>
  </TelephoneNumberList>
</SearchResult>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml, headers={"Content-Type": "application/xml"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        results = await carrier.search_numbers(NumberSearch(area_code="214"))

    assert {n.e164 for n in results} == {"+12145550177", "+18175550188"}


async def test_bandwidth_search_wraps_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        with pytest.raises(FeatureUnavailableError, match="unreachable"):
            await carrier.search_numbers(NumberSearch(area_code="214"))


async def test_bandwidth_search_401_403_raise_clear_auth_error():
    async def run(code: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(code)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            carrier = BandwidthHarness(http)
            await carrier.search_numbers(NumberSearch(area_code="214"))

    for code in (401, 403):
        with pytest.raises(ValidationFailedError, match="OAuth client credentials"):
            await run(code)


async def test_bandwidth_order_returns_pending_order_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/orders" in request.url.path
        body = request.content.decode()
        assert "csaas +12145550199" in body
        assert "site-1" in body
        assert "2145550199" in body
        return httpx.Response(
            200,
            content="""\
<?xml version="1.0" encoding="UTF-8"?>
<OrderResponse>
  <Order><id>order-42</id></Order>
  <OrderStatus>RECEIVED</OrderStatus>
</OrderResponse>
""",
            headers={"Content-Type": "application/xml"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        result = await carrier.order_number("+12145550199")

    assert result.status == "pending"
    assert result.provider_ref == "order-42"
    assert result.capabilities == {"sms": True, "mms": True, "voice": True}
    assert result.monthly_cost_cents is None
    assert result.setup_cost_cents is None


async def test_bandwidth_order_200_with_error_list_raises_with_description():
    """A 2xx HTTP response can still carry a Bandwidth-level rejection (ErrorList) -
    that must fail loudly with the carrier's own Description, not silently persist a
    "pending" row for an order that was never actually accepted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="""\
<?xml version="1.0" encoding="UTF-8"?>
<OrderResponse>
  <Order><id>order-43</id></Order>
  <OrderStatus>RECEIVED</OrderStatus>
  <ErrorList><Error><Description>Number no longer available</Description></Error></ErrorList>
</OrderResponse>
""",
            headers={"Content-Type": "application/xml"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        with pytest.raises(ValidationFailedError, match="Number no longer available"):
            await carrier.order_number("+12145550199")


async def test_bandwidth_order_200_with_empty_id_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="""\
<?xml version="1.0" encoding="UTF-8"?>
<OrderResponse>
  <OrderStatus>RECEIVED</OrderStatus>
</OrderResponse>
""",
            headers={"Content-Type": "application/xml"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        with pytest.raises(ValidationFailedError):
            await carrier.order_number("+12145550199")


async def test_bandwidth_order_401_403_raise_clear_auth_error():
    async def run(code: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(code)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            carrier = BandwidthHarness(http)
            await carrier.order_number("+12145550199")

    for code in (401, 403):
        with pytest.raises(ValidationFailedError, match="OAuth client credentials"):
            await run(code)


async def test_bandwidth_order_status_complete_and_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders/order-ok"):
            return httpx.Response(
                200,
                content="<OrderStatus>COMPLETE</OrderStatus>",
                headers={"Content-Type": "application/xml"},
            )
        return httpx.Response(
            200,
            # A real Bandwidth order-status body has ONE root element - the OrderStatus
            # and ErrorList fields are siblings inside it, not two top-level documents.
            content="""\
<?xml version="1.0" encoding="UTF-8"?>
<Order>
<OrderStatus>FAILED</OrderStatus>
<ErrorList><Error><Description>Inventory exhausted</Description></Error></ErrorList>
</Order>
""",
            headers={"Content-Type": "application/xml"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        complete = await carrier.order_status("order-ok")
        failed = await carrier.order_status("order-bad")

    assert complete.status == "active"
    assert complete.detail is None
    assert failed.status == "failed"
    assert failed.detail == "Inventory exhausted"


async def test_bandwidth_order_status_received_backordered_partial_are_pending():
    """Every non-terminal Bandwidth OrderStatus value must resolve to "pending" via the
    REAL mixin (not a hand-rolled dataclass) - COMPLETE and FAILED are the only two
    terminal states; sitting on any of RECEIVED/BACKORDERED/PARTIAL forever without
    reaching one of those must never get treated as done."""

    def handler(request: httpx.Request) -> httpx.Response:
        raw_status = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            content=f"<OrderStatus>{raw_status}</OrderStatus>",
            headers={"Content-Type": "application/xml"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        received = await carrier.order_status("RECEIVED")
        backordered = await carrier.order_status("BACKORDERED")
        partial = await carrier.order_status("PARTIAL")

    for result, raw in ((received, "RECEIVED"), (backordered, "BACKORDERED"), (partial, "PARTIAL")):
        assert result.status == "pending"
        assert result.detail == raw


async def test_bandwidth_order_status_url_quotes_provider_ref():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx.URL.path is the DECODED path - the wire-encoded form (what actually
        # protects against a stray "/" being read as a path separator) is raw_path.
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(
            200,
            content="<OrderStatus>COMPLETE</OrderStatus>",
            headers={"Content-Type": "application/xml"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthHarness(http)
        await carrier.order_status("order/with a slash & space")

    # A raw "/" or " " in provider_ref must never be interpreted as a path separator or
    # break the request line - quote(..., safe="") escapes everything.
    assert b"order%2Fwith%20a%20slash%20%26%20space" in captured["raw_path"]


async def test_bandwidth_release_2xx_404_and_409_are_idempotent():
    async def run(code: int) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert "/disconnects" in request.url.path
            return httpx.Response(code)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            carrier = BandwidthHarness(http)
            await carrier.release_number("+12145550199")

    await run(200)
    await run(404)
    # 409 means Bandwidth already considers the number gone (e.g. a disconnect order
    # already in flight) - release is idempotent by definition and must not fail a
    # repeated sweep over it.
    await run(409)
    with pytest.raises(ValidationFailedError):
        await run(400)


async def test_bandwidth_release_401_403_raise_clear_auth_error():
    async def run(code: int) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(code)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            carrier = BandwidthHarness(http)
            await carrier.release_number("+12145550199")

    for code in (401, 403):
        with pytest.raises(ValidationFailedError, match="OAuth client credentials"):
            await run(code)


def test_bandwidth_xml_escape_helper():
    assert _xml_escape("A & B < C") == "A &amp; B &lt; C"
    # A literal "&" in RAW input - even one that happens to spell out an entity name -
    # must ALWAYS be escaped. Treating "&amp;" in a raw value as "already escaped" and
    # passing it through unescaped would let attacker-supplied text smuggle entities
    # into the XML body.
    assert _xml_escape("A &amp; B") == "A &amp;amp; B"


async def test_signalwire_search_order_release():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "AvailablePhoneNumbers" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "available_phone_numbers": [
                        {
                            "phone_number": "+12145550123",
                            "region": "TX",
                            "locality": "Dallas",
                            "capabilities": {"SMS": True, "MMS": True, "voice": True},
                        }
                    ]
                },
            )
        if request.method == "POST" and "IncomingPhoneNumbers" in str(request.url):
            return httpx.Response(
                201,
                json={
                    "sid": "PN123",
                    "phone_number": "+12145550123",
                    "capabilities": {"sms": True, "mms": True, "voice": True},
                },
            )
        if request.method == "GET" and "IncomingPhoneNumbers" in request.url.path and "PN" not in request.url.path:
            return httpx.Response(
                200,
                json={"incoming_phone_numbers": [{"sid": "PN123"}]},
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = SignalWireHarness(http)

        found = await carrier.search_numbers(NumberSearch(area_code="214"))
        assert len(found) == 1
        assert found[0].e164 == "+12145550123"
        assert found[0].region == "TX"
        assert found[0].locality == "Dallas"
        assert found[0].capabilities == {"sms": True, "mms": True, "voice": True}
        assert found[0].monthly_cost_cents is None
        assert found[0].setup_cost_cents is None

        order = await carrier.order_number("+12145550123")
        assert order.status == "active"
        assert order.provider_ref == "PN123"
        assert order.capabilities == {"sms": True, "mms": True, "voice": True}

        await carrier.release_number("+12145550123")

    assert any(r.method == "DELETE" for r in requests)


async def test_signalwire_search_wraps_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = SignalWireHarness(http)
        with pytest.raises(FeatureUnavailableError, match="unreachable"):
            await carrier.search_numbers(NumberSearch(area_code="214"))


async def test_signalwire_release_url_quotes_provider_ref():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = SignalWireHarness(http)
        await carrier.release_number("+12145550123", provider_ref="PN/with a slash")

    assert b"PN%2Fwith%20a%20slash" in captured["raw_path"]


@dataclass(frozen=True)
class _StatusResult:
    status: str
    detail: str | None = None


class _FakeOrderCarrier:
    name = "bandwidth"

    def __init__(self, results: dict[str, _StatusResult]):
        self.results = results
        self.calls: list[str] = []

    async def order_status(self, provider_ref: str) -> _StatusResult:
        self.calls.append(provider_ref)
        return self.results[provider_ref]


class _NoOrderStatusCarrier:
    name = "plivo"


async def _make_org(session) -> uuid.UUID:
    """A real Org row. OrgNumber.org_id is a genuine FK to orgs.id and this suite runs
    with SQLite's foreign_keys PRAGMA on (tests/conftest.py) - a random uuid4 org_id
    fails on INSERT."""
    org = Org(id=uuid.uuid4(), name="P18 Org", slug=f"p18-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()
    return org.id


async def _make_number(
    session,
    *,
    org_id: uuid.UUID,
    carrier: str,
    e164: str,
    provider_ref: str,
) -> OrgNumber:
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164=e164,
        carrier=carrier,
        status="pending",
        provider_ref=provider_ref,
        capabilities={},
    )
    session.add(number)
    return number


async def test_poll_pending_number_orders_transitions_and_limit(session):
    org_id = await _make_org(session)
    session.info["org_id"] = org_id

    statuses: dict[str, _StatusResult] = {}
    for i in range(30):
        if i < 10:
            statuses[f"bw-{i}"] = _StatusResult(status="active")
        elif i < 20:
            statuses[f"bw-{i}"] = _StatusResult(status="failed", detail="No numbers")
        else:
            statuses[f"bw-{i}"] = _StatusResult(status="pending", detail="RECEIVED")

    fake = _FakeOrderCarrier(statuses)
    registry = CarrierRegistry({"bandwidth": fake}, primary="bandwidth")

    for i in range(30):
        await _make_number(
            session,
            org_id=org_id,
            carrier="bandwidth",
            e164=f"+1214555{i:04d}",
            provider_ref=f"bw-{i}",
        )
    await session.commit()
    session.info.pop("org_id", None)

    polled = await poll_pending_number_orders(session, registry, limit=25)

    assert polled == 25
    assert len(fake.calls) == 25

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OrgNumber).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    by_ref = {r.provider_ref: r for r in rows}

    for i in range(10):
        row = by_ref[f"bw-{i}"]
        assert row.status == "active"
        assert row.is_active is True
        assert row.order_detail is None

    for i in range(10, 20):
        row = by_ref[f"bw-{i}"]
        assert row.status == "failed"
        assert row.is_active is False
        assert row.order_detail == "No numbers"

    for i in range(20, 25):
        row = by_ref[f"bw-{i}"]
        assert row.status == "pending"
        assert row.order_detail == "RECEIVED"

    for i in range(25, 30):
        row = by_ref[f"bw-{i}"]
        assert row.status == "pending"
        assert row.order_detail is None


async def test_poll_pending_skips_carrier_without_order_status(session):
    org_id = await _make_org(session)
    session.info["org_id"] = org_id

    fake = _FakeOrderCarrier({"bw-1": _StatusResult(status="active")})
    plain = _NoOrderStatusCarrier()
    registry = CarrierRegistry({"bandwidth": fake, "plivo": plain}, primary="bandwidth")

    await _make_number(
        session,
        org_id=org_id,
        carrier="bandwidth",
        e164="+12145550001",
        provider_ref="bw-1",
    )
    await _make_number(
        session,
        org_id=org_id,
        carrier="plivo",
        e164="+12145550002",
        provider_ref="plivo-1",
    )
    await session.commit()
    session.info.pop("org_id", None)

    polled = await poll_pending_number_orders(session, registry, limit=10)

    assert polled == 1
    assert len(fake.calls) == 1

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OrgNumber).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    by_ref = {r.provider_ref: r for r in rows}

    assert by_ref["bw-1"].status == "active"
    assert by_ref["plivo-1"].status == "pending"
    assert by_ref["plivo-1"].order_detail is None


# ==================================================================================
# P18: cents parsing - the shared helper, and Telnyx/Plivo populating the new fields
# ==================================================================================
def test_parse_cost_cents_helper():
    assert parse_cost_cents("1.00") == 100
    assert parse_cost_cents("0.80") == 80
    assert parse_cost_cents("2") == 200
    assert parse_cost_cents(None) is None
    assert parse_cost_cents("") is None
    assert parse_cost_cents("   ") is None
    assert parse_cost_cents("not-a-number") is None


def test_parse_cost_cents_rejects_negative_and_oversized_values():
    # A negative "cost" is nonsensical for a per-number price - never store it.
    assert parse_cost_cents("-1.00") is None
    assert parse_cost_cents("-0.01") is None
    # Scientific notation Decimal happily parses ("1e999" is a valid, enormous Decimal,
    # no InvalidOperation) - the magnitude bound is what must catch it.
    assert parse_cost_cents("1e999") is None
    assert parse_cost_cents("1e30") is None
    # Just past and just at the $10,000,000.00 bound.
    assert parse_cost_cents("10000000.01") is None
    assert parse_cost_cents("10000000.00") == 1_000_000_000


class TelnyxHarness(TelnyxNumberProviderMixin):
    name = "telnyx"

    def __init__(self, client: httpx.AsyncClient):
        self.api_key = "test-telnyx-key"
        self.base_url = "https://api.telnyx.com/v2"
        self.messaging_profile_id = ""
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._client


class PlivoHarness(PlivoNumberProviderMixin):
    name = "plivo"

    def __init__(self, client: httpx.AsyncClient):
        self._auth = ("MAxxxx", "plivo-token")
        self.base_url = "https://api.plivo.com/v1/Account/MAxxxx"
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._client


async def test_telnyx_order_status_success_failed_pending():
    """P18 Gap: Telnyx's number_orders can be asynchronous - order_status must resolve
    "success" -> active, "failed"/"cancelled" -> failed (with the carrier's own error
    text), and anything else (still pending, or an unrecognised value) -> pending."""

    def handler(request: httpx.Request) -> httpx.Response:
        order_id = request.url.path.rsplit("/", 1)[-1]
        if order_id == "order-ok":
            return httpx.Response(200, json={"data": {"id": order_id, "status": "success"}})
        if order_id == "order-failed":
            return httpx.Response(
                200,
                json={
                    "data": {"id": order_id, "status": "failed"},
                    "errors": [{"detail": "Number no longer available"}],
                },
            )
        if order_id == "order-cancelled":
            return httpx.Response(200, json={"data": {"id": order_id, "status": "cancelled"}})
        return httpx.Response(200, json={"data": {"id": order_id, "status": "pending"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = TelnyxHarness(http)
        success = await carrier.order_status("order-ok")
        failed = await carrier.order_status("order-failed")
        cancelled = await carrier.order_status("order-cancelled")
        pending = await carrier.order_status("order-pending")

    assert success.status == "active"
    assert success.detail is None
    assert failed.status == "failed"
    assert failed.detail == "Number no longer available"
    assert cancelled.status == "failed"
    assert pending.status == "pending"


async def test_telnyx_order_status_non_200_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = TelnyxHarness(http)
        with pytest.raises(FeatureUnavailableError):
            await carrier.order_status("order-x")


async def test_telnyx_order_status_url_quotes_provider_ref():
    """Mirrors test_bandwidth_order_status_url_quotes_provider_ref: a raw "/" or ".."
    in provider_ref must never be interpreted as a path separator / traversal, or break
    the request line - quote(..., safe="") escapes everything. httpx.URL.path is the
    DECODED path (not what protects against this); raw_path is the actual wire form."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(200, json={"data": {"id": "order-x", "status": "success"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = TelnyxHarness(http)
        await carrier.order_status("order/with a slash and ../traversal")

    assert b"order%2Fwith%20a%20slash%20and%20..%2Ftraversal" in captured["raw_path"]
    # And, just as importantly, the RAW "/" and ".." must not appear unescaped anywhere
    # past the fixed "/number_orders/" prefix - i.e. no actual extra path segments.
    prefix = b"/v2/number_orders/"
    assert captured["raw_path"].startswith(prefix)
    assert b"/" not in captured["raw_path"][len(prefix):].replace(b"%2F", b"")


async def test_telnyx_messaging_carrier_now_has_order_status():
    """The composed adapter (not just the mixin in isolation) exposes order_status -
    this is what lets the route's `is_active` rule and the sweeper's SQL-side carrier
    filter (services/number_orders.py) treat Telnyx as pollable."""
    carrier = TelnyxMessagingCarrier(api_key="k")
    assert hasattr(carrier, "order_status")


async def test_telnyx_search_populates_cost_cents_present_and_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "phone_number": "+12145550300",
                        "phone_number_type": "local",
                        "region_information": {"administrative_area": "TX", "locality": "Dallas"},
                        "cost_information": {"monthly_cost": "1.00", "upfront_cost": "0.50"},
                        "features": [{"name": "sms"}, {"name": "voice"}],
                    },
                    {
                        # No cost_information at all - a real Telnyx response can omit it.
                        "phone_number": "+12145550301",
                        "phone_number_type": "local",
                        "features": [{"name": "sms"}],
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = TelnyxHarness(http)
        results = await carrier.search_numbers(NumberSearch(area_code="214"))

    assert len(results) == 2
    priced, unpriced = results[0], results[1]
    assert priced.e164 == "+12145550300"
    assert priced.monthly_cost_cents == 100
    assert priced.setup_cost_cents == 50
    assert unpriced.e164 == "+12145550301"
    assert unpriced.monthly_cost_cents is None
    assert unpriced.setup_cost_cents is None


async def test_plivo_search_populates_monthly_cost_cents_and_leaves_setup_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "objects": [
                    {
                        "number": "12145550302",
                        "region": "Texas, United States",
                        "monthly_rental_rate": "0.80",
                        "sms_enabled": True,
                        "mms_enabled": False,
                        "voice_enabled": True,
                    },
                    {
                        # No monthly_rental_rate at all.
                        "number": "12145550303",
                        "sms_enabled": True,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = PlivoHarness(http)
        results = await carrier.search_numbers(NumberSearch(area_code="214"))

    assert len(results) == 2
    priced, unpriced = results[0], results[1]
    assert priced.monthly_cost_cents == 80
    assert priced.setup_cost_cents is None
    assert unpriced.monthly_cost_cents is None
    assert unpriced.setup_cost_cents is None


# ==================================================================================
# P18: POST /numbers/order - persistence of purchase facts, provider_account_id
# ==================================================================================
@dataclass
class _FakeNumberCarrier:
    """A NumberProvider-shaped carrier the numbers route can order/search/release
    through, installed directly into app.state.carriers (the env-configured position -
    never a P17 DB account)."""

    name: str = "fakecarrier"
    order_result: OrderResult | None = None
    search_result: list = field(default_factory=list)
    released: list = field(default_factory=list)

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]:
        return self.search_result

    async def order_number(self, e164: str) -> OrderResult:
        assert self.order_result is not None, "test forgot to script an order_result"
        return self.order_result

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        self.released.append((e164, provider_ref))


@dataclass
class _FakeNumberCarrierPollable(_FakeNumberCarrier):
    """Same as _FakeNumberCarrier, but WITH order_status - so the route's
    `is_active = result.status == "active" or not hasattr(carrier_obj, "order_status")`
    rule treats a pending order as genuinely not-yet-routable (there IS a path to
    resolve it later, via the sweeper)."""

    async def order_status(self, provider_ref: str):  # pragma: no cover - never invoked
        raise NotImplementedError("not exercised by the order route itself")


@pytest.fixture
async def app_with_number_carrier(engine):
    """App wired with a NumberProvider-capable FakeCarrier as the (env-position)
    primary carrier. Returns (client, fake, application)."""
    app_settings = make_settings()
    application = create_app(app_settings)
    fake = _FakeNumberCarrier()
    application.state.carriers = CarrierRegistry({fake.name: fake}, primary=fake.name)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


async def test_order_route_env_carrier_persists_costs_and_leaves_provider_account_null(
    app_with_number_carrier,
):
    """A carrier with NO order_status (unpollable) that reports "pending" keeps the
    pre-P18 routable default (is_active True) - there is no sweeper path that will ever
    resolve it, so marking it inactive would strand the number forever."""
    client, fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-order-env@example.com")
    org = await create_org(client, token, "Org P18 Env")
    headers = auth_headers(token, org["id"])

    fake.order_result = OrderResult(
        e164="+12145550188",
        provider_ref="order-env-1",
        status="pending",
        capabilities={"sms": True, "mms": True, "voice": True},
        monthly_cost_cents=199,
        setup_cost_cents=99,
    )

    resp = await client.post(
        "/api/v1/numbers/order",
        json={"e164": "+12145550188", "carrier": "fakecarrier"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # No P17 DB account exists for "fakecarrier" at all - registry_org.db_backed_providers
    # must never attribute this purchase to one.
    assert body["provider_account_id"] is None
    assert body["provider_account_label"] is None
    assert body["monthly_cost_cents"] == 199
    assert body["purchase_cost_cents"] == 99
    assert body["purchased_at"] is not None
    assert body["status"] == "pending"
    assert not hasattr(fake, "order_status")
    assert body["is_active"] is True


async def test_order_route_pollable_carrier_pending_result_is_not_active(engine):
    """The mirror case: a carrier that DOES implement order_status (Bandwidth/Telnyx
    shaped) reporting "pending" is genuinely not yet routable - is_active must be False,
    since the sweeper (services/number_orders.py) can and will resolve it later."""
    app_settings = make_settings()
    application = create_app(app_settings)
    fake = _FakeNumberCarrierPollable(name="pollablecarrier")
    application.state.carriers = CarrierRegistry({fake.name: fake}, primary=fake.name)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "p18-order-pollable@example.com")
        org = await create_org(client, token, "Org P18 Pollable")
        headers = auth_headers(token, org["id"])

        fake.order_result = OrderResult(
            e164="+12145550701",
            provider_ref="order-pollable-1",
            status="pending",
            capabilities={"sms": True, "mms": True, "voice": True},
        )

        resp = await client.post(
            "/api/v1/numbers/order",
            json={"e164": "+12145550701", "carrier": "pollablecarrier"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert hasattr(fake, "order_status")
        assert body["status"] == "pending"
        assert body["is_active"] is False
    assert body["order_detail"] == "pending"


async def test_order_route_active_result_has_no_order_detail_and_is_active(
    app_with_number_carrier,
):
    client, fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-order-active@example.com")
    org = await create_org(client, token, "Org P18 Active")
    headers = auth_headers(token, org["id"])

    fake.order_result = OrderResult(
        e164="+12145550189",
        provider_ref="order-env-2",
        status="active",
        capabilities={"sms": True, "mms": True, "voice": True},
        monthly_cost_cents=None,
        setup_cost_cents=None,
    )

    resp = await client.post(
        "/api/v1/numbers/order",
        json={"e164": "+12145550189", "carrier": "fakecarrier"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_active"] is True
    assert body["order_detail"] is None
    assert body["monthly_cost_cents"] is None
    assert body["purchase_cost_cents"] is None


async def test_order_route_uses_client_supplied_cost_when_carrier_reports_none(
    app_with_number_carrier,
):
    """OrderIn.monthly_cost_cents/setup_cost_cents (the cost row the client selected
    from GET /numbers/available) is used ONLY as a fallback - here the carrier's order
    response reports no cost at all, so the client-supplied figures must be persisted."""
    client, fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-order-fallback@example.com")
    org = await create_org(client, token, "Org P18 Fallback")
    headers = auth_headers(token, org["id"])

    fake.order_result = OrderResult(
        e164="+12145550190",
        provider_ref="order-env-3",
        status="active",
        capabilities={"sms": True, "mms": True, "voice": True},
        monthly_cost_cents=None,
        setup_cost_cents=None,
    )

    resp = await client.post(
        "/api/v1/numbers/order",
        json={
            "e164": "+12145550190",
            "carrier": "fakecarrier",
            "monthly_cost_cents": 149,
            "setup_cost_cents": 0,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["monthly_cost_cents"] == 149
    assert body["purchase_cost_cents"] == 0


async def test_order_route_carrier_cost_wins_over_client_supplied_cost(
    app_with_number_carrier,
):
    """The mirror case: when the carrier DOES report a cost, it wins over whatever the
    client sent - a stale search-time quote must never override the carrier's own
    order-time figure."""
    client, fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-order-carrier-wins@example.com")
    org = await create_org(client, token, "Org P18 Carrier Wins")
    headers = auth_headers(token, org["id"])

    fake.order_result = OrderResult(
        e164="+12145550191",
        provider_ref="order-env-4",
        status="active",
        capabilities={"sms": True, "mms": True, "voice": True},
        monthly_cost_cents=299,
        setup_cost_cents=50,
    )

    resp = await client.post(
        "/api/v1/numbers/order",
        json={
            "e164": "+12145550191",
            "carrier": "fakecarrier",
            "monthly_cost_cents": 100,
            "setup_cost_cents": 100,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["monthly_cost_cents"] == 299
    assert body["purchase_cost_cents"] == 50


async def test_order_route_rejects_negative_monthly_cost_cents(app_with_number_carrier):
    """OrderIn.monthly_cost_cents is Field(ge=0, ...) - a negative client-supplied cost
    must be rejected at the request-validation layer, before it ever reaches the carrier
    or the database."""
    client, _fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-order-422-negative@example.com")
    org = await create_org(client, token, "Org P18 422 Negative")
    headers = auth_headers(token, org["id"])

    resp = await client.post(
        "/api/v1/numbers/order",
        json={
            "e164": "+12145550192",
            "carrier": "fakecarrier",
            "monthly_cost_cents": -500,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_order_route_rejects_oversized_monthly_cost_cents(app_with_number_carrier):
    """OrderIn.monthly_cost_cents is Field(..., le=100_000_000) - a client-supplied cost
    above $1,000,000.00 must be rejected the same way."""
    client, _fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-order-422-oversized@example.com")
    org = await create_org(client, token, "Org P18 422 Oversized")
    headers = auth_headers(token, org["id"])

    resp = await client.post(
        "/api/v1/numbers/order",
        json={
            "e164": "+12145550193",
            "carrier": "fakecarrier",
            "monthly_cost_cents": 2**40,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_order_route_db_backed_carrier_sets_provider_account_id_and_label(
    engine, monkeypatch
):
    key = Fernet.generate_key().decode()
    app_settings = make_settings(credentials_master_key=key)
    application = create_app(app_settings)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "p18-order-db@example.com")
        org = await create_org(client, token, "Org P18 DB")
        headers = auth_headers(token, org["id"])

        created = await client.post(
            "/api/v1/provider-accounts",
            json={
                "provider": "telnyx",
                "label": "DB Telnyx",
                "credentials": {
                    "api_key": "db-secret",
                    "public_key": "db-pub",
                    "messaging_profile_id": "mp-1",
                    "voice_connection_id": "vc-1",
                },
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        account_id = created.json()["id"]

        async def fake_probe_ok(name, settings, *, client=None):
            return ProbeResult(name, True, "Credentials accepted.", "test://telnyx")

        monkeypatch.setattr(provider_accounts_svc.probes, "probe", fake_probe_ok)
        probed = await client.post(
            f"/api/v1/provider-accounts/{account_id}/probe", headers=headers
        )
        assert probed.status_code == 200, probed.text
        assert probed.json()["status"] == "active"

        async def fake_order_number(self, e164: str) -> OrderResult:
            return OrderResult(
                e164=e164,
                provider_ref="db-order-1",
                status="active",
                capabilities={"sms": True, "mms": True, "voice": True},
                monthly_cost_cents=500,
                setup_cost_cents=250,
            )

        # The order route's carrier object is a REAL TelnyxMessagingCarrier built from the
        # DB credentials by registry_org.prime_org_registry - patched at the class level
        # since the test has no handle on that instance until the request builds it.
        monkeypatch.setattr(TelnyxMessagingCarrier, "order_number", fake_order_number)

        resp = await client.post(
            "/api/v1/numbers/order",
            json={"e164": "+12145550199", "carrier": "telnyx"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["provider_account_id"] == account_id
        assert body["provider_account_label"] == "DB Telnyx"
        assert body["monthly_cost_cents"] == 500
        assert body["purchase_cost_cents"] == 250


# ==================================================================================
# P18: RBAC - numbers:manage gates order/search/release, numbers:read gates list
# ==================================================================================
async def _add_member_with_role(
    client: httpx.AsyncClient, session, org_id: uuid.UUID, email: str, permissions: list[str]
) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = Role(id=uuid.uuid4(), org_id=org_id, name=email, permissions=permissions)
    session.add(role)
    await session.flush()
    session.add(OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id))
    await session.commit()
    return token


async def test_rbac_numbers_manage_gates_order_search_release(
    app_with_number_carrier, session
):
    client, fake, _application = app_with_number_carrier
    owner_token = await register_and_login(client, "p18-rbac-owner@example.com")
    org = await create_org(client, owner_token, "Org P18 RBAC")
    org_id = uuid.UUID(org["id"])
    owner_headers = auth_headers(owner_token, org["id"])

    fake.search_result = [
        AvailableNumber(e164="+12145550400", capabilities={"sms": True, "mms": True, "voice": True})
    ]
    fake.order_result = OrderResult(
        e164="+12145550401", provider_ref="order-rbac-1", status="active"
    )
    ordered = await client.post(
        "/api/v1/numbers/order",
        json={"e164": "+12145550401", "carrier": "fakecarrier"},
        headers=owner_headers,
    )
    assert ordered.status_code == 201, ordered.text
    number_id = ordered.json()["id"]

    readonly_token = await _add_member_with_role(
        client, session, org_id, "p18-rbac-readonly@example.com", ["numbers:read"]
    )
    readonly_headers = auth_headers(readonly_token, org["id"])

    # numbers:read is enough to list.
    listed = await client.get("/api/v1/numbers", headers=readonly_headers)
    assert listed.status_code == 200

    # numbers:read is NOT enough for search, order, or release - all three require
    # numbers:manage (app/api/routes/numbers.py).
    searched = await client.get(
        "/api/v1/numbers/available", params={"carrier": "fakecarrier"}, headers=readonly_headers
    )
    assert searched.status_code == 403

    order_attempt = await client.post(
        "/api/v1/numbers/order",
        json={"e164": "+12145550402", "carrier": "fakecarrier"},
        headers=readonly_headers,
    )
    assert order_attempt.status_code == 403

    release_attempt = await client.delete(
        f"/api/v1/numbers/{number_id}", headers=readonly_headers
    )
    assert release_attempt.status_code == 403

    # A member with no numbers permission at all cannot even list.
    none_token = await _add_member_with_role(
        client, session, org_id, "p18-rbac-none@example.com", []
    )
    none_headers = auth_headers(none_token, org["id"])
    listed_none = await client.get("/api/v1/numbers", headers=none_headers)
    assert listed_none.status_code == 403


async def test_release_via_bandwidth_and_signalwire_mixins_called_by_route(
    app_with_number_carrier,
):
    """Not the mixin unit tests above (those hit the mixin directly) - this proves the
    numbers route's release endpoint actually reaches release_number() on a carrier
    that only implements NumberProvider (no send_message), for both new providers."""
    client, fake, application = app_with_number_carrier
    token = await register_and_login(client, "p18-release@example.com")
    org = await create_org(client, token, "Org P18 Release")
    headers = auth_headers(token, org["id"])

    for extra_name in ("bandwidth", "signalwire"):
        extra = _FakeNumberCarrier(name=extra_name)
        extra.order_result = OrderResult(
            e164=f"+1214555050{0 if extra_name == 'bandwidth' else 1}",
            provider_ref=f"{extra_name}-ref",
            status="active",
        )
        application.state.carriers = CarrierRegistry(
            {fake.name: fake, extra_name: extra}, primary=fake.name
        )

        ordered = await client.post(
            "/api/v1/numbers/order",
            json={"e164": extra.order_result.e164, "carrier": extra_name},
            headers=headers,
        )
        assert ordered.status_code == 201, ordered.text
        number_id = ordered.json()["id"]

        released = await client.delete(f"/api/v1/numbers/{number_id}", headers=headers)
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "released"
        assert extra.released == [(extra.order_result.e164, f"{extra_name}-ref")]


# ==================================================================================
# P18: sweeper wiring - run_once() actually calls poll_pending_number_orders and
# reports it under "number_orders_polled"
# ==================================================================================
async def test_sweeper_run_once_polls_number_orders_and_counts_them(session):
    org_id = await _make_org(session)
    session.info["org_id"] = org_id

    fake = _FakeOrderCarrier(
        {
            "bw-sw-1": _StatusResult(status="active"),
            "bw-sw-2": _StatusResult(status="failed", detail="No inventory"),
        }
    )
    registry = CarrierRegistry({"bandwidth": fake}, primary="bandwidth")

    await _make_number(
        session, org_id=org_id, carrier="bandwidth", e164="+12145550501", provider_ref="bw-sw-1"
    )
    await _make_number(
        session, org_id=org_id, carrier="bandwidth", e164="+12145550502", provider_ref="bw-sw-2"
    )
    await session.commit()
    session.info.pop("org_id", None)

    fake_app = SimpleNamespace(state=SimpleNamespace(carriers=registry, settings=make_settings()))
    results = await sweeper_svc.run_once(fake_app)

    assert results["number_orders_polled"] == 2
    assert sorted(fake.calls) == ["bw-sw-1", "bw-sw-2"]


# ==================================================================================
# P18: bandwidth_site_id actually reaches the constructed carrier - env path
# (app/providers/registry.py::build_registry) and DB path
# (app/providers/registry_org.py::_construct_provider / build_registry_for_org) - and
# order_number() fails closed with a clear error when it does not.
# ==================================================================================
def test_build_registry_env_bandwidth_carries_site_id():
    s = make_settings(
        bandwidth_enabled=True,
        bandwidth_account_id="acct-env",
        bandwidth_api_username="user-env",
        bandwidth_api_password="pass-env",
        bandwidth_messaging_application_id="msg-app-env",
        bandwidth_site_id="site-env-1",
    )
    registry = build_registry(s)
    carrier = registry.get("bandwidth")
    assert carrier is not None
    assert carrier.site_id == "site-env-1"


def test_build_registry_env_bandwidth_site_id_missing_defaults_empty():
    s = make_settings(
        bandwidth_enabled=True,
        bandwidth_account_id="acct-env2",
        bandwidth_api_username="user-env2",
        bandwidth_api_password="pass-env2",
        bandwidth_messaging_application_id="msg-app-env2",
        # bandwidth_site_id left at its Settings default ("").
    )
    registry = build_registry(s)
    carrier = registry.get("bandwidth")
    assert carrier is not None
    assert carrier.site_id == ""


async def test_build_registry_for_org_db_bandwidth_carries_site_id_credential(session):
    key = Fernet.generate_key().decode()
    app_settings = make_settings(credentials_master_key=key)

    org_id = await _make_org(session)
    set_org_context(session, org_id)

    creds = {
        "account_id": "acct-db",
        "api_username": "user-db",
        "api_password": "pass-db",
        "messaging_application_id": "msg-app-db",
        "webhook_username": "hook-user",
        "webhook_password": "hook-pass",
        "site_id": "site-db-1",
    }
    row = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider="bandwidth",
        label="DB Bandwidth",
        credentials_encrypted=credentials_svc.encrypt(app_settings, creds),
        status="active",
        created_by=None,
    )
    session.add(row)
    await session.commit()

    registry, db_owned = build_registry_for_org(app_settings, [row])
    carrier = registry.get("bandwidth")
    assert carrier is not None
    assert carrier.site_id == "site-db-1"
    assert db_owned.get("bandwidth") is carrier


async def test_build_registry_for_org_db_bandwidth_site_id_missing_defaults_empty(session):
    key = Fernet.generate_key().decode()
    app_settings = make_settings(credentials_master_key=key)

    org_id = await _make_org(session)
    set_org_context(session, org_id)

    creds = {
        "account_id": "acct-db2",
        "api_username": "user-db2",
        "api_password": "pass-db2",
        "messaging_application_id": "msg-app-db2",
        "webhook_username": "hook-user2",
        "webhook_password": "hook-pass2",
        # site_id omitted entirely - a credential set created before P18, or one that
        # never filled in the optional field.
    }
    row = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider="bandwidth",
        label="DB Bandwidth No Site",
        credentials_encrypted=credentials_svc.encrypt(app_settings, creds),
        status="active",
        created_by=None,
    )
    session.add(row)
    await session.commit()

    registry, _db_owned = build_registry_for_org(app_settings, [row])
    carrier = registry.get("bandwidth")
    assert carrier is not None
    assert carrier.site_id == ""


async def test_bandwidth_order_number_without_site_id_raises_validation_error():
    """A REAL BandwidthMessagingCarrier (env-built, no bandwidth_site_id configured)
    must fail closed with the mixin's clear error - never attempt the order."""
    s = make_settings(
        bandwidth_enabled=True,
        bandwidth_account_id="acct-env3",
        bandwidth_api_username="user-env3",
        bandwidth_api_password="pass-env3",
        bandwidth_messaging_application_id="msg-app-env3",
    )
    registry = build_registry(s)
    carrier = registry.get("bandwidth")
    assert carrier.site_id == ""

    with pytest.raises(ValidationFailedError, match="bandwidth_site_id"):
        await carrier.order_number("+12145550700")


# ==================================================================================
# P18 review fixes: N+1 batching, SQL-side pollable-carrier filter, commit-per-row
# mutation isolation, two-org sweeper scoping, db_backed_providers TTL expiry
# ==================================================================================
async def test_list_numbers_batches_provider_account_label_query(
    app_with_number_carrier, session, query_counter
):
    """P18 review item 13: _out() must batch-load ProviderAccount labels once per list
    call, never one session.get() per row."""
    client, _fake, _application = app_with_number_carrier
    token = await register_and_login(client, "p18-n1@example.com")
    org = await create_org(client, token, "Org P18 N1")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    account = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider="fakecarrier",
        label="Batched Label",
        credentials_encrypted="unused-in-this-test",
        status="active",
        created_by=None,
    )
    session.add(account)
    await session.flush()
    for i in range(10):
        session.add(
            OrgNumber(
                id=uuid.uuid4(),
                org_id=org_id,
                e164=f"+1214555{3000 + i}",
                carrier="fakecarrier",
                status="active",
                provider_account_id=account.id,
                capabilities={},
            )
        )
    await session.commit()
    set_org_context(session, None)

    query_counter.reset()
    resp = await client.get("/api/v1/numbers", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 10
    assert all(row["provider_account_label"] == "Batched Label" for row in resp.json())

    provider_account_queries = [
        s for s in query_counter.statements if "provider_accounts" in s
    ]
    assert len(provider_account_queries) == 1, (
        "expected exactly one batched provider_accounts query for the whole list, got "
        f"{len(provider_account_queries)}: {provider_account_queries}"
    )


async def test_poll_pending_number_orders_sql_filters_unpollable_carriers_first(session):
    """P18 review item 3: the SQL query itself must restrict rows to carriers with
    order_status - never fetch N unpollable rows and skip them in Python, which would
    starve a pollable row sitting after them out of the per-pass `limit`.

    30 "plivo" (unpollable) rows sort BEFORE the single "bandwidth" (pollable) row by
    e164 - the column the query orders by. Pre-fix, `.limit(limit - polled)` at 25 would
    fetch only unpollable rows and never even read the bandwidth one."""
    org_id = await _make_org(session)
    session.info["org_id"] = org_id

    for i in range(30):
        await _make_number(
            session,
            org_id=org_id,
            carrier="plivo",
            e164=f"+1214555{i:04d}",
            provider_ref=f"plivo-{i}",
        )
    await _make_number(
        session, org_id=org_id, carrier="bandwidth", e164="+19995550001", provider_ref="bw-late"
    )
    await session.commit()
    session.info.pop("org_id", None)

    fake_bw = _FakeOrderCarrier({"bw-late": _StatusResult(status="active")})
    fake_plivo = _NoOrderStatusCarrier()
    registry = CarrierRegistry({"bandwidth": fake_bw, "plivo": fake_plivo}, primary="bandwidth")

    polled = await poll_pending_number_orders(session, registry, limit=25)

    assert polled == 1
    assert fake_bw.calls == ["bw-late"]

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OrgNumber)
            .where(OrgNumber.provider_ref == "bw-late")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    assert rows[0].status == "active"


async def test_poll_pending_number_orders_commit_per_row_survives_a_mid_pass_failure(
    session,
):
    """P18 review item (a): a carrier that raises on the SECOND row must not undo the
    FIRST row's already-committed transition - commit-per-row means row 1's write is
    durable regardless of what happens to row 2."""
    org_id = await _make_org(session)
    session.info["org_id"] = org_id

    await _make_number(
        session, org_id=org_id, carrier="bandwidth", e164="+12145550601", provider_ref="ok-1"
    )
    await _make_number(
        session, org_id=org_id, carrier="bandwidth", e164="+12145550602", provider_ref="boom-1"
    )
    await session.commit()
    session.info.pop("org_id", None)

    class _FlakyOrderCarrier:
        name = "bandwidth"

        def __init__(self):
            self.calls: list[str] = []

        async def order_status(self, provider_ref: str):
            self.calls.append(provider_ref)
            if provider_ref == "boom-1":
                raise RuntimeError("carrier had a bad day")
            return _StatusResult(status="active")

    flaky = _FlakyOrderCarrier()
    registry = CarrierRegistry({"bandwidth": flaky}, primary="bandwidth")

    polled = await poll_pending_number_orders(session, registry, limit=25)

    # Both rows were attempted (polled counts an attempt, not just a success)...
    assert polled == 2
    assert flaky.calls == ["ok-1", "boom-1"]

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OrgNumber).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    by_ref = {r.provider_ref: r for r in rows}
    # ...but row 1's transition survived row 2's exception - commit-per-row, not one
    # transaction for the whole org.
    assert by_ref["ok-1"].status == "active"
    assert by_ref["boom-1"].status == "pending"


async def test_poll_pending_number_orders_scopes_correctly_across_two_orgs(session):
    """P18 review item (b): org A's rows must be untouched while org B is processed,
    and BOTH orgs must get polled in the same pass - proof the CURRENT_ORG_ID/
    set_org_context wiring is bound and reset per org, not leaked or dropped."""
    org_a = await _make_org(session)
    org_b = await _make_org(session)

    session.info["org_id"] = org_a
    await _make_number(
        session, org_id=org_a, carrier="bandwidth", e164="+12145550611", provider_ref="a-1"
    )
    await session.commit()

    session.info["org_id"] = org_b
    await _make_number(
        session, org_id=org_b, carrier="bandwidth", e164="+12145550612", provider_ref="b-1"
    )
    await session.commit()
    session.info.pop("org_id", None)

    fake = _FakeOrderCarrier(
        {"a-1": _StatusResult(status="active"), "b-1": _StatusResult(status="failed", detail="no numbers")}
    )
    registry = CarrierRegistry({"bandwidth": fake}, primary="bandwidth")

    polled = await poll_pending_number_orders(session, registry, limit=25)

    assert polled == 2
    assert sorted(fake.calls) == ["a-1", "b-1"]

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OrgNumber).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    by_ref = {r.provider_ref: r for r in rows}
    assert by_ref["a-1"].status == "active"
    assert by_ref["a-1"].org_id == org_a
    assert by_ref["b-1"].status == "failed"
    assert by_ref["b-1"].org_id == org_b

    # Context must not have leaked past the call - a subsequent unscoped query needs
    # allow_unscoped explicitly, exactly like every other test in this file.
    from app.errors import MissingTenantContextError

    with pytest.raises(MissingTenantContextError):
        await session.execute(sa.select(OrgNumber))


async def test_poll_pending_number_orders_primes_and_scopes_db_backed_carriers(
    session, monkeypatch
):
    """P18 review item 1: the sweeper has no HTTP request to re-prime a DB-backed org's
    carrier registry cache (app/auth/deps.py::get_current_org normally does this on
    every authenticated hit). poll_pending_number_orders must prime it itself - through
    a REAL CarrierRegistryProxy wrapping a global registry with NO env bandwidth
    carrier at all, so a fallback to the global registry resolves NOTHING for
    "bandwidth" and is unambiguously distinguishable from resolving each org's own
    DB-backed adapter. Two orgs, two DIFFERENT DB-backed Bandwidth accounts (different
    account_id) - each org's pending row must be polled through ITS OWN constructed
    adapter. Removing the priming call, or the CURRENT_ORG_ID.set(...) it depends on,
    makes every assertion here fail (polled == 0, or a call list missing an org)."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier
    from app.providers.registry_org import CarrierRegistryProxy

    key = Fernet.generate_key().decode()
    app_settings = make_settings(credentials_master_key=key)

    org_a = await _make_org(session)
    org_b = await _make_org(session)

    async def _make_bandwidth_account(org_id: uuid.UUID, suffix: str) -> None:
        set_org_context(session, org_id)
        creds = {
            "account_id": f"acct-{suffix}",
            "api_username": f"user-{suffix}",
            "api_password": f"pass-{suffix}",
            "messaging_application_id": f"msg-{suffix}",
            "webhook_username": f"hook-user-{suffix}",
            "webhook_password": f"hook-pass-{suffix}",
            "site_id": f"site-{suffix}",
        }
        session.add(
            ProviderAccount(
                id=uuid.uuid4(),
                org_id=org_id,
                provider="bandwidth",
                label=f"Bandwidth {suffix}",
                credentials_encrypted=credentials_svc.encrypt(app_settings, creds),
                status="active",
                created_by=None,
            )
        )
        await session.commit()

    await _make_bandwidth_account(org_a, "a")
    await _make_bandwidth_account(org_b, "b")

    set_org_context(session, org_a)
    await _make_number(
        session, org_id=org_a, carrier="bandwidth", e164="+12145550621", provider_ref="order-a-1"
    )
    await session.commit()
    set_org_context(session, org_b)
    await _make_number(
        session, org_id=org_b, carrier="bandwidth", e164="+12145550622", provider_ref="order-b-1"
    )
    await session.commit()
    set_org_context(session, None)

    calls: list[tuple[str, str]] = []

    async def fake_order_status(self, provider_ref: str):
        # self.account_id is what distinguishes org A's constructed adapter from org
        # B's - proof each org's row was polled through ITS OWN DB-backed carrier.
        calls.append((self.account_id, provider_ref))
        return _StatusResult(status="active")

    monkeypatch.setattr(BandwidthMessagingCarrier, "order_status", fake_order_status)

    # No env bandwidth carrier configured at all.
    global_registry = build_registry(make_settings())
    assert global_registry.get("bandwidth") is None
    proxy = CarrierRegistryProxy(global_registry)

    polled = await poll_pending_number_orders(session, proxy, limit=25, settings=app_settings)

    assert polled == 2
    assert sorted(calls) == [("acct-a", "order-a-1"), ("acct-b", "order-b-1")]

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OrgNumber).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    by_ref = {r.provider_ref: r for r in rows}
    assert by_ref["order-a-1"].status == "active"
    assert by_ref["order-a-1"].org_id == org_a
    assert by_ref["order-b-1"].status == "active"
    assert by_ref["order-b-1"].org_id == org_b


async def test_db_backed_providers_ttl_expiry_returns_empty(monkeypatch):
    """P18 review item (g): db_backed_providers must mirror is_primed's own freshness
    rule - once the cache entry is older than the TTL backstop, it is treated exactly
    like "never primed" (empty set), not stale-but-trusted."""
    from app.providers import registry_org as registry_org_module

    org_id = uuid.uuid4()
    fake_carrier = object()
    registry = CarrierRegistry({"telnyx": fake_carrier}, primary="telnyx")

    await registry_org_module._cache_org_registry(org_id, 0, registry, {"telnyx": fake_carrier})

    # Freshly primed: the provider shows up.
    assert registry_org_module.db_backed_providers(org_id) == frozenset({"telnyx"})

    # Simulate the TTL backstop having lapsed by backdating the cache entry's timestamp.
    cached_registry, db_owned, _cached_at = registry_org_module._ORG_REGISTRY_CACHE[(org_id, 0)]
    registry_org_module._ORG_REGISTRY_CACHE[(org_id, 0)] = (
        cached_registry,
        db_owned,
        0.0,  # time.monotonic() started long before this, so this always reads as expired
    )

    assert registry_org_module.db_backed_providers(org_id) == frozenset()

    # Cleanup: this module-level cache persists across tests in the same process.
    registry_org_module._ORG_REGISTRY_CACHE.pop((org_id, 0), None)
