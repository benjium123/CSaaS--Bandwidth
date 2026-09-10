from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    AiUsageEvent,
    AuditLogEntry,
    Call,
    CreditLedgerEntry,
    Org,
    PaymentMethod,
)
from app.services import ai_usage, credits, stripe_client
from tests.conftest import auth_headers, create_org, register_and_login


async def test_summary_reports_balance_reserved_and_warning(client, session):
    token = await register_and_login(client, "billing-summary@example.com")
    org = await create_org(client, token, "Summary Org")
    org_id = uuid.UUID(org["id"])

    await credits.topup(session, org_id, 10_000_000, reference="pi-summary")
    await session.commit()
    await credits.charge_usage(session, org_id, 8_500_000, reference="spend-summary")
    await session.commit()
    await credits.reserve(session, org_id, 1_000_000, reference="call-summary")
    await session.commit()

    r = await client.get(
        "/api/v1/billing/summary", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["balance_micros"] == 500_000
    assert body["reserved_micros"] == 1_000_000
    assert body["available_micros"] == 0
    assert body["warning"] == "critical"


async def test_summary_never_exposes_cost(client):
    token = await register_and_login(client, "billing-no-cost@example.com")
    org = await create_org(client, token, "No Cost Org")

    for path in (
        "/api/v1/billing/summary",
        "/api/v1/billing/usage",
        "/api/v1/billing/rates",
    ):
        r = await client.get(path, headers=auth_headers(token, org["id"]))
        assert r.status_code == 200, r.text
        dumped = json.dumps(r.json()).lower()
        for forbidden in ("cost", "markup", "margin"):
            assert forbidden not in dumped


async def test_ledger_pages_with_a_cursor_and_stays_in_one_workspace(client, session):
    token_a = await register_and_login(client, "ledger-a@example.com")
    org_a = await create_org(client, token_a, "Ledger A")
    org_a_id = uuid.UUID(org_a["id"])

    token_b = await register_and_login(client, "ledger-b@example.com")
    org_b = await create_org(client, token_b, "Ledger B")
    org_b_id = uuid.UUID(org_b["id"])

    for i in range(5):
        await credits.topup(
            session, org_a_id, 5_000_000, reference=f"pi-ledger-a-{i}"
        )
        await session.commit()

    for i in range(3):
        await credits.topup(
            session, org_b_id, 5_000_000, reference=f"pi-ledger-b-{i}"
        )
        await session.commit()

    params: dict = {"limit": 2}
    seen_ids: list[str] = []
    for _ in range(10):
        r = await client.get(
            "/api/v1/billing/ledger",
            params=params,
            headers=auth_headers(token_a, org_a["id"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for entry in body["entries"]:
            seen_ids.append(entry["id"])

        next_cursor = body.get("next_cursor")
        if not next_cursor:
            break
        params["cursor"] = next_cursor
    else:
        raise AssertionError("ledger pagination did not terminate")

    assert len(seen_ids) == 5
    assert len(set(seen_ids)) == 5

    set_org_context(session, org_a_id)
    org_a_rows = (
        await session.execute(sa.select(CreditLedgerEntry.id))
    ).scalars().all()
    org_a_ids = {str(row) for row in org_a_rows}
    assert set(seen_ids) == org_a_ids


async def test_usage_groups_by_metric_and_totals_the_price(client, session):
    token = await register_and_login(client, "usage-owner@example.com")
    org = await create_org(client, token, "Usage Org")
    org_id = uuid.UUID(org["id"])

    await ai_usage.record(
        session,
        org_id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=100,
        source="worker",
        idempotency_key="usage-a",
    )
    await session.commit()

    await ai_usage.record(
        session,
        org_id,
        provider="deepgram",
        kind="stt",
        metric="stt_seconds",
        quantity=60,
        source="worker",
        idempotency_key="usage-b",
    )
    await session.commit()

    r = await client.get("/api/v1/billing/usage", headers=auth_headers(token, org["id"]))
    assert r.status_code == 200, r.text
    body = r.json()

    metrics = {row["metric"] for row in body["by_metric"]}
    assert metrics == {"llm_tokens_in", "stt_seconds"}
    assert len(body["by_metric"]) == 2

    total_from_rows = sum(int(row["price_micros"]) for row in body["by_metric"])
    assert body["total_price_micros"] == total_from_rows

    set_org_context(session, org_id)
    expected_total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(AiUsageEvent.price_micros), 0))
        )
    ).scalar_one()
    assert body["total_price_micros"] == int(expected_total)


