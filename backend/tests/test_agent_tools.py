"""P9 machine seams: contact lookup, appointment booking, KB search (route level),
warm handoff, and async AMD. Same worker-auth discipline as P8 (test_agent_seams.py):
JWT signed with the LiveKit secret, org resolved from the Call row, never from anything
the caller asserts about itself.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    Call,
    CallLeg,
    Contact,
    ContactPhone,
    ContactTag,
    Message,
    MessageThread,
    Tag,
)
from tests.conftest import auth_headers, make_org_with_number
from tests.test_agent_seams import _place_call, worker_headers, worker_token
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

OUR = "+12145550100"
THEIRS = "+19725550199"


@pytest.fixture
async def app_with_agent(engine):
    from app.config import Settings
    from app.main import create_app
    from tests.conftest import WEBHOOK_PASS, WEBHOOK_USER

    settings = Settings(
        app_env="test",
        jwt_secret="test-jwt-secret-not-a-real-one-padded-to-32+bytes",
        session_secret="test-session-secret",
        database_url="sqlite+aiosqlite:///:memory:",
        cors_origins="http://localhost:5173",
        sweeper_enabled=False,
        media_store_backend="memory",
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
        livekit_api_key="lk-test-key",
        livekit_api_secret="lk-test-secret-value-padded-to-32-bytes-plus",
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    import httpx

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, application


async def _room_call(session, org_id: uuid.UUID, *, status: str = "answered") -> Call:
    """A live LiveKit room call, built directly on the session (same pattern
    test_voice_plane.py uses for the answer-endpoint tests) - no real LiveKit needed
    since none of these seams call it."""
    set_org_context(session, org_id)
    room = f"call-room-{uuid.uuid4().hex[:8]}"
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status=status,
        extra={"via": "livekit", "room": room},
    )
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id=f"sip-{uuid.uuid4().hex[:8]}",
        to_e164=OUR,
        from_e164=THEIRS,
        status="answered" if status != "queued" else "ringing",
        reason="original",
    )
    session.add(call)
    session.add(leg)
    await session.commit()
    return call


# ==================================================================================
# Contact lookup
# ==================================================================================
async def test_contact_lookup_worker_auth_enforced(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "cl1@example.com", "Org CL1")

    r = await client.get(f"/api/v1/agent/contact/{THEIRS}?call_id={call_id}")
    assert r.status_code == 401


async def test_contact_lookup_unknown_call_404(app_with_agent):
    client, _app = app_with_agent
    r = await client.get(
        f"/api/v1/agent/contact/{THEIRS}?call_id={uuid.uuid4()}",
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 404


async def test_contact_lookup_no_contact_returns_empty_defaults(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "cl2@example.com", "Org CL2")

    r = await client.get(
        f"/api/v1/agent/contact/{THEIRS}?call_id={call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"name": "", "tags": [], "last_messages": []}


async def test_contact_lookup_name_tags_and_last_messages(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "cl3@example.com", "Org CL3", OUR)
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="Jane Doe", attributes={})
    phone = ContactPhone(
        id=uuid.uuid4(), org_id=org_id, contact_id=contact.id, e164=THEIRS, is_primary=True
    )
    tag = Tag(id=uuid.uuid4(), org_id=org_id, name="vip")
    session.add_all([contact, phone, tag])
    await session.flush()
    session.add(ContactTag(id=uuid.uuid4(), org_id=org_id, contact_id=contact.id, tag_id=tag.id))

    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=THEIRS, contact_id=contact.id
    )
    session.add(thread)
    await session.flush()
    # Explicit, strictly increasing created_at: SQLite's default-clock resolution is not
    # fine enough to guarantee distinct timestamps for 7 rows added in one loop, and a
    # tie would make "newest 5" order-dependent on insertion order rather than time.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(7):
        session.add(
            Message(
                id=uuid.uuid4(),
                org_id=org_id,
                thread_id=thread.id,
                direction="outbound" if i % 2 == 0 else "inbound",
                status="delivered" if i % 2 == 0 else "received",
                from_e164=OUR,
                to_e164=THEIRS,
                body=f"msg {i}",
                created_at=base + timedelta(seconds=i),
            )
        )
    await session.commit()

    r = await client.get(
        f"/api/v1/agent/contact/{THEIRS}?call_id={call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Jane Doe"
    assert body["tags"] == ["vip"]
    assert len(body["last_messages"]) == 5
    assert body["last_messages"][0]["body"] == "msg 6"
    assert body["last_messages"][0]["direction"] == "out"
    assert body["last_messages"][1]["direction"] == "in"


async def test_contact_lookup_e164_normalized(app_with_agent, session):
    """A loosely-formatted number (no +, local formatting) still resolves the same
    contact the machine seam's contract says it must."""
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "cl4@example.com", "Org CL4")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="Norm Test", attributes={})
    phone = ContactPhone(
        id=uuid.uuid4(), org_id=org_id, contact_id=contact.id, e164=THEIRS, is_primary=True
    )
    session.add_all([contact, phone])
    await session.commit()

    loose = THEIRS.replace("+1", "")
    r = await client.get(
        f"/api/v1/agent/contact/{loose}?call_id={call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Norm Test"


# ==================================================================================
# Appointment booking
# ==================================================================================
async def test_appointment_booking_worker_auth_enforced(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "ap1@example.com", "Org AP1")

    r = await client.post(
        "/api/v1/agent/appointments",
        json={"call_id": call_id, "contact_e164": THEIRS, "raw_when": "tomorrow at 3", "notes": ""},
    )
    assert r.status_code == 401


async def test_appointment_booking_unknown_call_404(app_with_agent):
    client, _app = app_with_agent
    r = await client.post(
        "/api/v1/agent/appointments",
        json={
            "call_id": str(uuid.uuid4()),
            "contact_e164": THEIRS,
            "raw_when": "tomorrow at 3",
            "notes": "",
        },
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 404


async def test_appointment_booking_parses_iso_and_publishes_after_commit(app_with_agent, session):
    client, application = app_with_agent
    _token, org, call_id = await _place_call(client, "ap2@example.com", "Org AP2")
    org_id = uuid.UUID(org["id"])

    bus = application.state.event_bus
    async with bus.subscribe(org_id) as queue:
        r = await client.post(
            "/api/v1/agent/appointments",
            json={
                "call_id": call_id,
                "contact_e164": THEIRS,
                "raw_when": "2026-09-01T15:00:00Z",
                "notes": "wants a callback",
            },
            headers=worker_headers(worker_token()),
        )
        assert r.status_code == 201, r.text
        event = await asyncio.wait_for(queue.get(), timeout=1)

    body = r.json()
    assert body["raw_when"] == "2026-09-01T15:00:00Z"
    parsed = datetime.fromisoformat(body["scheduled_for"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    assert body["status"] == "booked"

    assert event["type"] == "appointment.booked"
    assert event["appointment_id"] == body["id"]
    assert event["contact_e164"] == THEIRS
    assert event["raw_when"] == "2026-09-01T15:00:00Z"


async def test_appointment_booking_naive_iso_when_keeps_raw_null_parsed(app_with_agent):
    """F6: a naive (no tzinfo) ISO datetime is a plausible-looking parse but an
    un-anchored guess - it must resolve to scheduled_for=None, same as a string that
    does not parse as ISO at all, not silently assumed to be some particular timezone."""
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "ap5@example.com", "Org AP5")

    r = await client.post(
        "/api/v1/agent/appointments",
        json={
            "call_id": call_id,
            "contact_e164": THEIRS,
            "raw_when": "2026-09-01T15:00:00",
            "notes": "",
        },
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["raw_when"] == "2026-09-01T15:00:00"
    assert body["scheduled_for"] is None
    assert body["status"] == "booked"


async def test_appointment_booking_unparseable_when_keeps_raw_null_parsed(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "ap3@example.com", "Org AP3")

    r = await client.post(
        "/api/v1/agent/appointments",
        json={
            "call_id": call_id,
            "contact_e164": THEIRS,
            "raw_when": "sometime next week, afternoon-ish",
            "notes": "",
        },
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["raw_when"] == "sometime next week, afternoon-ish"
    assert body["scheduled_for"] is None
    assert body["status"] == "booked"


# ==================================================================================
# KB search (route level - chunking/scoring internals are tests/test_kb.py's job)
# ==================================================================================
async def test_kb_search_worker_auth_enforced(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "ks1@example.com", "Org KS1")

    r = await client.get(f"/api/v1/agent/kb/search?call_id={call_id}&q=refund")
    assert r.status_code == 401


async def test_kb_search_unknown_call_404(app_with_agent):
    client, _app = app_with_agent
    r = await client.get(
        f"/api/v1/agent/kb/search?call_id={uuid.uuid4()}&q=refund",
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 404


async def test_kb_search_returns_matching_chunks(app_with_agent, session):
    client, _app = app_with_agent
    token, org, call_id = await _place_call(client, "ks2@example.com", "Org KS2")

    r = await client.post(
        "/api/v1/kb/documents",
        json={"title": "Refund policy", "text": "We offer refunds within 30 days of purchase."},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text

    r2 = await client.get(
        f"/api/v1/agent/kb/search?call_id={call_id}&q=refund",
        headers=worker_headers(worker_token()),
    )
    assert r2.status_code == 200, r2.text
    chunks = r2.json()["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["title"] == "Refund policy"
    assert "refund" in chunks[0]["text"].lower()
    assert chunks[0]["score"] > 0


# ==================================================================================
# Warm handoff
# ==================================================================================
async def test_handoff_worker_auth_enforced(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, _ = await make_org_with_number(client, "ho1@example.com", "Org HO1", OUR)
    call = await _room_call(session, uuid.UUID(org["id"]))

    r = await client.post(
        "/api/v1/agent/handoff",
        json={"call_id": str(call.id), "reason": "wants pricing", "summary": "asked about price"},
    )
    assert r.status_code == 401


async def test_handoff_unknown_call_404(app_with_agent):
    client, _app = app_with_agent
    r = await client.post(
        "/api/v1/agent/handoff",
        json={"call_id": str(uuid.uuid4()), "reason": "x", "summary": ""},
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 404


async def test_handoff_publishes_call_handoff_with_room_reason_summary(app_with_agent, session):
    client, application = app_with_agent
    _token, org, _ = await make_org_with_number(client, "ho2@example.com", "Org HO2", OUR)
    org_id = uuid.UUID(org["id"])
    call = await _room_call(session, org_id)

    bus = application.state.event_bus
    async with bus.subscribe(org_id) as queue:
        r = await client.post(
            "/api/v1/agent/handoff",
            json={
                "call_id": str(call.id),
                "reason": "wants pricing",
                "summary": "caller asked about enterprise pricing",
            },
            headers=worker_headers(worker_token()),
        )
        assert r.status_code == 200, r.text
        event = await asyncio.wait_for(queue.get(), timeout=1)

    assert r.json() == {"published": True}
    assert event == {
        "type": "call.handoff",
        "call_id": str(call.id),
        "room": call.extra["room"],
        "reason": "wants pricing",
        "summary": "caller asked about enterprise pricing",
        "contact": THEIRS,
    }


async def test_handoff_terminal_call_is_409(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, _ = await make_org_with_number(client, "ho3@example.com", "Org HO3", OUR)
    call = await _room_call(session, uuid.UUID(org["id"]), status="completed")

    r = await client.post(
        "/api/v1/agent/handoff",
        json={"call_id": str(call.id), "reason": "x", "summary": ""},
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 409


async def test_handoff_non_room_call_is_409(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "ho4@example.com", "Org HO4")

    r = await client.post(
        "/api/v1/agent/handoff",
        json={"call_id": call_id, "reason": "x", "summary": ""},
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 409


# ==================================================================================
# AMD verdict
# ==================================================================================
async def test_amd_worker_auth_enforced(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, _ = await make_org_with_number(client, "amd1@example.com", "Org AMD1", OUR)
    call = await _room_call(session, uuid.UUID(org["id"]))

    r = await client.post("/api/v1/agent/amd", json={"call_id": str(call.id), "result": "machine"})
    assert r.status_code == 401


async def test_amd_invalid_result_is_422(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, _ = await make_org_with_number(client, "amd2@example.com", "Org AMD2", OUR)
    call = await _room_call(session, uuid.UUID(org["id"]))

    r = await client.post(
        "/api/v1/agent/amd",
        json={"call_id": str(call.id), "result": "robot"},
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 422


async def test_amd_sets_active_leg_result_once(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, _ = await make_org_with_number(client, "amd3@example.com", "Org AMD3", OUR)
    call = await _room_call(session, uuid.UUID(org["id"]))

    r1 = await client.post(
        "/api/v1/agent/amd",
        json={"call_id": str(call.id), "result": "machine"},
        headers=worker_headers(worker_token()),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"updated": True}

    leg = (
        await session.execute(
            sa.select(CallLeg)
            .where(CallLeg.call_id == call.id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().first()
    assert leg.amd_result == "machine"


async def test_amd_second_write_ignored_monotonic(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, _ = await make_org_with_number(client, "amd4@example.com", "Org AMD4", OUR)
    call = await _room_call(session, uuid.UUID(org["id"]))

    r1 = await client.post(
        "/api/v1/agent/amd",
        json={"call_id": str(call.id), "result": "human"},
        headers=worker_headers(worker_token()),
    )
    assert r1.json() == {"updated": True}

    r2 = await client.post(
        "/api/v1/agent/amd",
        json={"call_id": str(call.id), "result": "machine"},
        headers=worker_headers(worker_token()),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"updated": False}

    leg = (
        await session.execute(
            sa.select(CallLeg)
            .where(CallLeg.call_id == call.id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().first()
    assert leg.amd_result == "human"
