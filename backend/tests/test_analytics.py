"""P13 DR-7/DR-10: analytics overview series shapes, and transcript search - the LIKE
path (portable, SQLite) plus a pg_only test proving the tsvector branch."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.db.base import set_org_context
from app.models import (
    AgentSmsTurn,
    Call,
    ContactList,
    Message,
    OutboundCampaign,
)
from app.models.agent import CallTranscriptSegment
from app.services import analytics as analytics_svc
from app.services import messaging as messaging_svc
from app.services import search as search_svc
from tests.conftest import auth_headers, create_org, register_and_login

OUR = "+12145550100"
CONTACT = "+19725550101"
DAY0 = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
DAY1 = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


async def _org(client, email: str, name: str):
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, org


async def _thread_id(session, org_id: uuid.UUID) -> uuid.UUID:
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, CONTACT)
    await session.commit()
    return thread.id


# --------------------------------------------------------------------------------------
# Analytics overview
# --------------------------------------------------------------------------------------
async def test_overview_messages_series_shape_and_delivery_rate(client, session):
    token, org = await _org(client, "an1@example.com", "Org AN1")
    org_id = uuid.UUID(org["id"])
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    session.add_all(
        [
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="delivered", from_e164=OUR, to_e164=CONTACT, body="a", created_at=DAY1,
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
                status="failed", from_e164=OUR, to_e164=CONTACT, body="b", created_at=DAY1,
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="inbound",
                status="received", from_e164=CONTACT, to_e164=OUR, body="c", created_at=DAY1,
            ),
        ]
    )
    await session.commit()

    overview = await analytics_svc.overview(session, org_id, 7, now=DAY1)
    day = next(d for d in overview["messages"] if d["date"] == "2026-06-15")
    assert day["outbound"] == 2
    assert day["inbound"] == 1
    assert day["delivery_rate"] == 0.5  # 1 delivered of 2 terminal outbound


async def test_overview_calls_series_avg_duration(client, session):
    token, org = await _org(client, "an2@example.com", "Org AN2")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    session.add_all(
        [
            Call(
                id=uuid.uuid4(), org_id=org_id, direction="outbound", contact_e164=CONTACT,
                our_e164=OUR, carrier="bandwidth", status="completed", duration_seconds=60,
                created_at=DAY1,
            ),
            Call(
                id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=CONTACT,
                our_e164=OUR, carrier="bandwidth", status="completed", duration_seconds=120,
                created_at=DAY1,
            ),
        ]
    )
    await session.commit()

    overview = await analytics_svc.overview(session, org_id, 7, now=DAY1)
    day = next(d for d in overview["calls"] if d["date"] == "2026-06-15")
    assert day["calls"] == 2
    assert day["avg_duration_seconds"] == 90.0


async def test_overview_campaign_progress_snapshot(client, session):
    token, org = await _org(client, "an3@example.com", "Org AN3")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv", status="ready"
    )
    session.add(lst)
    await session.flush()
    session.add_all(
        [
            OutboundCampaign(
                id=uuid.uuid4(), org_id=org_id, name="C1", channel="sms", list_id=lst.id,
                status="running",
            ),
            OutboundCampaign(
                id=uuid.uuid4(), org_id=org_id, name="C2", channel="sms", list_id=lst.id,
                status="completed",
            ),
        ]
    )
    await session.commit()

    overview = await analytics_svc.overview(session, org_id, 7, now=DAY1)
    counts = {row["status"]: row["count"] for row in overview["campaigns"]}
    assert counts == {"running": 1, "completed": 1}


async def test_overview_ai_series_turns_and_handoffs(client, session):
    token, org = await _org(client, "an4@example.com", "Org AN4")
    org_id = uuid.UUID(org["id"])
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    inbound_ids = []
    for i in range(2):
        msg = Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="inbound",
            status="received", from_e164=CONTACT, to_e164=OUR, body=f"m{i}", created_at=DAY1,
        )
        session.add(msg)
        await session.flush()
        inbound_ids.append(msg.id)
    session.add_all(
        [
            AgentSmsTurn(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id,
                inbound_message_id=inbound_ids[0], status="replied", detail="",
                created_at=DAY1,
            ),
            AgentSmsTurn(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id,
                inbound_message_id=inbound_ids[1], status="handoff", detail="keyword:human",
                created_at=DAY1,
            ),
        ]
    )
    await session.commit()

    overview = await analytics_svc.overview(session, org_id, 7, now=DAY1)
    day = next(d for d in overview["ai"] if d["date"] == "2026-06-15")
    assert day["turns"] == 2
    assert day["handoffs"] == 1


async def test_overview_route_requires_reports_read_and_is_org_scoped(client, session):
    token, org = await _org(client, "an5@example.com", "Org AN5")
    h = auth_headers(token, org["id"])
    r = await client.get("/api/v1/analytics/overview", params={"days": 7}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "messages" in body and "calls" in body and "campaigns" in body and "ai" in body


# --------------------------------------------------------------------------------------
# Transcript search
# --------------------------------------------------------------------------------------
async def _call_with_segments(session, org_id: uuid.UUID, *, texts: list[str]) -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="outbound", contact_e164=CONTACT,
        our_e164=OUR, carrier="bandwidth", status="completed",
    )
    session.add(call)
    await session.flush()
    for i, text in enumerate(texts):
        session.add(
            CallTranscriptSegment(
                id=uuid.uuid4(), org_id=org_id, call_id=call.id,
                role="user" if i % 2 == 0 else "agent", text=text, at_ms=i * 1000,
            )
        )
    await session.commit()
    return call


async def test_search_like_path_finds_matching_transcript_text(client, session):
    token, org = await _org(client, "se1@example.com", "Org SE1")
    org_id = uuid.UUID(org["id"])
    matching = await _call_with_segments(
        session, org_id, texts=["Hi, I'm interested in the property on Elm Street", "Great!"]
    )
    await _call_with_segments(session, org_id, texts=["Unrelated chat about weather"])

    set_org_context(session, org_id)
    results = await search_svc.search_transcripts(session, org_id, "elm street")
    assert len(results) == 1
    assert results[0]["call_id"] == str(matching.id)
    assert any(seg["matched"] for seg in results[0]["segments"])


async def test_search_is_case_insensitive_and_org_scoped(client, session):
    token_a, org_a = await _org(client, "se2a@example.com", "Org SE2A")
    token_b, org_b = await _org(client, "se2b@example.com", "Org SE2B")
    org_id_a = uuid.UUID(org_a["id"])
    org_id_b = uuid.UUID(org_b["id"])
    await _call_with_segments(session, org_id_a, texts=["Special DISCOUNT offer"])
    await _call_with_segments(session, org_id_b, texts=["Special DISCOUNT offer"])

    set_org_context(session, org_id_a)
    results_a = await search_svc.search_transcripts(session, org_id_a, "discount")
    assert len(results_a) == 1

    set_org_context(session, org_id_b)
    results_b = await search_svc.search_transcripts(session, org_id_b, "totally-absent-term")
    assert results_b == []


async def test_search_empty_query_returns_no_results(client, session):
    token, org = await _org(client, "se3@example.com", "Org SE3")
    org_id = uuid.UUID(org["id"])
    await _call_with_segments(session, org_id, texts=["hello"])
    set_org_context(session, org_id)
    assert await search_svc.search_transcripts(session, org_id, "   ") == []


async def test_search_route_requires_reports_read(client, session):
    token, org = await _org(client, "se4@example.com", "Org SE4")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    await _call_with_segments(session, org_id, texts=["ask about the lot on Main Street"])

    r = await client.get("/api/v1/search/transcripts", params={"q": "main street"}, headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


@pytest.mark.pg_only
async def test_search_tsvector_path_finds_matching_transcript_text(client, session):
    """Only runs against real Postgres (see conftest's GUARD 3) - exercises the
    websearch_to_tsquery branch against the migration's generated tsvector index."""
    token, org = await _org(client, "se5@example.com", "Org SE5")
    org_id = uuid.UUID(org["id"])
    matching = await _call_with_segments(
        session, org_id, texts=["The seller wants to close by end of month"]
    )
    await _call_with_segments(session, org_id, texts=["Totally different subject"])

    set_org_context(session, org_id)
    results = await search_svc.search_transcripts(session, org_id, "seller close")
    assert len(results) == 1
    assert results[0]["call_id"] == str(matching.id)
