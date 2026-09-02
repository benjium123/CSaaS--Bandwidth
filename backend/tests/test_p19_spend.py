from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    AuditLogEntry,
    Call,
    Message,
    MessageThread,
    OrgMembership,
    OrgNumber,
    ProviderRate,
    ProviderSpendDaily,
    Role,
)
from app.repositories import users as users_repo
from app.services import spend as spend_svc
from app.services import sweeper as sweeper_svc
from tests.conftest import (
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)


async def _org_with_thread(
    client: httpx.AsyncClient,
    session,
    email: str,
    name: str,
    our_e164: str = "+12145550100",
    contact_e164: str = "+19725550101",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Register + create an org, plus one open MessageThread. Returns (org_id, thread_id)."""
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    thread_id = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_id,
            org_id=org_id,
            our_e164=our_e164,
            contact_e164=contact_e164,
            status="open",
        )
    )
    await session.flush()
    await session.commit()
    return org_id, thread_id


async def _add_member_with_role(
    client: httpx.AsyncClient,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = Role(id=uuid.uuid4(), org_id=org_id, name=email, permissions=permissions)
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=user.id,
            role_id=role.id,
        )
    )
    await session.commit()
    return token


async def test_rate_resolution_override_beats_default_unknown_zero(client, session):
    token = await register_and_login(client, "p19-rate@example.com")
    org = await create_org(client, token, "Rate Org")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    # (unit_cost_micros, is_override, is_known)
    assert await spend_svc.resolve_rate(session, "telnyx", "sms_out") == (4_000, False, True)

    session.add(
        ProviderRate(
            id=uuid.uuid4(),
            org_id=org_id,
            provider="telnyx",
            metric="sms_out",
            unit_cost_micros=1_234,
            currency="USD",
        )
    )
    await session.commit()

    set_org_context(session, org_id)
    assert await spend_svc.resolve_rate(session, "telnyx", "sms_out") == (1_234, True, True)
    # Entirely uncatalogued provider: not known, not just "no override".
    assert await spend_svc.resolve_rate(session, "unknown", "sms_out") == (0, False, False)
    assert await spend_svc.resolve_rate(session, "telnyx", "bogus") == (0, False, False)


async def test_rollup_math_idempotent_and_summary_equals_daily(client, session):
    token = await register_and_login(client, "p19-rollup@example.com")
    org = await create_org(client, token, "Rollup Org")
    org_id = uuid.UUID(org["id"])
    day = date(2026, 6, 15)  # June has 30 days
    start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    number_id = uuid.uuid4()
    session.add(
        OrgNumber(
            id=number_id,
            org_id=org_id,
            e164="+12145550123",
            carrier="telnyx",
            is_active=True,
            status="active",
            monthly_cost_cents=1_000,
            purchase_cost_cents=2_000,
            purchased_at=start,
        )
    )

    thread_id = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_id,
            org_id=org_id,
            our_e164="+12145550123",
            contact_e164="+19725559999",
            status="open",
        )
    )

    # Flush the parents (OrgNumber, MessageThread) before the children (Message, Call):
    # with SQLite FKs enforced, a single mixed flush is not guaranteed to order these
    # correctly (see tests/test_inbox_aggregate.py's _seed for the same landmine).
    await session.flush()

    for i in range(3):
        session.add(
            Message(
                id=uuid.uuid4(),
                org_id=org_id,
                thread_id=thread_id,
                direction="outbound",
                status="delivered",
                from_e164="+12145550123",
                to_e164="+19725559999",
                body=f"hi {i}",
                media=[],
                carrier="telnyx",
                provider_message_id=f"p19-rollup-out-{i}",
                created_at=start,
            )
        )

    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread_id,
            direction="inbound",
            status="received",
            from_e164="+19725559999",
            to_e164="+12145550123",
            body="mms",
            media=[{"url": "https://example.com/a.png"}],
            carrier="telnyx",
            provider_message_id="p19-rollup-in",
            created_at=start,
        )
    )

    session.add(
        Call(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="outbound",
            contact_e164="+19725559999",
            our_e164="+12145550123",
            carrier="telnyx",
            status="completed",
            duration_seconds=61,
            created_at=start,
        )
    )
    session.add(
        Call(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="outbound",
            contact_e164="+19725559999",
            our_e164="+12145550123",
            carrier="telnyx",
            status="completed",
            duration_seconds=0,
            created_at=start,
        )
    )
    await session.commit()

    rows_written = await spend_svc.rollup_day(session, org_id, day)
    assert rows_written == 5

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == day,
            )
        )
    ).scalars().all()
    by_key = {(r.provider, r.metric, r.number_id): r for r in rows}

    sms_out = by_key[("telnyx", "sms_out", None)]
    assert sms_out.quantity == 3
    assert sms_out.cost_micros == 3 * 4_000
    assert sms_out.scope_key == spend_svc.TRAFFIC_SCOPE

    mms_in = by_key[("telnyx", "mms_in", None)]
    assert mms_in.quantity == 1
    assert mms_in.cost_micros == 1 * 15_000
    assert mms_in.scope_key == spend_svc.TRAFFIC_SCOPE

    voice_out = by_key[("telnyx", "voice_min_out", None)]
    assert voice_out.quantity == 2
    assert voice_out.cost_micros == 2 * 7_000
    assert voice_out.scope_key == spend_svc.TRAFFIC_SCOPE

    mrc = by_key[("telnyx", "number_mrc", number_id)]
    assert mrc.quantity == 1
    assert mrc.cost_micros == (1_000 * 10_000) // 30
    assert mrc.scope_key == str(number_id)

    setup = by_key[("telnyx", "number_setup", number_id)]
    assert setup.quantity == 1
    assert setup.cost_micros == 2_000 * 10_000
    assert setup.scope_key == str(number_id)

    # Idempotent: recompute replaces, never duplicates.
    rows_written_again = await spend_svc.rollup_day(session, org_id, day)
    assert rows_written_again == 5

    set_org_context(session, org_id)
    rows_after = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == day,
            )
        )
    ).scalars().all()
    assert len(rows_after) == 5

    summary = await spend_svc.summary(session, org_id, day, day)
    daily_rows = await spend_svc.daily(session, org_id, day, day)
    assert summary["total_micros"] == sum(r["cost_micros"] for r in daily_rows)
    assert summary["by_provider"]["telnyx"]["cost_micros"] == summary["total_micros"]
    assert summary["unrated_providers"] == []  # telnyx is fully catalogued

    telnyx_numbers = summary["by_provider"]["telnyx"]["numbers"]
    assert any(n["number_id"] == str(number_id) for n in telnyx_numbers)
    number_entry = next(n for n in telnyx_numbers if n["number_id"] == str(number_id))
    assert number_entry["cost_micros"] == mrc.cost_micros + setup.cost_micros


async def test_org_isolation_of_spend_rows(client, session):
    token_a = await register_and_login(client, "p19-isoa@example.com")
    org_a = await create_org(client, token_a, "Isolation A")
    org_a_id = uuid.UUID(org_a["id"])

    token_b = await register_and_login(client, "p19-isob@example.com")
    org_b = await create_org(client, token_b, "Isolation B")
    org_b_id = uuid.UUID(org_b["id"])

    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)

    set_org_context(session, org_a_id)
    thread_a = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_a,
            org_id=org_a_id,
            our_e164="+12145550124",
            contact_e164="+19725550001",
            status="open",
        )
    )
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_a_id,
            thread_id=thread_a,
            direction="outbound",
            status="delivered",
            from_e164="+12145550124",
            to_e164="+19725550001",
            body="a",
            media=[],
            carrier="telnyx",
            provider_message_id="p19-iso-a",
            created_at=start,
        )
    )
    await session.commit()

    set_org_context(session, org_b_id)
    thread_b = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_b,
            org_id=org_b_id,
            our_e164="+12145550125",
            contact_e164="+19725550002",
            status="open",
        )
    )
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_b_id,
            thread_id=thread_b,
            direction="outbound",
            status="delivered",
            from_e164="+12145550125",
            to_e164="+19725550002",
            body="b",
            media=[],
            carrier="telnyx",
            provider_message_id="p19-iso-b",
            created_at=start,
        )
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_a_id, day)
    await spend_svc.rollup_day(session, org_b_id, day)

    set_org_context(session, org_a_id)
    rows_a = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.period_date == day,
            )
        )
    ).scalars().all()
    assert len(rows_a) == 1
    assert rows_a[0].org_id == org_a_id

    set_org_context(session, org_b_id)
    rows_b = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.period_date == day,
            )
        )
    ).scalars().all()
    assert len(rows_b) == 1
    assert rows_b[0].org_id == org_b_id


async def test_rbac_and_rate_put_validation_audit(client, session):
    owner_token = await register_and_login(client, "p19-rbac-owner@example.com")
    org = await create_org(client, owner_token, "RBAC Org")
    org_id = uuid.UUID(org["id"])

    reports_token = await _add_member_with_role(
        client, session, org_id, "p19-reports@example.com", ["reports:read"]
    )
    reports_headers = auth_headers(reports_token, org["id"])

    r = await client.get(
        "/api/v1/spend/daily?from=2026-06-01&to=2026-06-15",
        headers=reports_headers,
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/provider-rates", headers=reports_headers)
    assert r.status_code == 403

    r = await client.put(
        "/api/v1/provider-rates",
        json={"rates": [{"provider": "telnyx", "metric": "sms_out", "unit_cost_micros": 100}]},
        headers=reports_headers,
    )
    assert r.status_code == 403

    settings_token = await _add_member_with_role(
        client, session, org_id, "p19-settings@example.com", ["settings:write"]
    )
    settings_headers = auth_headers(settings_token, org["id"])

    r = await client.put(
        "/api/v1/provider-rates",
        json={"rates": [{"provider": "telnyx", "metric": "sms_out", "unit_cost_micros": 123}]},
        headers=settings_headers,
    )
    assert r.status_code == 200, r.text

    bad_metric = await client.put(
        "/api/v1/provider-rates",
        json={"rates": [{"provider": "telnyx", "metric": "bogus", "unit_cost_micros": 100}]},
        headers=settings_headers,
    )
    assert bad_metric.status_code == 422

    negative = await client.put(
        "/api/v1/provider-rates",
        json={"rates": [{"provider": "telnyx", "metric": "sms_out", "unit_cost_micros": -1}]},
        headers=settings_headers,
    )
    assert negative.status_code == 422

    set_org_context(session, org_id)
    audit_rows = (
        await session.execute(
            sa.select(AuditLogEntry).where(
                AuditLogEntry.org_id == org_id,
                AuditLogEntry.action == "provider_rates.updated",
            )
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["rates"][0]["provider"] == "telnyx"
    assert audit_rows[0].detail["rates"][0]["metric"] == "sms_out"
    assert audit_rows[0].detail["rates"][0]["unit_cost_micros"] == 123


async def test_rollup_recent_counts_orgs(client, session):
    token_a = await register_and_login(client, "p19-sweep-a@example.com")
    await create_org(client, token_a, "Sweep A")

    token_b = await register_and_login(client, "p19-sweep-b@example.com")
    await create_org(client, token_b, "Sweep B")

    assert await spend_svc.rollup_recent(session) == 2


async def test_number_mrc_prorated_across_month_lengths_no_override(client, session):
    """No `monthly_cost_cents` set -> falls back to the resolved rate, prorated by the
    ACTUAL days in that number's month (28-day Feb 2026 is not a leap year; 31-day Jul)."""
    token = await register_and_login(client, "p19-mrc@example.com")
    org = await create_org(client, token, "MRC Org")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    telnyx_rate = (await spend_svc.resolve_rate(session, "telnyx", "number_mrc"))[0]

    feb_number_id = uuid.uuid4()
    session.add(
        OrgNumber(
            id=feb_number_id,
            org_id=org_id,
            e164="+12145550200",
            carrier="telnyx",
            is_active=True,
            status="active",
            monthly_cost_cents=None,
            purchased_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    jul_number_id = uuid.uuid4()
    session.add(
        OrgNumber(
            id=jul_number_id,
            org_id=org_id,
            e164="+12145550201",
            carrier="telnyx",
            is_active=True,
            status="active",
            monthly_cost_cents=None,
            purchased_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    feb_day = date(2026, 2, 10)  # 2026 is not a leap year -> 28 days
    jul_day = date(2026, 7, 10)  # 31 days

    await spend_svc.rollup_day(session, org_id, feb_day)
    await spend_svc.rollup_day(session, org_id, jul_day)

    set_org_context(session, org_id)
    feb_row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == feb_day,
                ProviderSpendDaily.number_id == feb_number_id,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalar_one()
    jul_row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == jul_day,
                ProviderSpendDaily.number_id == jul_number_id,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalar_one()

    assert feb_row.cost_micros == telnyx_rate // 28
    assert jul_row.cost_micros == telnyx_rate // 31
    assert feb_row.cost_micros != jul_row.cost_micros


async def test_unknown_carrier_still_counted_with_zero_cost(client, session):
    token = await register_and_login(client, "p19-unknown@example.com")
    org = await create_org(client, token, "Unknown Carrier Org")
    org_id = uuid.UUID(org["id"])
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    thread_id = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_id,
            org_id=org_id,
            our_e164="+12145550300",
            contact_e164="+19725550301",
            status="open",
        )
    )
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread_id,
            direction="outbound",
            status="delivered",
            from_e164="+12145550300",
            to_e164="+19725550301",
            body="hi",
            media=[],
            carrier="acme_unknown",
            provider_message_id="p19-unknown-1",
            created_at=start,
        )
    )
    await session.commit()

    rows_written = await spend_svc.rollup_day(session, org_id, day)
    assert rows_written == 1

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.provider == "acme_unknown",
            )
        )
    ).scalar_one()
    assert row.metric == "sms_out"
    assert row.quantity == 1
    assert row.cost_micros == 0
    assert row.scope_key == spend_svc.TRAFFIC_SCOPE

    # unrated_providers: a carrier with no catalogue entry at all is flagged in the
    # summary payload, distinct from a catalogued carrier with a real $0 rate.
    summary = await spend_svc.summary(session, org_id, day, day)
    assert summary["unrated_providers"] == ["acme_unknown"]


