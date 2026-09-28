"""Customers (owners included) never see which carrier carries their traffic or what it
costs us. The DB columns are still written; only the customer API changed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.main import create_app
from app.models import Call
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult
from app.providers.registry import CarrierRegistry
from app.services.telephony_billing import PLATFORM_PRICE_MICROS
from tests.conftest import (
    auth_headers,
    create_org,
    make_org_with_number,
    make_platform_operator,
    make_settings,
    register_and_login,
)

HIDDEN_KEYS = (
    "carrier",
    "provider_account_id",
    "provider_account_label",
    "purchase_cost_cents",
    "monthly_cost_cents",
    "order_detail",
)
OUR = "+12145550100"
CONTACT = "+19725559999"


async def test_numbers_list_hides_carrier_facts_and_carries_the_two_flags(app_with_carrier):
    client, _fake, _app = app_with_carrier
    token, org, _number = await make_org_with_number(
        client, "hide-list@example.com", "Org Hide List", OUR
    )
    r = await client.get("/api/v1/numbers", headers=auth_headers(token, org["id"]))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows
    for row in rows:
        for key in HIDDEN_KEYS:
            assert key not in row, key
        assert isinstance(row["carrier_campaign_locked"], bool)
        assert isinstance(row["e911_supported"], bool)


@dataclass
class _PricedCarrier:
    """Reports a carrier cost that must never reach the customer."""

    name: str = "bandwidth"
    carrier_cost_cents: int = 123
    search_result: list = field(default_factory=list)

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]:
        return [
            AvailableNumber(
                e164="+12145550777",
                monthly_cost="$1.23",
                setup_cost="$0.99",
                monthly_cost_cents=self.carrier_cost_cents,
                setup_cost_cents=99,
            )
        ]

    async def order_number(self, e164: str) -> OrderResult:  # pragma: no cover
        raise NotImplementedError

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        return None

    async def lookup_owned_number(self, e164: str) -> bool | None:  # pragma: no cover
        return True


async def test_available_numbers_show_our_retail_price_not_the_carrier_cost(engine):
    application = create_app(make_settings())
    carrier = _PricedCarrier()
    application.state.carriers = CarrierRegistry({carrier.name: carrier}, primary=carrier.name)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "hide-avail@example.com")
        org = await create_org(client, token, "Org Hide Avail")
        r = await client.get(
            "/api/v1/numbers/available",
            params={"area_code": "214"},
            headers=auth_headers(token, org["id"]),
        )
    assert r.status_code == 200, r.text
    (row,) = r.json()
    retail_cents = PLATFORM_PRICE_MICROS["number_mrc"] // 10_000
    assert retail_cents != carrier.carrier_cost_cents
    assert row["monthly_cost_cents"] == retail_cents
    assert row["monthly_cost"] == f"${retail_cents / 100:.2f}"
    # number_setup is free at retail, whatever the carrier charges us.
    assert row["setup_cost_cents"] == PLATFORM_PRICE_MICROS["number_setup"] // 10_000 == 0
    assert row["setup_cost"] == ""
    assert "$1.23" not in r.text and "$0.99" not in r.text


@pytest.fixture
async def client_with_key(engine):
    application = create_app(make_settings(credentials_master_key=Fernet.generate_key().decode()))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/provider-accounts",
        "/api/v1/spend/summary",
        "/api/v1/spend/daily",
        "/api/v1/provider-rates",
    ],
)
async def test_operator_only_routes_are_403_for_an_owner_and_200_for_an_operator(
    client_with_key, path
):
    client = client_with_key
    email = f"hide-op-{uuid.uuid4().hex[:8]}@example.com"
    token = await register_and_login(client, email)
    org = await create_org(client, token, "Org Hide Op")
    headers = auth_headers(token, org["id"])

    denied = await client.get(path, headers=headers)
    assert denied.status_code == 403, denied.text

    await make_platform_operator(email)
    allowed = await client.get(path, headers=headers)
    assert allowed.status_code == 200, allowed.text


async def test_owner_cannot_mutate_provider_accounts_rates_or_rollup_without_operator(
    client_with_key,
):
    client = client_with_key
    token = await register_and_login(client, "hide-mut@example.com")
    org = await create_org(client, token, "Org Hide Mut")
    headers = auth_headers(token, org["id"])

    created = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "x", "credentials": {"api_key": "k"}},
        headers=headers,
    )
    assert created.status_code == 403, created.text
    put = await client.put("/api/v1/provider-rates", json={"rates": []}, headers=headers)
    assert put.status_code == 403, put.text
    rollup = await client.post(
        "/api/v1/spend/rollup", params={"day": "2026-06-01"}, headers=headers
    )
    assert rollup.status_code == 403, rollup.text


async def test_call_api_hides_carrier_and_route_reason_but_the_row_keeps_them(app_with_carrier):
    client, _fake, _app = app_with_carrier
    token, org, _number = await make_org_with_number(
        client, "hide-call@example.com", "Org Hide Call", OUR
    )
    org_id = uuid.UUID(org["id"])
    call_id = uuid.uuid4()
    reason = "Called via Bandwidth - cheapest healthy route"
    async with get_sessionmaker()() as s:
        set_org_context(s, org_id)
        s.add(
            Call(
                id=call_id,
                org_id=org_id,
                direction="outbound",
                contact_e164=CONTACT,
                our_e164=OUR,
                carrier="bandwidth",
                route_reason=reason,
                status="completed",
            )
        )
        await s.commit()
    h = auth_headers(token, org["id"])

    one = await client.get(f"/api/v1/calls/{call_id}", headers=h)
    assert one.status_code == 200, one.text
    assert "carrier" not in one.json()
    assert one.json()["route_reason"] is None

    listed = await client.get("/api/v1/calls", headers=h)
    assert listed.status_code == 200, listed.text
    (row,) = [c for c in listed.json() if c["id"] == str(call_id)]
    assert "carrier" not in row
    assert row["route_reason"] is None
    assert "Bandwidth" not in one.text and "Bandwidth" not in listed.text

    async with get_sessionmaker()() as s:
        set_org_context(s, org_id)
        stored = await s.get(Call, call_id)
        assert stored.carrier == "bandwidth"
        assert stored.route_reason == reason


async def test_routing_console_is_403_for_an_owner_and_200_for_an_operator(client_with_key):
    client = client_with_key
    email = "hide-routing@example.com"
    token = await register_and_login(client, email)
    org = await create_org(client, token, "Org Hide Routing")
    headers = auth_headers(token, org["id"])

    for path in (
        "/api/v1/routing/catalog",
        "/api/v1/routing/carriers",
        "/api/v1/routing/policy",
    ):
        denied = await client.get(path, headers=headers)
        assert denied.status_code == 403, (path, denied.text)
    patched = await client.patch(
        "/api/v1/routing/policy", json={"allow_cross_carrier_failover": True}, headers=headers
    )
    assert patched.status_code == 403, patched.text

    await make_platform_operator(email)
    allowed = await client.get("/api/v1/routing/catalog", headers=headers)
    assert allowed.status_code == 200, allowed.text
