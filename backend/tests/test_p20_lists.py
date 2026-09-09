from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    TRAFFIC_SCOPE,
    Inbox,
    OrgMembership,
    OrgNumber,
    ProviderSpendDaily,
    Role,
)
from app.repositories import users as users_repo
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


async def _add_agent(client, session, org_id: uuid.UUID, email: str) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def test_numbers_list_includes_inbox_name(client, session):
    token, org, number = await make_org_with_number(
        client, "inbox-name@example.com", "Inbox Name", "+12025550160"
    )
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    r = await client.get("/api/v1/numbers", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    assert r.json()[0]["inbox_name"] == number["e164"]

    set_org_context(session, org_id)
    inbox = (await session.execute(sa.select(Inbox))).scalar_one()
    inbox.name = "Sales line"
    await session.commit()

    r = await client.get("/api/v1/numbers", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()[0]["inbox_name"] == "Sales line"


async def test_numbers_list_inbox_name_null_when_no_inbox(client, session):
    token, org, _number = await make_org_with_number(
        client, "inbox-null@example.com", "Inbox Null", "+12025550161"
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    inbox = (await session.execute(sa.select(Inbox))).scalar_one()
    await session.delete(inbox)
    await session.commit()

    r = await client.get(
        "/api/v1/numbers", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["inbox_name"] is None


async def test_numbers_list_inbox_name_is_not_n_plus_one(client, query_counter):
    token = await register_and_login(client, "inbox-nplus@example.com")
    org = await create_org(client, token, "Inbox N Plus")
    headers = auth_headers(token, org["id"])

    for e164 in ["+12025550162", "+12025550163", "+12025550164"]:
        r = await client.post("/api/v1/numbers", json={"e164": e164}, headers=headers)
        assert r.status_code == 201, r.text

    query_counter.reset()
    r = await client.get("/api/v1/numbers", headers=headers)
    assert r.status_code == 200, r.text

    inbox_from_statements = [
        statement for statement in query_counter.statements if "FROM inboxes" in statement
    ]
    assert len(inbox_from_statements) == 1


async def test_provider_account_list_reports_numbers_and_spend(client_with_key, session):
    token = await register_and_login(client_with_key, "pstats@example.com")
    org = await create_org(client_with_key, token, "Provider Stats")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    r = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "Primary",
            "credentials": {
                "api_key": "secret",
                "public_key": "secret",
                "messaging_profile_id": "profile",
                "voice_connection_id": "conn",
            },
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    account_id = uuid.UUID(r.json()["id"])

    set_org_context(session, org_id)
    number_rows = [
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org_id,
            e164="+12025550165",
            carrier="telnyx",
            provider_account_id=account_id,
            status="active",
        ),
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org_id,
            e164="+12025550166",
            carrier="telnyx",
            provider_account_id=account_id,
            status="released",
            is_active=False,
        ),
    ]
    for row in number_rows:
        session.add(row)
        await session.commit()

    today = datetime.now(timezone.utc).date()
    first = date(today.year, today.month, 1)
    last_prev = first - timedelta(days=1)

    # When today is the first of the month, "today" and "the 1st" are the same date, so
    # combine their cost into one row to respect the spend table's unique constraint.
    if today == first:
        in_month_entries = [(today, 200)]
    else:
        in_month_entries = [(today, 123), (first, 77)]

    spend_entries = in_month_entries + [(last_prev, 999)]
    for period_date, cost_micros in spend_entries:
        session.add(
            ProviderSpendDaily(
                id=uuid.uuid4(),
                org_id=org_id,
                period_date=period_date,
                provider="telnyx",
                metric="sms_out",
                quantity=10,
                cost_micros=cost_micros,
                number_id=None,
                scope_key=TRAFFIC_SCOPE,
            )
        )
        await session.commit()

    r = await client_with_key.get("/api/v1/provider-accounts", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["numbers_count"] == 1
    assert body[0]["spend_mtd_micros"] == 200


async def test_provider_account_create_reports_zero_stats(client_with_key):
    token = await register_and_login(client_with_key, "pzero@example.com")
    org = await create_org(client_with_key, token, "Provider Zero")

    r = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "Primary",
            "credentials": {
                "api_key": "secret",
                "public_key": "secret",
                "messaging_profile_id": "profile",
                "voice_connection_id": "conn",
            },
        },
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["numbers_count"] == 0
    assert r.json()["spend_mtd_micros"] == 0


async def test_provider_account_list_requires_settings_read(client, session):
    owner_token = await register_and_login(client, "psettings-owner@example.com")
    org = await create_org(client, owner_token, "Provider Settings")
    agent_token = await _add_agent(
        client, session, uuid.UUID(org["id"]), "psettings-agent@example.com"
    )

    r = await client.get(
        "/api/v1/provider-accounts",
        headers=auth_headers(agent_token, org["id"]),
    )
    assert r.status_code == 403, r.text