async def test_date_range_over_366_days_rejected(client, session):
    token = await register_and_login(client, "p19-range@example.com")
    org = await create_org(client, token, "Range Org")
    headers = auth_headers(token, org["id"])

    r = await client.get(
        "/api/v1/spend/summary?from=2024-01-01&to=2026-06-15", headers=headers
    )
    assert r.status_code == 422

    r = await client.get(
        "/api/v1/spend/daily?from=2024-01-01&to=2026-06-15", headers=headers
    )
    assert r.status_code == 422


async def test_sweeper_run_once_rolls_up_spend_and_gates_hourly(client, session):
    token = await register_and_login(client, "p19-sweeper@example.com")
    await create_org(client, token, "Sweeper Spend Org")

    fake_app = SimpleNamespace(state=SimpleNamespace(settings=make_settings()))

    results = await sweeper_svc.run_once(fake_app)
    assert results["spend_orgs_rolled_up"] >= 1

    # Hourly gate: an immediate second pass must not re-run (no _spend_last_run reset).
    results_again = await sweeper_svc.run_once(fake_app)
    assert "spend_orgs_rolled_up" not in results_again


async def test_effective_rates_reports_default_alongside_override(client, session):
    """UI Reset-to-default needs the code default even when an org override exists -
    GET /provider-rates must never make the caller keep its own copy of DEFAULT_RATES."""
    owner_token = await register_and_login(client, "p19-default@example.com")
    org = await create_org(client, owner_token, "Default Rates Org")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(owner_token, org["id"])

    set_org_context(session, org_id)
    known_default = spend_svc.DEFAULT_RATES_MICROS["telnyx"]["sms_out"]

    # Before any override: unit_cost_micros == default_unit_cost_micros, is_override False.
    r = await client.get("/api/v1/provider-rates", headers=headers)
    assert r.status_code == 200, r.text
    before = next(
        row for row in r.json() if row["provider"] == "telnyx" and row["metric"] == "sms_out"
    )
    assert before["unit_cost_micros"] == known_default
    assert before["default_unit_cost_micros"] == known_default
    assert before["is_override"] is False

    put = await client.put(
        "/api/v1/provider-rates",
        json={
            "rates": [
                {"provider": "telnyx", "metric": "sms_out", "unit_cost_micros": 999_999}
            ]
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text

    r = await client.get("/api/v1/provider-rates", headers=headers)
    assert r.status_code == 200, r.text
    after = next(
        row for row in r.json() if row["provider"] == "telnyx" and row["metric"] == "sms_out"
    )
    assert after["unit_cost_micros"] == 999_999
    assert after["is_override"] is True
    # The code default is still reported, unchanged, so the UI can offer Reset-to-default
    # without a second lookup.
    assert after["default_unit_cost_micros"] == known_default
    assert after["default_unit_cost_micros"] != after["unit_cost_micros"]


async def test_spend_rollup_route_rbac_valid_day_and_future_rejected(client, session):
    """POST /api/v1/spend/rollup - the frontend's "Recalculate today" button. settings:write
    only, rolls up the caller's own org, rejects a future day."""
    owner_token = await register_and_login(client, "p19-rolluproute-owner@example.com")
    org = await create_org(client, owner_token, "Rollup Route Org")
    org_id = uuid.UUID(org["id"])
    owner_headers = auth_headers(owner_token, org["id"])

    reports_token = await _add_member_with_role(
        client, session, org_id, "p19-rolluproute-reader@example.com", ["settings:read"]
    )
    reports_headers = auth_headers(reports_token, org["id"])

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    # RBAC: settings:read is not enough - the route requires settings:write.
    r = await client.post(
        "/api/v1/spend/rollup", params={"day": yesterday.isoformat()}, headers=reports_headers
    )
    assert r.status_code == 403

    # Seed one outbound message on `yesterday` so rows_written is deterministic.
    set_org_context(session, org_id)
    thread_id = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_id,
            org_id=org_id,
            our_e164="+12145550400",
            contact_e164="+19725550401",
            status="open",
        )
    )
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread_id,
            direction="outbound",
            status="delivered",
            from_e164="+12145550400",
            to_e164="+19725550401",
            body="hi",
            media=[],
            carrier="telnyx",
            provider_message_id="p19-rollup-route-1",
            created_at=datetime(
                yesterday.year, yesterday.month, yesterday.day, 12, 0, tzinfo=timezone.utc
            ),
        )
    )
    await session.commit()

    r = await client.post(
        "/api/v1/spend/rollup", params={"day": yesterday.isoformat()}, headers=owner_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["day"] == yesterday.isoformat()
    assert body["rows_written"] == 1

    future = today + timedelta(days=5)
    r = await client.post(
        "/api/v1/spend/rollup", params={"day": future.isoformat()}, headers=owner_headers
    )
    assert r.status_code == 422


# ======================================================================================
# Opus review (money math): quantity = sum of billed segments, not row counts; only
# billable outbound statuses count; MRC eligibility is purchased/released-window based,
# not status=="active"; scope_key; unrated_providers; explicit org threading.
# ======================================================================================
async def test_rollup_plain_inbound_sms(client, session):
    org_id, thread_id = await _org_with_thread(client, session, "p19-in-sms@example.com", "Inbound SMS Org")
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="inbound",
            status="received", from_e164="+19725550101", to_e164="+12145550100",
            body="hey", media=[], carrier="telnyx", provider_message_id="p19-in-1",
            created_at=start,
        )
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day
            )
        )
    ).scalar_one()
    assert row.metric == "sms_in"
    assert row.quantity == 1
    assert row.cost_micros == 4_000  # telnyx sms_in default
    assert row.scope_key == spend_svc.TRAFFIC_SCOPE


