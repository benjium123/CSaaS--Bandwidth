# ruff: noqa: E501, F811
"""Regression tests for the second P43 bug hunt (findings on the new monitoring and
verification code). One test (or small group) per finding."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    Call,
    CallReview,
    KycDocument,
    Message,
    MessageThread,
    MonitorSignal,
    Org,
    OrgMonitoring,
    TextVerdict,
)
from app.services import monitor_calls, monitor_score, monitor_text
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    FakeCarrier,
    _install,
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)
from tests.fake_ai import FakeSafetyAI
from tests.test_p41_kyc import (  # noqa: F401 - fixtures
    _complete_application,
    _pdf_bytes,
    kyc_app,
    kyc_settings,
)

OUR = "+15125550100"
THEM = "+15125550199"


@pytest.fixture
def fix_settings():
    return make_settings(
        monitor_enforced=True,
        public_web_url="https://console.example.test",
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
    )


@pytest.fixture
async def app_ai(engine, fix_settings):
    application = create_app(fix_settings)
    carrier = FakeCarrier()
    _install(application, carrier)
    fake = FakeSafetyAI()
    with fake.installed():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            yield client, carrier, fake, application


async def _org(session, *, age_days: int = 0) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Fix", slug=f"fix-{org_id.hex[:8]}", created_at=datetime.now(timezone.utc) - timedelta(days=age_days)))
    await session.commit()
    return org_id


# --- 1. link tracking hid the real link from the check ------------------------------------


async def test_tracked_links_are_screened_before_they_are_replaced(app_ai, session):
    client, carrier, fake, _app = app_ai
    token, org, _num = await make_org_with_number(client, f"l-{uuid.uuid4().hex[:6]}@example.com", "Links", OUR)
    fake.text_verdict = lambda text: {
        "verdict": "block" if "paypa1-login.top" in text else "allow",
        "category": "phishing", "confidence": 95, "reason": "Look-alike login page",
    }
    r = await client.post(
        "/api/v1/messages",
        json={"to": THEM, "from": OUR, "body": "Your account needs attention: https://paypa1-login.top/verify", "track_links": True},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "rejected"
    assert carrier.sent == []
    assert "paypa1-login.top" in fake.tasks("outbound text message")[0]["messages"][1]["content"]


# --- 2. outsiders alone can't pause an account ----------------------------------------------


async def test_public_reports_and_replies_alone_never_pause(session, fix_settings):
    org_id = await _org(session)
    for _ in range(6):
        await monitor_score.add_signal(session, fix_settings, org_id, "public_report", "Report")
    await session.commit()
    set_org_context(session, org_id)
    state = (await session.execute(sa.select(OrgMonitoring))).scalar_one()
    assert state.score >= fix_settings.monitor_pause_score
    assert state.level == "restricted"
    # platform-observed evidence on top does pause
    await monitor_score.add_signal(session, fix_settings, org_id, "call_scam", "Scam call")
    await session.commit()
    await session.refresh(state)
    assert state.level == "paused"


async def test_repeat_reports_from_one_sender_count_once(app_ai, session, fix_settings):
    from app.models import NumberReport

    client, _carrier, _fake, _app = app_ai
    for _ in range(4):
        r = await client.post(
            "/api/v1/public/report-number",
            json={"number": OUR, "kind": "call", "description": "They demanded gift cards as the IRS."},
        )
        assert r.status_code == 202
    assert len((await session.execute(sa.select(NumberReport))).scalars().all()) == 1


# --- 3. behaviour signals for two workspaces in one pass --------------------------------------


async def test_behaviour_tick_handles_several_workspaces(session, fix_settings):
    orgs = [await _org(session), await _org(session)]
    for org_id in orgs:
        set_org_context(session, org_id)
        thread = MessageThread(id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=THEM)
        session.add(thread)
        await session.flush()
        session.add(Message(id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound", status="received", from_e164=THEM, to_e164=OUR, body="This is a scam, stop texting me"))
        await session.commit()
    counts = await monitor_calls.behaviour_tick(session, fix_settings)
    assert counts["signals"] == 2
    kinds = (await session.execute(sa.select(MonitorSignal.org_id).execution_options(allow_unscoped=True))).scalars().all()
    assert sorted(kinds) == sorted(orgs)


# --- 4. two sends of a new text at once ----------------------------------------------------------


async def test_verdict_write_survives_a_concurrent_insert(session, fix_settings, monkeypatch, engine):
    from app.db.session import get_sessionmaker

    org_id = await _org(session)
    digest = monitor_text.body_hash("Your order is ready")
    # another worker already wrote this verdict, but our session looked before it did
    async with get_sessionmaker()() as other:
        set_org_context(other, org_id)
        other.add(TextVerdict(id=uuid.uuid4(), org_id=org_id, body_hash=digest, verdict="allow", source="ai", expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
        await other.commit()
    real_execute = session.execute
    calls = {"n": 0}

    async def first_lookup_misses(stmt, *args, **kwargs):
        result = await real_execute(stmt, *args, **kwargs)
        if "text_verdicts" in str(stmt) and calls["n"] == 0:
            calls["n"] += 1

            class Empty:
                def scalar_one_or_none(self):
                    return None

            return Empty()
        return result

    monkeypatch.setattr(session, "execute", first_lookup_misses)
    await monitor_text._store(session, org_id, digest, monitor_text.Screening(action="hold", reason="x", source="ai"))
    monkeypatch.undo()
    await session.commit()
    set_org_context(session, org_id)
    rows = (await session.execute(sa.select(TextVerdict))).scalars().all()
    assert len(rows) == 1 and rows[0].verdict == "hold"


# --- 5. step-up rotates the cookie: the events socket must stay open -----------------------------


async def test_events_socket_survives_session_rotation_but_not_revocation(engine, session, monkeypatch):
    from app.api.routes import softphone
    from app.events.bus import EventBus
    from app.models import Session as IdentitySession
    from tests.test_events_ws import FakeWebSocket, _FakeApp

    monkeypatch.setattr(softphone, "ACCESS_TTL_SECONDS", 0.2)
    monkeypatch.setattr(softphone, "PING_INTERVAL_SECONDS", 0.1)
    settings = make_settings(auth_bearer_compat=False, session_cookie_secure=False)
    application = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as browser:
        email = f"ws-{uuid.uuid4().hex[:6]}@example.com"
        password = "correct-horse-battery-staple"
        await browser.post("/api/v1/auth/register", json={"email": email, "password": password})
        await browser.post("/api/v1/auth/login", json={"email": email, "password": password})
        csrf = {"X-CSRF-Token": browser.cookies.get("csaas_csrf", "")}
        org = (await browser.post("/api/v1/orgs", json={"name": "WS Org"}, headers=csrf)).json()
        cookie = browser.cookies.get("csaas_session")
    ws = FakeWebSocket(_FakeApp(settings, EventBus()), token=None, org_id=org["id"])
    ws.cookies = {"csaas_session": cookie}
    ws.headers = {"origin": "http://localhost:5173"}
    task = asyncio.ensure_future(softphone.events_ws(ws))
    try:
        for _ in range(100):
            if ws.accepted:
                break
            await asyncio.sleep(0.01)
        assert ws.accepted and ws.closed_code is None
        row = (await session.execute(sa.select(IdentitySession))).scalar_one()
        row.token_hash = "0" * 64  # what a step-up rotation does to the stored secret
        await session.commit()
        await asyncio.sleep(1.0)
        assert ws.closed_code is None, "a rotated session must not close the socket"
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()
        for _ in range(200):
            if ws.closed_code is not None:
                break
            await asyncio.sleep(0.05)
        assert ws.closed_code == 4401
    finally:
        ws.disconnect()
        await asyncio.wait_for(task, timeout=3)


# --- 7. an AI outage doesn't stall the sweeper ------------------------------------------------------


async def test_second_look_stops_calling_the_ai_once_it_is_down(app_ai, session, fix_settings):
    client, _carrier, fake, _app = app_ai
    token, org, _num = await make_org_with_number(client, f"d-{uuid.uuid4().hex[:6]}@example.com", "Down", OUR)
    fake.text_verdict = lambda t: {"verdict": "hold", "category": "other", "confidence": 50, "reason": "x"}
    for i in range(3):
        await client.post("/api/v1/messages", json={"to": THEM, "from": OUR, "body": f"Special offer number {'abc'[i]} today"}, headers=auth_headers(token, org["id"]))
    before = len(fake.requests)
    fake.fail = True
    counts = await monitor_text.second_look_tick(session, fix_settings)
    assert counts["waiting"] == 3
    assert len(fake.requests) - before == 2  # one message, one retry - then it stops


# --- 8. call reviews never stall on unfinished calls --------------------------------------------------


async def test_review_of_a_call_that_never_finished_is_given_up(session, fix_settings):
    org_id = await _org(session)
    set_org_context(session, org_id)
    call = Call(id=uuid.uuid4(), org_id=org_id, direction="outbound", contact_e164=THEM, our_e164=OUR, carrier="bandwidth", status="ringing", created_at=datetime.now(timezone.utc) - timedelta(hours=8))
    session.add(call)
    await session.flush()
    session.add(CallReview(id=uuid.uuid4(), org_id=org_id, call_id=call.id, reason="sample"))
    await session.commit()
    assert await monitor_calls.review_tick(session, fix_settings, store=None) == {"skipped": 1}


# --- 9. editing details makes the AI read the documents again -------------------------------------


async def test_changed_details_reset_document_reviews(kyc_app, session):
    client, _app, _carrier, created, outcomes = kyc_app
    token = await register_and_login(client, "edit@acme-plumbing.example")
    org = await create_org(client, token, "Edit Co")
    h = auth_headers(token, org["id"])
    person_id = await _complete_application(client, created, outcomes, token, org["id"])
    set_org_context(session, uuid.UUID(org["id"]))
    docs = (await session.execute(sa.select(KycDocument))).scalars().all()
    assert all(d.review_result == "pass" for d in docs)

    kyc_app[1].state.fake_ai.fail = True  # keep the new reviews pending so we can see the reset
    r = await client.put(f"/api/v1/kyc/persons/{person_id}/address", json={"line1": "99 New Rd", "city": "Austin", "postal_code": "78703", "country": "US"}, headers=h)
    assert r.status_code == 200, r.text
    await session.commit()
    set_org_context(session, uuid.UUID(org["id"]))
    proof = (await session.execute(sa.select(KycDocument).where(KycDocument.kind == "proof_of_address").execution_options(populate_existing=True))).scalar_one()
    assert proof.review_result in (None, "error")

    r = await client.put("/api/v1/kyc/profile/business", json={"legal_name": "Different Name LLC"}, headers=h)
    assert r.status_code == 200, r.text
    await session.commit()
    set_org_context(session, uuid.UUID(org["id"]))
    cert = (await session.execute(sa.select(KycDocument).where(KycDocument.kind == "registration_certificate").execution_options(populate_existing=True))).scalar_one()
    assert cert.review_result in (None, "error")


# --- 10. one broken document doesn't block the application -------------------------------------------


async def test_one_broken_document_does_not_stop_the_others(kyc_app, session, kyc_settings, monkeypatch):
    from app.services import kyc_automation, kyc_doc_reader

    client, app, _carrier, created, outcomes = kyc_app
    app.state.fake_ai.fail = True
    token = await register_and_login(client, "broken@acme-plumbing.example")
    org = await create_org(client, token, "Broken Co")
    await _complete_application(client, created, outcomes, token, org["id"])
    app.state.fake_ai.fail = False
    real = kyc_doc_reader.review_document
    seen = {"n": 0}

    async def first_one_explodes(session_, settings_, store, profile, document, persons):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ValueError("corrupt file")
        return await real(session_, settings_, store, profile, document, persons)

    monkeypatch.setattr(kyc_doc_reader, "review_document", first_one_explodes)
    await kyc_automation.process(session, kyc_settings, app.state.media_store, uuid.UUID(org["id"]), reviews_only=True, force_reviews=True)
    set_org_context(session, uuid.UUID(org["id"]))
    results = sorted(d.review_result for d in (await session.execute(sa.select(KycDocument).execution_options(populate_existing=True))).scalars().all())
    assert results == ["error", "pass"]


# --- 11. edited auto-reply texts are screened ------------------------------------------------------------


async def test_only_the_standard_stop_reply_skips_the_ai(app_ai, session):
    from app.compliance import service as compliance_svc
    from app.models.compliance import ComplianceSettings

    client, _carrier, _fake, _app = app_ai
    _token, org, _num = await make_org_with_number(client, f"s-{uuid.uuid4().hex[:6]}@example.com", "Stop Co", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    standard = await compliance_svc.auto_reply_body(session, org_id, compliance_svc.KeywordHit("opt_out", "STOP"))
    assert await compliance_svc.is_standard_auto_reply(session, org_id, standard) is True
    settings_row = (await session.execute(sa.select(ComplianceSettings))).scalar_one()
    settings_row.optout_text = "Unsubscribed. Claim your refund at refund-now.top/claim"
    await session.commit()
    edited = await compliance_svc.auto_reply_body(session, org_id, compliance_svc.KeywordHit("opt_out", "STOP"))
    assert await compliance_svc.is_standard_auto_reply(session, org_id, edited) is False


# --- 12. a clean verdict never carries over to a different link or number ---------------------------------


def test_cache_key_keeps_links_and_phone_numbers():
    h = monitor_text.body_hash
    assert h("Your code is 123456") == h("Your code is 654321")
    assert h("Call us at 212-555-0100") != h("Call us at 212-555-0199")
    assert h("Order at shop1.com/deals") != h("Order at shop9.com/deals")


# --- 13. proof of address isn't a business document ---------------------------------------------------------


async def test_proof_of_address_does_not_count_as_the_business_document(kyc_app, session):
    client, _app, _carrier, _created, _outcomes = kyc_app
    token = await register_and_login(client, "proof@acme-plumbing.example")
    org = await create_org(client, token, "Proof Co")
    h = auth_headers(token, org["id"])
    person = (await client.post("/api/v1/kyc/persons", json={"role": "owner", "full_name": "Pat Lee", "ownership_percent": 100, "residential_address": {"line1": "1 Elm", "city": "Austin", "postal_code": "78701", "country": "US"}}, headers=h)).json()
    r = await client.post("/api/v1/kyc/documents", data={"kind": "proof_of_address", "person_id": person["id"]}, files={"file": ("bill.pdf", _pdf_bytes(), "application/pdf")}, headers=h)
    assert r.status_code == 201
    missing = (await client.get("/api/v1/kyc/profile", headers=h)).json()["missing"]
    assert "documents" in missing and "proof_of_address" not in missing


async def test_a_phone_number_in_the_workspace_name_loses_the_exemption(app_ai, session):
    from app.compliance import service as compliance_svc

    client, _carrier, _fake, _app = app_ai
    _token, org, _num = await make_org_with_number(client, f"n-{uuid.uuid4().hex[:6]}@example.com", "3M Plumbing", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    body = await compliance_svc.auto_reply_body(session, org_id, compliance_svc.KeywordHit("opt_out", "STOP"))
    assert await compliance_svc.is_standard_auto_reply(session, org_id, body) is True
    row = await session.get(Org, org_id)
    row.name = "Refunds call 800-555-0100"
    await session.commit()
    set_org_context(session, org_id)
    body = await compliance_svc.auto_reply_body(session, org_id, compliance_svc.KeywordHit("opt_out", "STOP"))
    assert await compliance_svc.is_standard_auto_reply(session, org_id, body) is False
