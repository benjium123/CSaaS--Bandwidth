"""HTTP tests for the ops console endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import BillingPayment, BillingRefusal, Call, Org, User
from app.models import Session as IdentitySession
from app.services import bundles, credits, telephony_billing
from tests.conftest import auth_headers, make_settings, register_and_login


@pytest.fixture
def ops_settings():
    return make_settings(monitor_enforced=True, monitor_auto_action=False, kyc_enforced=False)


@pytest.fixture
async def ops(engine, ops_settings):
    from app.main import create_app

    application = create_app(ops_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _operator(client, session, email="ops@example.com", role="admin") -> str:
    """A named platform operator with a second factor and a session that proved it."""
    from app.services import operators as operators_svc

    token = await register_and_login(client, email)
    user = (
        await session.execute(
            sa.select(User).where(sa.func.lower(User.email) == email).execution_options(
                allow_unscoped=True
            )
        )
    ).scalar_one()
    user.has_passkey = True
    await operators_svc.grant(session, email=email, role=role)
    live = (
        await session.execute(
            sa.select(IdentitySession)
            .where(IdentitySession.user_id == user.id)
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    live.second_factor_at = datetime.now(timezone.utc)
    await session.commit()
    return token


async def _new_org(session, name: str, *, prepaid: bool = False) -> uuid.UUID:
    """Create a tenant org and optionally mark it as telephony prepaid."""
    org_id = uuid.uuid4()
    org = Org(
        id=org_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{org_id.hex[:6]}",
        telephony_prepaid=prepaid,
    )
    if prepaid:
        org.telephony_prepaid_since = datetime.now(timezone.utc)
    session.add(org)
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def _enable(session, org_id: uuid.UUID) -> None:
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.telephony_prepaid_since = datetime.now(timezone.utc)
    await session.commit()


async def _outbound_call(
    session,
    org_id,
    *,
    direction="outbound",
    status="answered",
    answered_at=None,
    ended_at=None,
    created_at=None,
    duration_seconds=0,
    extra=None,
):
    """Create a Call row with required telephony fields populated."""
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction=direction,
        status=status,
        answered_at=answered_at,
        ended_at=ended_at,
        created_at=created_at or datetime.now(timezone.utc),
        duration_seconds=duration_seconds,
        extra=extra or {},
        contact_e164="+15550001000",
        our_e164="+15550002000",
        carrier="telnyx",
    )
    session.add(call)
    await session.commit()
    return call


async def _seed_console_data(session) -> tuple[uuid.UUID, uuid.UUID]:
    """Build the org A activity and return org ids for A and B."""
    now = datetime.now(timezone.utc)

    A = await _new_org(session, "A Telecom", prepaid=True)
    B = await _new_org(session, "B Plumbing", prepaid=False)

    set_org_context(session, A)
    await credits.topup(session, A, 50_000_000, reference="pi_a1")
    set_org_context(session, A)
    await credits.charge_usage(session, A, 30_000, reference="u1")
    set_org_context(session, A)
    await bundles.credit(session, A, "sms", 5000, reference="pi:pi_b1")
    set_org_context(session, A)
    await bundles.take(session, A, "sms", 3, reference="m1")

    set_org_context(session, A)
    session.add(
        BillingPayment(
            id=uuid.uuid4(),
            org_id=A,
            kind="sms_bundle",
            state="paid",
            quantity=5,
            list_micros=60_000_000,
            paid_micros=48_000_000,
            discount_micros=12_000_000,
            stripe_fee_micros=1_700_000,
            units_credited=5000,
            paid_at=now,
            stripe_payment_intent_id="pi_b1",
        )
    )
    session.add(
        BillingRefusal(
            id=uuid.uuid4(),
            org_id=A,
            kind="sms",
            created_at=now,
        )
    )
    await session.commit()

    set_org_context(session, A)
    await _outbound_call(
        session,
        A,
        direction="outbound",
        status="answered",
        answered_at=now - timedelta(seconds=61),
        created_at=now - timedelta(seconds=61),
        ended_at=now,
        duration_seconds=61,
    )

    set_org_context(session, A)
    await _outbound_call(
        session,
        A,
        direction="inbound",
        status="answered",
        answered_at=now - timedelta(seconds=90),
        created_at=now - timedelta(seconds=90),
        ended_at=now,
        duration_seconds=90,
    )

    set_org_context(session, A)
    await _outbound_call(
        session,
        A,
        direction="inbound",
        status="failed",
        answered_at=None,
        created_at=now,
        ended_at=now,
        duration_seconds=0,
        extra={"refused": "no_credit"},
    )

    return A, B


async def test_orgs_endpoint_returns_all_orgs_and_metrics(ops, session, ops_settings):
    token = await _operator(ops, session)
    A, B = await _seed_console_data(session)

    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"orgs", "totals", "summary"}

    orgs = body["orgs"]
    ids = {o["org_id"] for o in orgs}
    assert str(A) in ids
    assert str(B) in ids

    a = next(o for o in orgs if o["org_id"] == str(A))
    assert a["prepaid"] is True
    assert a["balance_micros"] == 50_000_000 - 30_000
    assert a["sms_bundle_units"] == 4997

    m = a["metrics"]
    assert m["paid"] == 48_000_000
    assert m["discount"] == 12_000_000
    assert m["stripe_fees"] == 1_700_000
    assert m["usage_revenue"] == 30_000
    assert m["minutes_out"] == 2
    assert m["minutes_in"] == 2
    assert m["calls_out"] == 1
    assert m["calls_in"] == 1
    assert m["blocked_credit_inbound_call"] == 1
    assert m["blocked_credit_sms"] == 1
    assert m["sms_bundle_units_used"] == 3
    assert m["cash_profit"] == 48_000_000 - 1_700_000 - m["carrier_cost"]


async def test_org_detail_has_series_ledger_payments_refusals(ops, session, ops_settings):
    token = await _operator(ops, session)
    A, _ = await _seed_console_data(session)

    r = await ops.get(f"/api/v1/ops/console/orgs/{A}", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["series"]) == 30
    assert body["ledger"], "ledger should not be empty"
    assert len(body["payments"]) == 1
    assert body["payments"][0]["stripe_payment_intent_id"] == "pi_b1"
    assert len(body["refusals"]) == 1
    assert body["refusals"][0]["kind"] == "sms"


async def test_orgs_csv(ops, session, ops_settings):
    token = await _operator(ops, session)
    await _seed_console_data(session)

    r = await ops.get("/api/v1/ops/console/orgs.csv", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")

    header = r.text.splitlines()[0].lower()
    assert "org_id" in header
    assert "paid" in header


async def test_prices_default_put_and_unit_price(ops, session, ops_settings):
    token = await _operator(ops, session)
    A, _ = await _seed_console_data(session)

    r = await ops.get("/api/v1/ops/console/prices", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    prices = r.json()["prices"]
    sms_out = next(p for p in prices if p["metric"] == "sms_out")
    assert sms_out["price_micros"] == 15_000

    r = await ops.put(
        "/api/v1/ops/console/prices/sms_out",
        json={"price_micros": 16_000, "note": "test"},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text

    r = await ops.get("/api/v1/ops/console/prices", headers=auth_headers(token))
    prices = r.json()["prices"]
    sms_out = next(p for p in prices if p["metric"] == "sms_out")
    assert sms_out["price_micros"] == 16_000

    set_org_context(session, A)
    assert await telephony_billing.unit_price(session, A, "telnyx", "sms_out") == 16_000

    r = await ops.put(
        "/api/v1/ops/console/prices/not_a_metric",
        json={"price_micros": 1, "note": "x"},
        headers=auth_headers(token),
    )
    assert 400 <= r.status_code < 500, r.text


async def test_adjust_balance(ops, session, ops_settings):
    token = await _operator(ops, session)
    A, _ = await _seed_console_data(session)

    before = (
        await ops.get(f"/api/v1/ops/console/orgs/{A}", headers=auth_headers(token))
    ).json()
    previous = before["org"]["balance_micros"]

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{A}/adjust",
        json={"amount_micros": 10_000_000, "note": "launch credit"},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["balance_after_micros"] == previous + 10_000_000

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{A}/adjust",
        json={"amount_micros": 0, "note": "bad"},
        headers=auth_headers(token),
    )
    assert 400 <= r.status_code < 500, r.text


async def test_add_bundle(ops, session, ops_settings):
    token = await _operator(ops, session)
    B = await _new_org(session, "B Plumbing", prepaid=False)

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{B}/bundles",
        json={"kind": "mms", "units": 100, "note": "gift"},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["units_after"] == 100


async def test_set_prepaid(ops, session, ops_settings):
    token = await _operator(ops, session)
    B = await _new_org(session, "B Plumbing", prepaid=False)

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{B}/prepaid",
        json={"enabled": True},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["prepaid"] is True

    set_org_context(session, B)
    session.expunge_all()
    org = await session.get(Org, B)
    assert org.telephony_prepaid_since is not None


async def test_reviewer_can_read_but_not_write(ops, session, ops_settings):
    A = await _new_org(session, "A Telecom", prepaid=True)
    reviewer = await _operator(ops, session, email="rev@example.com", role="reviewer")

    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(reviewer))
    assert r.status_code == 200, r.text

    r = await ops.put(
        "/api/v1/ops/console/prices/sms_out",
        json={"price_micros": 1, "note": "no"},
        headers=auth_headers(reviewer),
    )
    assert r.status_code == 403, r.text

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{A}/adjust",
        json={"amount_micros": 1000, "note": "no"},
        headers=auth_headers(reviewer),
    )
    assert r.status_code == 403, r.text


async def test_orgs_requires_operator_and_authentication(ops, session, ops_settings):
    await _new_org(session, "Noisy Customer")

    r = await ops.get("/api/v1/ops/console/orgs")
    assert r.status_code in (401, 403), r.status_code

    token = await register_and_login(ops, "customer@example.com")
    r = await ops.get("/api/v1/ops/console/orgs", headers=auth_headers(token))
    assert r.status_code in (401, 403), r.status_code


async def test_large_credit_needs_a_second_operator(ops, session, ops_settings):
    """P44d: above $50/day one operator only REQUESTS credit; a different admin applies it."""
    first = await _operator(ops, session)
    B = await _new_org(session, "B Plumbing", prepaid=False)

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{B}/adjust",
        json={"amount_micros": 40_000_000, "note": "goodwill"},
        headers=auth_headers(first),
    )
    assert r.json()["balance_after_micros"] == 40_000_000  # under the solo limit
    r = await ops.post(
        f"/api/v1/ops/console/orgs/{B}/adjust",
        json={"amount_micros": 20_000_000, "note": "more goodwill"},
        headers=auth_headers(first),
    )
    assert r.status_code == 200, r.text
    pending_id = r.json()["pending_approval"]
    assert await credits.balance(session, B) == 40_000_000  # not applied yet

    r = await ops.post(
        f"/api/v1/ops/console/grants/{pending_id}/decide",
        json={"approve": True},
        headers=auth_headers(first),
    )
    assert r.status_code == 403, r.text  # the requester cannot approve their own grant

    second = await _operator(ops, session, email="ops2@example.com")
    listed = (await ops.get("/api/v1/ops/console/grants/pending", headers=auth_headers(second))).json()
    assert [g["id"] for g in listed] == [pending_id]
    r = await ops.post(
        f"/api/v1/ops/console/grants/{pending_id}/decide",
        json={"approve": True},
        headers=auth_headers(second),
    )
    assert r.status_code == 200, r.text
    assert r.json()["balance_after_micros"] == 60_000_000
    r = await ops.post(
        f"/api/v1/ops/console/grants/{pending_id}/decide",
        json={"approve": True},
        headers=auth_headers(second),
    )
    assert r.status_code == 409  # decided once only


async def test_big_bundle_grant_needs_a_second_operator(ops, session, ops_settings):
    token = await _operator(ops, session)
    B = await _new_org(session, "B Plumbing", prepaid=False)
    r = await ops.post(
        f"/api/v1/ops/console/orgs/{B}/bundles",
        json={"kind": "sms", "units": 50_000, "note": "gift"},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert "pending_approval" in r.json()
    assert await bundles.units(session, B, "sms") == 0