async def test_rollup_inbound_call_voice_minutes(client, session):
    org_id, _thread_id = await _org_with_thread(client, session, "p19-in-call@example.com", "Inbound Call Org")
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    session.add(
        Call(
            id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164="+19725550101",
            our_e164="+12145550100", carrier="telnyx", status="completed",
            duration_seconds=130, created_at=start,
        )
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day
            )
        )
    ).scalar_one()
    assert row.metric == "voice_min_in"
    assert row.quantity == 3  # ceil(130/60) == 3
    assert row.cost_micros == 3 * 3_500  # telnyx voice_min_in default


async def test_rollup_outbound_mms(client, session):
    org_id, thread_id = await _org_with_thread(client, session, "p19-mms-out@example.com", "Outbound MMS Org")
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
            status="delivered", from_e164="+12145550100", to_e164="+19725550101",
            body="pic", media=[{"url": "https://example.com/a.png"}], carrier="telnyx",
            provider_message_id="p19-mms-out-1", created_at=start,
        )
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day
            )
        )
    ).scalar_one()
    assert row.metric == "mms_out"
    assert row.quantity == 1
    assert row.cost_micros == 15_000  # telnyx mms_out default


async def test_rollup_message_quantity_sums_segment_counts_not_rows(client, session):
    """THE money-math fix: quantity is the sum of billed segments per message
    (carrier-reported when known, else our own estimate, else 1) - never a row count."""
    org_id, thread_id = await _org_with_thread(client, session, "p19-segments@example.com", "Segments Org")
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    session.add_all(
        [
            # carrier-reported count wins over our own estimate.
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="delivered", from_e164="+12145550100", to_e164="+19725550101",
                body="a", media=[], carrier="telnyx", provider_message_id="p19-seg-1",
                segment_count_carrier=3, segment_count_est=2, created_at=start,
            ),
            # no carrier report yet -> falls back to our own estimate.
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="delivered", from_e164="+12145550100", to_e164="+19725550101",
                body="b", media=[], carrier="telnyx", provider_message_id="p19-seg-2",
                segment_count_carrier=None, segment_count_est=2, created_at=start,
            ),
            # neither reported -> falls back to 1.
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="delivered", from_e164="+12145550100", to_e164="+19725550101",
                body="c", media=[], carrier="telnyx", provider_message_id="p19-seg-3",
                created_at=start,
            ),
        ]
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "sms_out",
            )
        )
    ).scalar_one()
    # 3 messages, but 3 + 2 + 1 = 6 billed segments - not a row count of 3.
    assert row.quantity == 6
    assert row.cost_micros == 6 * 4_000


