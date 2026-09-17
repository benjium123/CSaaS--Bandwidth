# ruff: noqa: E501
"""P43 AI text guard: every outbound text is screened before a carrier sees it."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import Message, MonitorSignal, Org, OrgMonitoring, TextVerdict
from app.services import messaging as messaging_svc
from app.services import monitor_score, monitor_text
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

TO = "+15125550199"


@pytest.fixture
def guard_settings():
    return make_settings(
        monitor_enforced=True,
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
    )


@pytest.fixture
async def guard(engine, guard_settings):
    application = create_app(guard_settings)
    carrier = FakeCarrier()
    _install(application, carrier)
    fake = FakeSafetyAI()
    with fake.installed():
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, carrier, fake, application


async def _org(client, number: str = "+15125550100"):
    token, org, _num = await make_org_with_number(
        client, f"g-{uuid.uuid4().hex[:8]}@example.com", f"Guard {uuid.uuid4().hex[:6]}", number
    )
    return token, org


async def _send(client, token, org, body, from_=None):
    return await client.post(
        "/api/v1/messages",
        json={"to": TO, "from": from_ or "+15125550100", "body": body},
        headers=auth_headers(token, org["id"]),
    )


async def _message(session, org_id) -> Message:
    set_org_context(session, uuid.UUID(org_id))
    return (
        await session.execute(sa.select(Message).order_by(Message.created_at.desc()).limit(1))
    ).scalar_one()


async def test_normal_text_is_sent_and_cached_per_body(guard, session):
    client, carrier, fake, _app = guard
    token, org = await _org(client)
    r = await _send(client, token, org, "Hi, your plumber arrives at 3pm. Reply C to confirm.")
    assert r.status_code == 201, r.text
    assert len(carrier.sent) == 1
    msg = await _message(session, org["id"])
    assert msg.moderation_state == "allowed"
    assert len(fake.tasks("outbound text message")) == 1

    # Same message with different numbers in it: one verdict covers both.
    r = await _send(client, token, org, "Hi, your plumber arrives at 5pm. Reply C to confirm.")
    assert r.status_code == 201
    assert len(carrier.sent) == 2
    assert len(fake.tasks("outbound text message")) == 1


async def test_obvious_scam_is_blocked_by_rules_without_ai(guard, session, guard_settings):
    client, carrier, fake, _app = guard
    token, org = await _org(client)
    r = await _send(
        client, token, org, "IRS FINAL NOTICE: a warrant is issued for your arrest. Pay with gift cards today."
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert carrier.sent == []
    assert fake.tasks("outbound text message") == []
    msg = await _message(session, org["id"])
    assert msg.moderation_state == "blocked" and msg.error_code == "blocked_scam"
    signals = (await session.execute(sa.select(MonitorSignal))).scalars().all()
    assert [s.kind for s in signals] == ["text_blocked"]


async def test_ai_hold_then_second_look_clears_and_sends(guard, session, guard_settings):
    client, carrier, fake, app = guard
    token, org = await _org(client)
    fake.text_verdict = lambda text: {
        "verdict": "hold",
        "category": "off_business",
        "confidence": 55,
        "reason": "Unusual for a plumber",
    }
    r = await _send(client, token, org, "Special offer on water heaters this week only!")
    assert r.status_code == 201, r.text
    assert carrier.sent == []
    msg = await _message(session, org["id"])
    assert msg.status == "queued" and msg.moderation_state == "held"
    assert "safety review" in msg.failure_reason_public

    # The stale-queued crash recovery must leave a held text alone.
    set_org_context(session, uuid.UUID(org["id"]))
    msg.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    await messaging_svc.recover_stale_queued(session, registry=app.state.carriers, settings=guard_settings)
    assert carrier.sent == []

    fake.text_verdict = lambda text: {"verdict": "allow", "category": "none", "confidence": 80, "reason": "Fine"}
    counts = await monitor_text.second_look_tick(session, guard_settings)
    assert counts["cleared"] == 1
    released = await messaging_svc.release_held_messages(
        session, carrier, registry=app.state.carriers, settings=guard_settings
    )
    assert released == 1 and len(carrier.sent) == 1
    msg = await _message(session, org["id"])
    assert msg.status == "accepted" and msg.moderation_state == "cleared"


async def test_second_look_confirms_a_held_scam(guard, session, guard_settings):
    client, carrier, fake, _app = guard
    token, org = await _org(client)
    fake.text_verdict = lambda text: {"verdict": "hold", "category": "phishing", "confidence": 70, "reason": "Asks to verify"}
    await _send(client, token, org, "Please verify your account details at acme-login.example/verify")
    counts = await monitor_text.second_look_tick(session, guard_settings)
    assert counts["confirmed"] == 1
    msg = await _message(session, org["id"])
    assert msg.status == "rejected" and msg.moderation_state == "blocked"
    assert carrier.sent == []
    kinds = [s.kind for s in (await session.execute(sa.select(MonitorSignal))).scalars().all()]
    assert kinds == ["text_held_confirmed"]


async def test_ai_outage_holds_new_accounts_but_not_established_ones(guard, session, guard_settings):
    client, carrier, fake, _app = guard
    fake.fail = True
    token, org = await _org(client)
    r = await _send(client, token, org, "Your order is ready for pickup.")
    assert r.status_code == 201
    assert carrier.sent == []
    assert (await _message(session, org["id"])).moderation_state == "held"

    token2, org2 = await _org(client, "+15125550101")
    org_row = await session.get(Org, uuid.UUID(org2["id"]))
    org_row.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    await session.commit()
    r = await _send(client, token2, org2, "Your order is ready for pickup today.", from_="+15125550101")
    assert r.status_code == 201
    assert len(carrier.sent) == 1
    set_org_context(session, uuid.UUID(org2["id"]))
    verdict = (await session.execute(sa.select(TextVerdict))).scalar_one()
    assert verdict.source == "unchecked"

    # The AI comes back and finds the text was harmful: a signal is recorded.
    fake.fail = False
    fake.text_verdict = lambda text: {"verdict": "block", "category": "phishing", "confidence": 90, "reason": "Phishing"}
    assert await monitor_text.unchecked_tick(session, guard_settings) == 1
    kinds = [s.kind for s in (await session.execute(sa.select(MonitorSignal))).scalars().all()]
    assert "text_unchecked_flagged" in kinds


async def test_compliance_auto_replies_are_never_screened(guard, session, guard_settings):
    client, carrier, fake, _app = guard
    token, org = await _org(client)
    set_org_context(session, uuid.UUID(org["id"]))
    message = await messaging_svc.send_message(
        session,
        uuid.UUID(org["id"]),
        carrier,
        to_e164=TO,
        from_e164="+15125550100",
        body="You are unsubscribed. Reply START to resubscribe.",
        exemption="auto_reply",
    )
    assert message.status == "accepted"
    assert message.moderation_state == "exempt"
    assert fake.tasks("outbound text message") == []


async def test_enough_signals_pause_the_account_and_write_a_case_file(guard, session, guard_settings):
    client, carrier, fake, _app = guard
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    for _ in range(4):
        await monitor_score.add_signal(session, guard_settings, org_id, "text_blocked", "Blocked text: gift cards")
    await session.commit()
    state = (await session.execute(sa.select(OrgMonitoring))).scalar_one()
    assert state.level == "paused" and state.score >= guard_settings.monitor_pause_score

    r = await _send(client, token, org, "Hello there")
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_paused"

    fake.generic = {
        "summary": "Repeated gift card scam texts.",
        "what_they_claim": "Plumbing reminders",
        "what_we_saw": ["4 blocked texts"],
        "evidence_quotes": [{"source": "text", "quote": "Pay with gift cards"}],
        "false_alarm_signs": [],
        "recommendation": "suspend_and_ban",
        "confidence": 90,
    }
    assert await monitor_score.case_file_tick(session, guard_settings) == 1
    set_org_context(session, org_id)
    await session.refresh(state)
    assert state.case_file["status"] == "ready"
    assert state.case_file["recommendation"] == "suspend_and_ban"

    # Signals keep coming in, but only an operator ends a pause.
    await monitor_score.unpause(session, state, operator_id=None, note="False alarm")
    await session.commit()
    r = await _send(client, token, org, "Hello again, your appointment is at 2pm")
    assert r.status_code == 201, r.text


def test_normalisation_ignores_numbers_and_spacing():
    assert monitor_text.body_hash("Code 123456  for Acme") == monitor_text.body_hash("code 999999 for acme")
    assert monitor_text.body_hash("Pay now") != monitor_text.body_hash("Pay later")
