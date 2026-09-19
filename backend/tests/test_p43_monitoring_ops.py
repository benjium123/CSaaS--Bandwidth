# ruff: noqa: E501, F811
"""P43 monitoring for people: customers see a pause and appeal; the public reports numbers;
operators work the queue, decide held texts and cases; the canary and exam prove it works."""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    MonitorHealth,
    MonitorLabel,
    MonitorSignal,
    NumberReport,
    OrgMonitoring,
    SecurityAlert,
)
from app.services import monitor_exam, monitor_score
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    FakeCarrier,
    _install,
    auth_headers,
    make_org_with_number,
    make_settings,
)
from tests.fake_ai import FakeSafetyAI
from tests.test_p41_kyc import _make_operator

OUR = "+15125550100"


@pytest.fixture
def ops_settings():
    return make_settings(
        monitor_enforced=True,
        # These suites exercise the ENFORCEMENT mechanism (a pause blocks traffic, writes a
        # case file, is appealable, is undone by an operator). The product default is now
        # detect-only - MONITOR_AUTO_ACTION off, so a restricting score only RECOMMENDS and a
        # human decides - so these opt into automatic action to keep testing the mechanism
        # they were written for. The detect-only policy has its own tests in
        # tests/test_monitor_operator_control.py.
        monitor_auto_action=True,
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
    )


@pytest.fixture
async def mon(engine, ops_settings):
    application = create_app(ops_settings)
    carrier = FakeCarrier()
    _install(application, carrier)
    fake = FakeSafetyAI()
    with fake.installed():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            yield client, carrier, fake, application


async def _paused_org(client, session, settings):
    token, org, _num = await make_org_with_number(
        client, f"o-{uuid.uuid4().hex[:6]}@example.com", "Paused Co", OUR
    )
    org_id = uuid.UUID(org["id"])
    for _ in range(4):
        await monitor_score.add_signal(session, settings, org_id, "text_blocked", "Blocked text: gift cards")
    await session.commit()
    return token, org, org_id


async def test_customer_sees_pause_and_appeals(mon, session, ops_settings):
    client, _carrier, _fake, _app = mon
    token, org, org_id = await _paused_org(client, session, ops_settings)
    h = auth_headers(token, org["id"])
    status = (await client.get("/api/v1/monitoring/status", headers=h)).json()
    assert status["level"] == "paused" and "paused" in status["message"]
    r = await client.post(
        "/api/v1/monitoring/appeal",
        json={"explanation": "Those texts were a staff member testing our phishing training."},
        headers=h,
    )
    assert r.status_code == 200, r.text
    set_org_context(session, org_id)
    state = (await session.execute(sa.select(OrgMonitoring))).scalar_one()
    assert "phishing training" in state.appeal
    alert = (await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "monitor_appeal"))).scalar_one()
    assert alert.org_id == org_id


async def test_watch_level_is_invisible_to_customers(mon, session, ops_settings):
    client, _carrier, _fake, _app = mon
    token, org, _num = await make_org_with_number(client, f"w-{uuid.uuid4().hex[:6]}@example.com", "Watch Co", OUR)
    await monitor_score.add_signal(session, ops_settings, uuid.UUID(org["id"]), "text_blocked", "x", weight=35)
    await session.commit()
    status = (await client.get("/api/v1/monitoring/status", headers=auth_headers(token, org["id"]))).json()
    assert status["level"] == "normal" and status["message"] is None


async def test_public_report_is_anonymous_and_triaged(mon, session, ops_settings):
    from app.api.routes import monitoring as monitoring_routes

    client, _carrier, fake, _app = mon
    _token, org, _num = await make_org_with_number(client, f"r-{uuid.uuid4().hex[:6]}@example.com", "Reported Co", OUR)
    ours = await client.post(
        "/api/v1/public/report-number",
        json={"number": OUR, "kind": "call", "description": "They said I owe the IRS and must pay with gift cards."},
    )
    not_ours = await client.post(
        "/api/v1/public/report-number",
        json={"number": "+15125559999", "kind": "text", "description": "Weird text about a package."},
    )
    assert ours.status_code == not_ours.status_code == 202
    assert ours.json() == not_ours.json()  # never reveals whether the number is ours
    assert await monitoring_routes.assess_reports_tick(session, ops_settings) == 1
    reports = (await session.execute(sa.select(NumberReport))).scalars().all()
    assert all(r.assessment is not None for r in reports)
    set_org_context(session, uuid.UUID(org["id"]))
    signal = (await session.execute(sa.select(MonitorSignal))).scalar_one()
    assert signal.kind == "public_report"