async def test_rollup_excludes_queued_and_rejected_outbound(client, session):
    org_id, thread_id = await _org_with_thread(client, session, "p19-excl@example.com", "Exclusion Org")
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    thread2 = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread2, org_id=org_id, our_e164="+12145550102", contact_e164="+19725550103",
            status="open",
        )
    )
    await session.flush()
    session.add_all(
        [
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="delivered", from_e164="+12145550100", to_e164="+19725550101",
                body="ok", media=[], carrier="telnyx", provider_message_id="p19-excl-ok",
                created_at=start,
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="queued", from_e164="+12145550100", to_e164="+19725550101",
                body="q", media=[], carrier="telnyx", provider_message_id="p19-excl-q",
                created_at=start,
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="rejected", from_e164="+12145550100", to_e164="+19725550101",
                body="r", media=[], carrier="telnyx", provider_message_id="p19-excl-r",
                created_at=start,
            ),
            # A SECOND provider where EVERY outbound message is non-billable: the group
            # must never appear at all, not as a zero-quantity row.
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread2, direction="outbound",
                status="queued", from_e164="+12145550102", to_e164="+19725550103",
                body="q2", media=[], carrier="bandwidth", provider_message_id="p19-excl-q2",
                created_at=start,
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread2, direction="outbound",
                status="rejected", from_e164="+12145550102", to_e164="+19725550103",
                body="r2", media=[], carrier="bandwidth", provider_message_id="p19-excl-r2",
                created_at=start,
            ),
        ]
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    telnyx_row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.provider == "telnyx",
            )
        )
    ).scalar_one()
    assert telnyx_row.quantity == 1  # only the delivered message counts

    bandwidth_row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.provider == "bandwidth",
            )
        )
    ).scalar_one_or_none()
    assert bandwidth_row is None