async def test_usage_call_drilldown_404s_for_another_workspaces_call(client, session):
    token_a = await register_and_login(client, "usage-a-404@example.com")
    org_a = await create_org(client, token_a, "Usage 404 A")

    token_b = await register_and_login(client, "usage-b-404@example.com")
    org_b = await create_org(client, token_b, "Usage 404 B")
    org_b_id = uuid.UUID(org_b["id"])

    call_id = uuid.uuid4()
    set_org_context(session, org_b_id)
    session.add(
        Call(
            id=call_id,
            org_id=org_b_id,
            direction="outbound",
            contact_e164="+19725550199",
            our_e164="+12145550100",
            carrier="test",
        )
    )
    await session.commit()

    r = await client.get(
        f"/api/v1/billing/usage/calls/{call_id}",
        headers=auth_headers(token_a, org_a["id"]),
    )
    assert r.status_code == 404, r.text


async def test_rate_sheet_shows_customer_prices_only(client):
    token = await register_and_login(client, "rates-customer@example.com")
    org = await create_org(client, token, "Rates Customer Org")

    r = await client.get("/api/v1/billing/rates", headers=auth_headers(token, org["id"]))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) > 0

    for row in rows:
        assert set(row.keys()) == {"provider", "metric", "price_micros", "currency"}
        assert "cost_micros" not in row
        assert "unit_cost_micros" not in row


async def test_topup_creates_a_checkout_link(client, session, monkeypatch):
    token = await register_and_login(client, "topup-checkout@example.com")
    org = await create_org(client, token, "Topup Checkout Org")

    async def fake_create_checkout_session(*args, **kwargs):
        return {"id": "cs_test", "url": "https://checkout.test/x"}

    monkeypatch.setattr(stripe_client, "create_checkout_session", fake_create_checkout_session)

    r = await client.post(
        "/api/v1/billing/topups",
        json={"amount_micros": 25_000_000},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"checkout_url": "https://checkout.test/x"}

    set_org_context(session, uuid.UUID(org["id"]))
    audit_count = (
        await session.execute(
            sa.select(sa.func.count(AuditLogEntry.id)).where(
                AuditLogEntry.action == "billing.topup_started"
            )
        )
    ).scalar_one()
    assert audit_count == 1


