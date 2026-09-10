from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.db.base import set_org_context
from app.models.agent import CallTranscriptSegment
from app.models.voice import Call
from app.services.search import search_transcripts
from tests.conftest import create_org, register_and_login


async def _make_call(
    session,
    org_id: uuid.UUID,
    *,
    our_e164: str,
    contact_e164: str,
    direction: str,
    status: str,
    created_at: datetime,
    ended_at: datetime | None = None,
    answered_at: datetime | None = None,
    duration_seconds: int | None = None,
) -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        direction=direction,
        carrier="bandwidth",
        status=status,
        ended_at=ended_at,
        answered_at=answered_at,
        duration_seconds=duration_seconds,
        extra={},
        created_at=created_at,
    )
    session.add(call)
    await session.commit()
    return call


async def _make_segment(
    session,
    org_id: uuid.UUID,
    call: Call,
    role: str,
    text: str,
    at_ms: int,
) -> CallTranscriptSegment:
    set_org_context(session, org_id)
    segment = CallTranscriptSegment(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        role=role,
        text=text,
        at_ms=at_ms,
    )
    session.add(segment)
    await session.commit()
    return segment


async def test_matched_flag_is_set_only_on_matching_segments(client, session):
    token = await register_and_login(client, "search-d25-1@example.com")
    org = await create_org(client, token, "Search D25 One")
    org_id = uuid.UUID(org["id"])

    call = await _make_call(
        session,
        org_id,
        our_e164="+12145550101",
        contact_e164="+19725550101",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    await _make_segment(session, org_id, call, "agent", "intro", 0)
    await _make_segment(session, org_id, call, "contact", "needle is here", 1000)
    await _make_segment(session, org_id, call, "agent", "outro", 2000)

    result = await search_transcripts(session, org_id, "needle")
    assert len(result) == 1
    segments = sorted(result[0]["segments"], key=lambda s: s["at_ms"])
    assert [s["matched"] for s in segments] == [False, True, False]


async def test_matched_flag_is_not_reused_across_calls(client, session):
    token = await register_and_login(client, "search-d25-2@example.com")
    org = await create_org(client, token, "Search D25 Two")
    org_id = uuid.UUID(org["id"])

    call_a = await _make_call(
        session,
        org_id,
        our_e164="+12145550101",
        contact_e164="+19725550101",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 10, 0, tzinfo=timezone.utc),
    )
    call_b = await _make_call(
        session,
        org_id,
        our_e164="+12145550101",
        contact_e164="+19725550102",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )

    await _make_segment(session, org_id, call_a, "agent", "no match A", 0)
    await _make_segment(session, org_id, call_a, "contact", "needle A", 1000)
    await _make_segment(session, org_id, call_b, "agent", "needle B", 0)
    await _make_segment(session, org_id, call_b, "contact", "no match B", 1000)

    result = await search_transcripts(session, org_id, "needle")
    by_call = {r["call_id"]: r for r in result}
    assert len(by_call) == 2

    segments_a = sorted(by_call[str(call_a.id)]["segments"], key=lambda s: s["at_ms"])
    segments_b = sorted(by_call[str(call_b.id)]["segments"], key=lambda s: s["at_ms"])
    assert [s["matched"] for s in segments_a] == [False, True]
    assert [s["matched"] for s in segments_b] == [True, False]


async def test_matched_flag_survives_a_second_query(client, session):
    token = await register_and_login(client, "search-d25-3@example.com")
    org = await create_org(client, token, "Search D25 Three")
    org_id = uuid.UUID(org["id"])

    call = await _make_call(
        session,
        org_id,
        our_e164="+12145550101",
        contact_e164="+19725550101",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    await _make_segment(session, org_id, call, "agent", "apple pie", 0)
    await _make_segment(session, org_id, call, "contact", "banana bread", 1000)

    apple_result = await search_transcripts(session, org_id, "apple")
    assert len(apple_result) == 1
    apple_segments = sorted(apple_result[0]["segments"], key=lambda s: s["at_ms"])
    assert [s["matched"] for s in apple_segments] == [True, False]

    banana_result = await search_transcripts(session, org_id, "banana")
    assert len(banana_result) == 1
    banana_segments = sorted(banana_result[0]["segments"], key=lambda s: s["at_ms"])
    assert [s["matched"] for s in banana_segments] == [False, True]


async def test_search_respects_inbox_scoping(client, session):
    token = await register_and_login(client, "search-d25-4@example.com")
    org = await create_org(client, token, "Search D25 Four")
    org_id = uuid.UUID(org["id"])

    call_a = await _make_call(
        session,
        org_id,
        our_e164="+12145550101",
        contact_e164="+19725550101",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    call_b = await _make_call(
        session,
        org_id,
        our_e164="+12145550102",
        contact_e164="+19725550102",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 5, 0, tzinfo=timezone.utc),
    )
    await _make_segment(session, org_id, call_a, "contact", "needle", 0)
    await _make_segment(session, org_id, call_b, "contact", "needle", 0)

    assert await search_transcripts(session, org_id, "needle", allowed_e164s=frozenset()) == []

    result = await search_transcripts(
        session,
        org_id,
        "needle",
        allowed_e164s=frozenset({"+12145550101"}),
    )
    assert len(result) == 1
    assert result[0]["call_id"] == str(call_a.id)


@pytest.mark.pg_only
async def test_stemmed_match_is_flagged_on_postgres(client, session):
    token = await register_and_login(client, "search-d25-pg@example.com")
    org = await create_org(client, token, "Search D25 Postgres")
    org_id = uuid.UUID(org["id"])

    call = await _make_call(
        session,
        org_id,
        our_e164="+12145550101",
        contact_e164="+19725550101",
        direction="inbound",
        status="completed",
        created_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    await _make_segment(session, org_id, call, "contact", "he was running late", 0)
    await _make_segment(session, org_id, call, "agent", "unrelated", 1000)

    result = await search_transcripts(session, org_id, "run")
    assert len(result) == 1
    segments = sorted(result[0]["segments"], key=lambda s: s["at_ms"])
    assert [s["matched"] for s in segments] == [True, False]