async def test_rollup_honors_org_rate_override(client, session):
    org_id, thread_id = await _org_with_thread(client, session, "p19-override@example.com", "Override Org")
    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    set_org_context(session, org_id)
    session.add(
        ProviderRate(
            id=uuid.uuid4(), org_id=org_id, provider="telnyx", metric="sms_out",
            unit_cost_micros=999, currency="USD",
        )
    )
    session.add_all(
        [
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="delivered", from_e164="+12145550100", to_e164="+19725550101",
                body=f"m{i}", media=[], carrier="telnyx", provider_message_id=f"p19-override-{i}",
                created_at=start,
            )
            for i in range(2)
        ]
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "sms_out",
            )
        )
    ).scalar_one()
    assert row.quantity == 2
    assert row.cost_micros == 2 * 999  # override, not the 4_000 default


async def test_number_setup_absent_day_after_purchase(client, session):
    token = await register_and_login(client, "p19-setupgap@example.com")
    org = await create_org(client, token, "Setup Gap Org")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    purchase_day = date(2026, 6, 15)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164="+12145550600", carrier="telnyx",
            is_active=True, status="active", purchase_cost_cents=500,
            purchased_at=datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, purchase_day)
    set_org_context(session, org_id)
    setup_row = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == purchase_day,
                ProviderSpendDaily.metric == "number_setup",
            )
        )
    ).scalar_one_or_none()
    assert setup_row is not None
    assert setup_row.cost_micros == 500 * 10_000

    next_day = purchase_day + timedelta(days=1)
    await spend_svc.rollup_day(session, org_id, next_day)
    set_org_context(session, org_id)
    setup_row_next = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == next_day,
                ProviderSpendDaily.metric == "number_setup",
            )
        )
    ).scalar_one_or_none()
    assert setup_row_next is None  # setup is charged once, on the purchase day only

    # But the number still existed the day after purchase, so MRC still accrues.
    mrc_row_next = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == next_day,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalar_one_or_none()
    assert mrc_row_next is not None


