"""Cross-tenant regression tests for list/summary surfaces.

DEFECT 1 was a severe tenant leak: ``sa.select(sa.func.count()).select_from(Model)``
puts no mapped column in the statement, so the session-level tenant guard attached no
org filter and aggregate counts spanned every org in the database. This file exists to
prove every surface that was vulnerable now stays org-scoped.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.main import create_app
from app.models import TRAFFIC_SCOPE, OrgNumber, ProviderSpendDaily
from tests.conftest import (
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)


@pytest.fixture
async def client_with_key(engine):
    key = Fernet.generate_key().decode()
    application = create_app(make_settings(credentials_master_key=key))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TELNYX_CREDENTIALS = {
    "api_key": "secret",
    "public_key": "secret",
    "messaging_profile_id": "profile",
    "voice_connection_id": "conn",
}


async def test_capabilities_member_count_is_org_scoped(client):
    token_a = await register_and_login(client, "p20tenancy-cap-a@example.com")
    org_a = await create_org(client, token_a, "Tenancy Cap A")

    token_b = await register_and_login(client, "p20tenancy-cap-b@example.com")
    org_b = await create_org(client, token_b, "Tenancy Cap B")

    r_a = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token_a, org_a["id"])
    )
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["org"]["member_count"] == 1

    r_b = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token_b, org_b["id"])
    )
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["org"]["member_count"] == 1


async def test_provider_account_spend_is_org_scoped(client_with_key, session):
    token_a = await register_and_login(client_with_key, "p20tenancy-spend-a@example.com")
    org_a = await create_org(client_with_key, token_a, "Tenancy Spend A")

    r_a = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "A",
            "credentials": TELNYX_CREDENTIALS,
        },
        headers=auth_headers(token_a, org_a["id"]),
    )
    assert r_a.status_code == 201, r_a.text

    token_b = await register_and_login(client_with_key, "p20tenancy-spend-b@example.com")
    org_b = await create_org(client_with_key, token_b, "Tenancy Spend B")
    org_b_id = uuid.UUID(org_b["id"])

    r_b = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "B",
            "credentials": TELNYX_CREDENTIALS,
        },
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert r_b.status_code == 201, r_b.text

    set_org_context(session, org_b_id)
    session.add(
        ProviderSpendDaily(
            id=uuid.uuid4(),
            org_id=org_b_id,
            period_date=datetime.now(timezone.utc).date(),
            provider="telnyx",
            metric="sms_out",
            quantity=1,
            cost_micros=5_000_000,
            number_id=None,
            scope_key=TRAFFIC_SCOPE,
        )
    )
    await session.commit()

    r_a = await client_with_key.get(
        "/api/v1/provider-accounts", headers=auth_headers(token_a, org_a["id"])
    )
    assert r_a.status_code == 200, r_a.text
    body_a = r_a.json()
    assert len(body_a) == 1
    assert body_a[0]["spend_mtd_micros"] == 0, (
        "org A must not read org B's provider spend"
    )

    r_b = await client_with_key.get(
        "/api/v1/provider-accounts", headers=auth_headers(token_b, org_b["id"])
    )
    assert r_b.status_code == 200, r_b.text
    body_b = r_b.json()
    assert len(body_b) == 1
    assert body_b[0]["spend_mtd_micros"] == 5_000_000


async def test_provider_account_numbers_count_is_org_scoped(client_with_key, session):
    token_a = await register_and_login(client_with_key, "p20tenancy-numcount-a@example.com")
    org_a = await create_org(client_with_key, token_a, "Tenancy Num Count A")

    r_a = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "A",
            "credentials": TELNYX_CREDENTIALS,
        },
        headers=auth_headers(token_a, org_a["id"]),
    )
    assert r_a.status_code == 201, r_a.text

    token_b = await register_and_login(client_with_key, "p20tenancy-numcount-b@example.com")
    org_b = await create_org(client_with_key, token_b, "Tenancy Num Count B")
    org_b_id = uuid.UUID(org_b["id"])

    r_b = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "B",
            "credentials": TELNYX_CREDENTIALS,
        },
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert r_b.status_code == 201, r_b.text
    account_b_id = uuid.UUID(r_b.json()["id"])

    set_org_context(session, org_b_id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org_b_id,
            e164="+12025559902",
            carrier="telnyx",
            provider_account_id=account_b_id,
            status="active",
        )
    )
    await session.commit()

    r_a = await client_with_key.get(
        "/api/v1/provider-accounts", headers=auth_headers(token_a, org_a["id"])
    )
    assert r_a.status_code == 200, r_a.text
    body_a = r_a.json()
    assert len(body_a) == 1
    assert body_a[0]["numbers_count"] == 0

    r_b = await client_with_key.get(
        "/api/v1/provider-accounts", headers=auth_headers(token_b, org_b["id"])
    )
    assert r_b.status_code == 200, r_b.text
    body_b = r_b.json()
    assert len(body_b) == 1
    assert body_b[0]["numbers_count"] == 1


async def test_numbers_list_is_org_scoped(client):
    token_a, org_a, number_a = await make_org_with_number(
        client, "p20tenancy-numbers-a@example.com", "Tenancy Numbers A", "+12025559901"
    )
    token_b, org_b, number_b = await make_org_with_number(
        client, "p20tenancy-numbers-b@example.com", "Tenancy Numbers B", "+12025559911"
    )

    r = await client.get(
        "/api/v1/numbers", headers=auth_headers(token_a, org_a["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["e164"] == number_a["e164"]
    assert body[0]["inbox_name"] == number_a["e164"]
    assert body[0]["e164"] != number_b["e164"]