async def test_topup_refuses_an_amount_outside_the_allowed_range(client):
    token = await register_and_login(client, "topup-range@example.com")
    org = await create_org(client, token, "Topup Range Org")

    r = await client.post(
        "/api/v1/billing/topups",
        json={"amount_micros": 1_000_000},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code in (400, 422), r.text
    assert "micros" not in r.text.lower()


async def test_turning_auto_recharge_off_keeps_the_assistant_fallback(client, session):
    token = await register_and_login(client, "auto-fallback@example.com")
    org = await create_org(client, token, "Auto Fallback Org")
    org_id = uuid.UUID(org["id"])

    pm_id = uuid.uuid4()
    set_org_context(session, org_id)
    session.add(
        PaymentMethod(
            id=pm_id,
            org_id=org_id,
            stripe_customer_id="cus_auto_fallback",
            stripe_payment_method_id="pm_auto_fallback",
            brand="Visa",
            last4="4242",
            is_default=True,
        )
    )
    await session.commit()

    set_org_context(session, org_id)
    org_row = await session.get(Org, org_id)
    org_row.credit_auto_recharge = {
        "enabled": True,
        "threshold_micros": 5_000_000,
        "amount_micros": 25_000_000,
        "payment_method_id": str(pm_id),
        "fallback": "human_flow",
    }
    await session.commit()

    r = await client.patch(
        "/api/v1/billing/auto-recharge",
        json={"enabled": False},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text

    set_org_context(session, org_id)
    org_row = await session.get(Org, org_id)
    await session.refresh(org_row)
    assert org_row.credit_auto_recharge["enabled"] is False
    assert org_row.credit_auto_recharge["fallback"] == "human_flow"


async def test_auto_recharge_requires_a_payment_method_that_belongs_to_the_workspace(
    client, session
):
    token_a = await register_and_login(client, "auto-a-pm@example.com")
    org_a = await create_org(client, token_a, "Auto A PM Org")

    token_b = await register_and_login(client, "auto-b-pm@example.com")
    org_b = await create_org(client, token_b, "Auto B PM Org")
    org_b_id = uuid.UUID(org_b["id"])

    pm_b_id = uuid.uuid4()
    set_org_context(session, org_b_id)
    session.add(
        PaymentMethod(
            id=pm_b_id,
            org_id=org_b_id,
            stripe_customer_id="cus_auto_b",
            stripe_payment_method_id="pm_auto_b",
            brand="Visa",
            last4="4242",
            is_default=True,
        )
    )
    await session.commit()

    r = await client.patch(
        "/api/v1/billing/auto-recharge",
        json={
            "enabled": True,
            "threshold_micros": 5_000_000,
            "amount_micros": 25_000_000,
            "payment_method_id": str(pm_b_id),
        },
        headers=auth_headers(token_a, org_a["id"]),
    )
    assert r.status_code == 404, r.text


async def test_deleting_the_default_card_promotes_the_next_one(client, session, monkeypatch):
    token = await register_and_login(client, "cards-default@example.com")
    org = await create_org(client, token, "Cards Default Org")
    org_id = uuid.UUID(org["id"])

    pm1_id = uuid.uuid4()
    pm2_id = uuid.uuid4()

    set_org_context(session, org_id)
    session.add(
        PaymentMethod(
            id=pm1_id,
            org_id=org_id,
            stripe_customer_id="cus_cards_default",
            stripe_payment_method_id="pm_cards_default_1",
            brand="Visa",
            last4="4242",
            is_default=True,
        )
    )
    await session.commit()

    set_org_context(session, org_id)
    session.add(
        PaymentMethod(
            id=pm2_id,
            org_id=org_id,
            stripe_customer_id="cus_cards_default",
            stripe_payment_method_id="pm_cards_default_2",
            brand="Mastercard",
            last4="4444",
            is_default=False,
        )
    )
    await session.commit()

    async def fake_detach_payment_method(*args, **kwargs):
        return None

    monkeypatch.setattr(stripe_client, "detach_payment_method", fake_detach_payment_method)

    r = await client.delete(
        f"/api/v1/billing/payment-methods/{pm1_id}",
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 204, r.text

    set_org_context(session, org_id)
    remaining = (
        await session.execute(
            sa.select(PaymentMethod).where(PaymentMethod.org_id == org_id)
        )
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == pm2_id
    assert remaining[0].is_default is True


async def test_a_card_used_by_auto_recharge_cannot_be_deleted(client, session):
    token = await register_and_login(client, "cards-auto-used@example.com")
    org = await create_org(client, token, "Cards Auto Used Org")
    org_id = uuid.UUID(org["id"])

    pm_id = uuid.uuid4()
    set_org_context(session, org_id)
    session.add(
        PaymentMethod(
            id=pm_id,
            org_id=org_id,
            stripe_customer_id="cus_cards_auto_used",
            stripe_payment_method_id="pm_cards_auto_used",
            brand="Visa",
            last4="4242",
            is_default=True,
        )
    )
    await session.commit()

    set_org_context(session, org_id)
    org_row = await session.get(Org, org_id)
    org_row.credit_auto_recharge = {
        "enabled": True,
        "threshold_micros": 5_000_000,
        "amount_micros": 25_000_000,
        "payment_method_id": str(pm_id),
    }
    await session.commit()

    r = await client.delete(
        f"/api/v1/billing/payment-methods/{pm_id}",
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code in (400, 422), r.text
    assert "turn off auto-recharge" in r.text.lower()


async def test_billing_money_routes_require_the_billing_permission(client):
    pytest.skip("No member API to add a non-owner member in this bundle")