async def test_operator_releases_a_held_text_and_it_is_learned(mon, session, ops_settings):
    client, carrier, fake, app = mon
    token, org, _num = await make_org_with_number(client, f"h-{uuid.uuid4().hex[:6]}@example.com", "Held Co", OUR)
    fake.text_verdict = lambda t: {"verdict": "hold", "category": "off_business", "confidence": 50, "reason": "Unusual"}
    await client.post("/api/v1/messages", json={"to": "+15125550199", "from": OUR, "body": "Flash sale on gutters!"}, headers=auth_headers(token, org["id"]))
    ops = auth_headers(await _make_operator(client, session, "held-ops@platform.example"))
    held = (await client.get("/api/v1/ops/monitoring/held-texts", headers=ops)).json()
    assert len(held) == 1 and held[0]["body"] == "Flash sale on gutters!"
    r = await client.post(f"/api/v1/ops/monitoring/texts/{held[0]['id']}", json={"decision": "release", "note": "Normal promo"}, headers=ops)
    assert r.status_code == 200, r.text
    from app.services import messaging as messaging_svc

    assert await messaging_svc.release_held_messages(session, carrier, registry=app.state.carriers, settings=ops_settings) == 1
    assert len(carrier.sent) == 1
    label = (await session.execute(sa.select(MonitorLabel))).scalar_one()
    assert label.label == "legit" and label.context["business"]["business_name"]
    # the same text again is allowed straight away (operator verdict cached)
    await client.post("/api/v1/messages", json={"to": "+15125550198", "from": OUR, "body": "Flash sale on gutters!"}, headers=auth_headers(token, org["id"]))
    assert len(carrier.sent) == 2


async def test_operator_queue_case_file_and_unpause(mon, session, ops_settings):
    client, _carrier, fake, _app = mon
    token, org, org_id = await _paused_org(client, session, ops_settings)
    assert await monitor_score.case_file_tick(session, ops_settings) == 1
    ops_token = await _make_operator(client, session, "case-ops@platform.example")
    ops = auth_headers(ops_token)
    queue = (await client.get("/api/v1/ops/monitoring", headers=ops)).json()
    assert queue[0]["org_id"] == org["id"] and queue[0]["case_status"] == "ready"
    case = (await client.get(f"/api/v1/ops/monitoring/orgs/{org['id']}", headers=ops)).json()
    assert case["level"] == "paused" and case["case_file"]["recommendation"] == "keep_paused"
    assert len(case["signals"]) == 4
    r = await client.post(f"/api/v1/ops/monitoring/orgs/{org['id']}/unpause", json={"note": "Verified with the owner"}, headers=ops)
    assert r.status_code == 200, r.text
    assert r.json()["level"] == "normal"
    report = (await client.get("/api/v1/ops/monitoring/report", headers=ops)).json()
    assert "texts" in report and "health" in report


async def test_canary_raises_an_alert_when_a_scam_slips_through(session, ops_settings):
    lenient = FakeSafetyAI()  # says every call is "ok"
    with lenient.installed():
        row = await monitor_exam.canary_tick(session, ops_settings)
    assert row.passed is False and row.detail["misses"] == [{"kind": "call", "id": "canary-call-scam"}]
    alert = (await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "monitor_health"))).scalar_one()
    assert alert.status == "open"

    strict = FakeSafetyAI()
    strict.call_verdict = lambda t: {"verdict": "scam", "confidence": 90, "category": "impersonation", "summary": "x", "evidence": []}
    strict.text_verdict = lambda t: {"verdict": "block" if "Wells Fargo" in t or "sheriff" in t else "allow", "category": "phishing", "confidence": 90, "reason": "x"}
    with strict.installed():
        row = await monitor_exam.canary_tick(session, ops_settings)
    assert row.passed is True
    assert len((await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "monitor_health"))).scalars().all()) == 1


async def test_ai_outage_fails_the_canary(session, ops_settings):
    down = FakeSafetyAI()
    down.fail = True
    with down.installed():
        row = await monitor_exam.canary_tick(session, ops_settings)
    assert row.passed is False and row.detail["unavailable"] > 0
    assert (await session.execute(sa.select(MonitorHealth))).scalar_one().kind == "canary"


def test_exam_library_is_balanced_and_well_formed():
    texts = monitor_exam.load_cases("texts")
    calls = monitor_exam.load_cases("calls")
    assert {c["label"] for c in texts + calls} == {"scam", "legit"}
    assert sum(c["label"] == "scam" for c in texts) >= 10 and sum(c["label"] == "legit" for c in texts) >= 10
    assert all(c.get("body") for c in texts) and all(c.get("transcript") for c in calls)
    assert len({c["id"] for c in texts + calls}) == len(texts + calls)

