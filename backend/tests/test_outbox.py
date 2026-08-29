"""P13 DR-4: the durable platform-event outbox (services/outbox.py + the Tier-1 hooks).

Covers the outbox mechanics directly (same-transaction commit/rollback, the
call.completed before_flush listener firing exactly once) and one full webhook-driven
hook (message.received) to prove a real ingest writes the outbox row atomically with
the message. The remaining hooks (message.finalized, appointment.booked,
campaign.completed, voicemail.created) are exercised through their own phases' flows;
this file pins the MECHANISM they all share.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Org, PlatformEvent
from app.models.voice import Call
from app.services import calls as calls_svc
from app.services.outbox import record_platform_event
from tests.conftest import fixture_bytes, make_org_with_number, webhook_auth_headers

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"


async def _events(session, org_id=None):
    stmt = sa.select(PlatformEvent).execution_options(**{ALLOW_UNSCOPED_KEY: True})
    rows = list((await session.execute(stmt)).scalars().all())
    if org_id is not None:
        rows = [r for r in rows if r.org_id == org_id]
    return rows


async def _make_org(session) -> uuid.UUID:
    org = Org(id=uuid.uuid4(), name="Outbox Org", slug=f"outbox-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    return org.id


async def test_unknown_event_type_is_rejected(session):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    try:
        record_platform_event(session, org_id, "not.a.real.type", {})
    except ValueError as exc:
        assert "not.a.real.type" in str(exc)
    else:
        raise AssertionError("unknown event type must raise")


async def test_event_commits_and_rolls_back_with_the_transaction(session):
    org_id = await _make_org(session)
    set_org_context(session, org_id)

    record_platform_event(session, org_id, "message.received", {"probe": "rollback"})
    await session.rollback()
    set_org_context(session, org_id)
    assert await _events(session, org_id) == []

    record_platform_event(session, org_id, "message.received", {"probe": "commit"})
    await session.commit()
    events = await _events(session, org_id)
    assert len(events) == 1
    assert events[0].payload["probe"] == "commit"


async def test_call_completed_listener_fires_once_at_terminal(session):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164="+19725550101",
        our_e164=OUR,
        carrier="bandwidth",
        status="queued",
    )
    session.add(call)
    await session.commit()

    calls_svc._advance_call(call, "initiated")
    await session.commit()
    assert [e for e in await _events(session, org_id) if e.event_type == "call.completed"] == []

    calls_svc._advance_call(call, "completed")
    await session.commit()
    completed = [
        e for e in await _events(session, org_id) if e.event_type == "call.completed"
    ]
    assert len(completed) == 1
    assert completed[0].payload["call_id"] == str(call.id)

    # Terminal is immutable; touching the row again must not re-emit.
    call.tag = "poke"
    await session.commit()
    completed = [
        e for e in await _events(session, org_id) if e.event_type == "call.completed"
    ]
    assert len(completed) == 1


async def test_inbound_webhook_writes_message_received_event(app_with_carrier, session):
    client, _, _ = app_with_carrier
    _token, org, _ = await make_org_with_number(client, "outbox@example.com", "Org O", OUR)

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200

    events = [
        e
        for e in await _events(session)
        if e.event_type == "message.received" and str(e.org_id) == org["id"]
    ]
    assert len(events) == 1
    assert events[0].payload["to"] == OUR

    # Redelivery dedupes the message; it must not duplicate the outbox row either.
    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200
    events = [
        e
        for e in await _events(session)
        if e.event_type == "message.received" and str(e.org_id) == org["id"]
    ]
    assert len(events) == 1