async def test_no_mrc_for_number_purchased_after_rollup_day(client, session):
    token = await register_and_login(client, "p19-futurepurchase@example.com")
    org = await create_org(client, token, "Future Purchase Org")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    rollup_target_day = date(2026, 6, 15)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164="+12145550700", carrier="telnyx",
            is_active=True, status="active", monthly_cost_cents=1_000,
            # Purchased the day AFTER the day being rolled up - it did not exist yet.
            purchased_at=datetime(2026, 6, 16, 8, 0, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    rows_written = await spend_svc.rollup_day(session, org_id, rollup_target_day)
    assert rows_written == 0


async def test_released_number_mrc_released_at_clause_is_live(client, session):
    """A release must never erase PAST MRC (relaxed status != "pending" predicate), but
    the released_at/start comparison must still be a REAL, live filter - not vacuously
    true just because status no longer gates it."""
    token = await register_and_login(client, "p19-release@example.com")
    org = await create_org(client, token, "Release Org")
    org_id = uuid.UUID(org["id"])
    day = date(2026, 6, 15)
    set_org_context(session, org_id)
    number_id = uuid.uuid4()
    session.add(
        OrgNumber(
            id=number_id, org_id=org_id, e164="+12145550500", carrier="telnyx",
            is_active=True, status="active", monthly_cost_cents=1_000,
            purchased_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row_before = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalar_one()

    # Release the number AFTER `day` (released_at >= that day's start): a re-rollup of
    # this past day must still carry its MRC.
    num = await session.get(OrgNumber, number_id)
    num.status = "released"
    num.released_at = datetime(2026, 6, 20, tzinfo=timezone.utc)
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row_after_release = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalar_one()
    assert row_after_release.cost_micros == row_before.cost_micros

    # Now move released_at to BEFORE `day`'s start: the clause must actually exclude
    # it - proving it is a live comparison, not a no-op now that status doesn't gate.
    num2 = await session.get(OrgNumber, number_id)
    num2.released_at = datetime(2026, 6, 10, tzinfo=timezone.utc)
    await session.commit()

    await spend_svc.rollup_day(session, org_id, day)
    set_org_context(session, org_id)
    row_after_early_release = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id, ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalar_one_or_none()
    assert row_after_early_release is None


async def test_http_spend_summary_org_isolation(client, session):
    token_a = await register_and_login(client, "p19-httpisoa@example.com")
    org_a = await create_org(client, token_a, "HTTP Iso A")
    org_a_id = uuid.UUID(org_a["id"])

    token_b = await register_and_login(client, "p19-httpisob@example.com")
    org_b = await create_org(client, token_b, "HTTP Iso B")
    org_b_id = uuid.UUID(org_b["id"])

    day = date(2026, 6, 15)
    start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    set_org_context(session, org_a_id)
    thread_a = uuid.uuid4()
    session.add(
        MessageThread(
            id=thread_a, org_id=org_a_id, our_e164="+12145550110",
            contact_e164="+19725550111", status="open",
        )
    )
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_a_id, thread_id=thread_a, direction="outbound",
            status="delivered", from_e164="+12145550110", to_e164="+19725550111",
            body="a", media=[], carrier="telnyx", provider_message_id="p19-httpiso-a",
            created_at=start,
        )
    )
    await session.commit()
    await spend_svc.rollup_day(session, org_a_id, day)

    r_a = await client.get(
        "/api/v1/spend/summary",
        params={"from": day.isoformat(), "to": day.isoformat()},
        headers=auth_headers(token_a, org_a["id"]),
    )
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["total_micros"] > 0

    r_b = await client.get(
        "/api/v1/spend/summary",
        params={"from": day.isoformat(), "to": day.isoformat()},
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["total_micros"] == 0
    assert r_b.json()["by_provider"] == {}


async def test_provider_spend_daily_unique_key_collision_raises(client, session):
    token = await register_and_login(client, "p19-collision@example.com")
    org = await create_org(client, token, "Collision Org")
    org_id = uuid.UUID(org["id"])
    day = date(2026, 6, 15)

    set_org_context(session, org_id)
    session.add(
        ProviderSpendDaily(
            id=uuid.uuid4(), org_id=org_id, period_date=day, provider="telnyx",
            metric="sms_out", quantity=1, cost_micros=4_000, number_id=None,
            scope_key=spend_svc.TRAFFIC_SCOPE,
        )
    )
    await session.commit()

    set_org_context(session, org_id)
    session.add(
        ProviderSpendDaily(
            id=uuid.uuid4(), org_id=org_id, period_date=day, provider="telnyx",
            metric="sms_out", quantity=1, cost_micros=4_000, number_id=None,
            scope_key=spend_svc.TRAFFIC_SCOPE,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()
