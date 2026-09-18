"""The operator endpoints over HTTP, because the services passing is not the same as the
routes working.

Everything in test_monitor_operator_control.py calls services directly. That is how a key
collision survived: `case_file["recommendation"]` was already taken by the AI case file, where
it holds a plain STRING, so `recommended_level()` called `.get()` on a string and every
decision request against a paused account would have been a 500. No service-level test could
see it, because no service-level test had an AI case file in the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org, OrgMonitoring, User
from app.models import Session as IdentitySession
from app.services import monitor_score
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


async def _customer(session, settings, name: str, *, signals: int = 0) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name=name, slug=f"{name.lower().replace(chr(32), chr(45))}-{org_id.hex[:6]}"))
    await session.commit()
    set_org_context(session, org_id)
    for n in range(signals):
        await monitor_score.add_signal(
            session, settings, org_id, "text_blocked", f"blocked {n}", weight=40
        )
    await session.commit()
    return org_id


async def test_accounts_lists_every_customer_not_just_the_noisy_ones(ops, session, ops_settings):
    token = await _operator(ops, session)
    quiet = await _customer(session, ops_settings, "Quiet Co")
    noisy = await _customer(session, ops_settings, "Noisy Co", signals=3)

    r = await ops.get("/api/v1/ops/monitoring/accounts", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {a["org_id"] for a in body["accounts"]}
    assert str(quiet) in ids and str(noisy) in ids
    assert body["auto_action"] is False, "operators must be able to see the policy in force"

    flagged = next(a for a in body["accounts"] if a["org_id"] == str(noisy))
    assert flagged["needs_decision"] is True
    assert flagged["recommendation"]["level"] == "paused"
    assert flagged["level"] == "watch", "detection must not have restricted the account"
    calm = next(a for a in body["accounts"] if a["org_id"] == str(quiet))
    assert calm["needs_decision"] is False


async def test_needs_decision_is_filtered_before_paging_not_after(ops, session, ops_settings):
    """The bug this pins: filtering a JSON field in Python AFTER limit/offset returns only the
    waiting accounts that happen to land on the first page. With one flagged account buried
    behind twelve quiet ones and a page size of 5, the naive version returns nothing."""
    token = await _operator(ops, session)
    for n in range(12):
        await _customer(session, ops_settings, f"Quiet {n:02d}")
    buried = await _customer(session, ops_settings, "Zebra Last", signals=3)

    r = await ops.get(
        "/api/v1/ops/monitoring/accounts?needs_decision=true&limit=5",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["org_id"] for a in body["accounts"]] == [str(buried)]
    assert body["total"] == 1


async def test_review_on_demand_reports_and_does_not_act(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Review Me", signals=3)

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/review",
        json={"days": 7, "thorough": True},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["actions_taken"] == []
    assert report["monitor"]["level"] == "watch"
    assert report["monitor"]["recommendation"]["level"] == "paused"
    assert report["window_days"] == 7
    assert "headline" in report

    set_org_context(session, org_id)
    state = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one()
    await session.refresh(state)
    assert state.level == "watch", "an on-demand review must never restrict an account"
    assert state.reviewed_at is not None, "the review should be recorded against the account"


async def test_applying_a_recommendation_pauses_and_arms_the_case_file(ops, session, ops_settings):
    """The second bug this pins: an operator-applied pause must leave the SAME state an
    automatic one does. `case_file["status"] == "pending"` is what makes the sweeper write the
    evidence pack and email the owners - without it we pause a customer and tell nobody."""
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Apply Me", signals=3)

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
        json={"action": "apply", "note": "Confirmed courier impersonation across 4 numbers."},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied_level"] == "paused"

    set_org_context(session, org_id)
    state = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one()
    await session.refresh(state)
    assert state.level == "paused"
    assert state.paused_at is not None
    assert state.paused_reason
    assert (state.case_file or {}).get("status") == "pending", "the case file was never armed"
    assert monitor_score.recommended_level(state) is None, "the recommendation must be spent"


async def test_a_decision_on_a_paused_account_with_an_ai_case_file_does_not_500(
    ops, session, ops_settings
):
    """The key collision, end to end. `case_file["recommendation"]` is a STRING written by the
    AI case file. Reading a pending operator decision out of that same key made `.get()` crash
    on a str, so this request returned 500 instead of a clean 409."""
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Cased Co")
    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=True)
    state.level = "paused"
    state.case_file = {
        "status": "written",
        "recommendation": "keep_paused",  # the AI's own key, a plain string
        "evidence": ["three blocked texts"],
    }
    await session.commit()

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
        json={"action": "apply", "note": "trying to decide on an account with a case file"},
        headers=auth_headers(token),
    )
    assert r.status_code == 409, f"expected a clean conflict, got {r.status_code}: {r.text}"
    assert "recommendation" in r.text.lower()


async def test_rejecting_clears_the_recommendation_over_http(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Reject Me", signals=3)

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
        json={"action": "reject", "note": "Checked the messages - this is their real business."},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "reject"

    set_org_context(session, org_id)
    state = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one()
    await session.refresh(state)
    assert state.level != "paused"
    assert monitor_score.recommended_level(state) is None
    assert state.score == 0, "a rejected recommendation must not be re-raised by the arithmetic"


async def test_the_endpoints_require_an_operator(ops, session, ops_settings):
    """A customer must not be able to read the whole platform's account list."""
    org_id = await _customer(session, ops_settings, "Nosy Co")
    token = await register_and_login(ops, "customer@example.com")
    for method, path, body in (
        ("get", "/api/v1/ops/monitoring/accounts", None),
        ("post", f"/api/v1/ops/monitoring/orgs/{org_id}/review", {"days": 7}),
        ("post", f"/api/v1/ops/monitoring/orgs/{org_id}/decision", {"action": "apply", "note": "no"}),
    ):
        r = await getattr(ops, method)(path, headers=auth_headers(token), **({"json": body} if body else {}))
        assert r.status_code == 403, f"{path} allowed a non-operator: {r.status_code}"
