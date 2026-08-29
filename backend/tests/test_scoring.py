"""P13 DR-8: LLM call scoring - done/disabled/failed paths, and the retry-once semantics.
MockTransport only - no live LLM call anywhere in this file (matches test_sms_agent.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import CallScore
from app.models.agent import CallTranscriptSegment
from app.models.voice import Call
from app.services import scoring
from tests.conftest import create_org, make_settings, register_and_login

FROZEN = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)


async def _org_id(client, email: str, name: str) -> uuid.UUID:
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return uuid.UUID(org["id"])


async def _terminal_call_with_transcript(session, org_id: uuid.UUID) -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164="+19725550101",
        our_e164="+12145550100",
        carrier="bandwidth",
        status="completed",
        ended_at=FROZEN,
    )
    session.add(call)
    await session.flush()
    session.add(
        CallTranscriptSegment(
            id=uuid.uuid4(), org_id=org_id, call_id=call.id, role="user", text="Hi there", at_ms=0
        )
    )
    session.add(
        CallTranscriptSegment(
            id=uuid.uuid4(),
            org_id=org_id,
            call_id=call.id,
            role="agent",
            text="How can I help?",
            at_ms=500,
        )
    )
    await session.commit()
    return call


def _anthropic_client(bodies: list) -> httpx.AsyncClient:
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            return httpx.Response(500, json={"error": {"message": "no scripted response left"}})
        item = queue.pop(0)
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=item)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _text_reply(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


async def _score_row(session, org_id: uuid.UUID, call_id: uuid.UUID) -> CallScore:
    set_org_context(session, org_id)
    return (
        await session.execute(sa.select(CallScore).where(CallScore.call_id == call_id))
    ).scalar_one()


async def test_no_llm_key_configured_marks_disabled(client, session):
    org_id = await _org_id(client, "sc1@example.com", "Org SC1")
    call = await _terminal_call_with_transcript(session, org_id)

    settings = make_settings()  # no anthropic_api_key / openai_api_key
    counts = await scoring.score_pending_calls(session, settings, now=FROZEN)
    assert counts == {"done": 0, "failed": 0, "disabled": 1}

    row = await _score_row(session, org_id, call.id)
    assert row.status == "disabled"
    assert row.sentiment is None
    assert row.score is None


async def test_successful_scoring_marks_done_with_parsed_fields(client, session):
    org_id = await _org_id(client, "sc2@example.com", "Org SC2")
    call = await _terminal_call_with_transcript(session, org_id)

    settings = make_settings(anthropic_api_key="test-anthropic-key")
    body = '{"sentiment": "positive", "score": 5, "summary": "Happy customer."}'
    mock = _anthropic_client([_text_reply(body)])
    async with mock:
        counts = await scoring.score_pending_calls(session, settings, client=mock, now=FROZEN)
    assert counts == {"done": 1, "failed": 0, "disabled": 0}

    row = await _score_row(session, org_id, call.id)
    assert row.status == "done"
    assert row.sentiment == "positive"
    assert row.score == 5
    assert row.summary == "Happy customer."


async def test_llm_error_marks_failed_and_is_retried_exactly_once(client, session):
    org_id = await _org_id(client, "sc3@example.com", "Org SC3")
    call = await _terminal_call_with_transcript(session, org_id)
    settings = make_settings(anthropic_api_key="test-anthropic-key")

    # First pass: the LLM 500s -> failed, eligible for one retry after the cooldown.
    mock1 = _anthropic_client([500])
    async with mock1:
        counts = await scoring.score_pending_calls(session, settings, client=mock1, now=FROZEN)
    assert counts == {"done": 0, "failed": 1, "disabled": 0}
    row = await _score_row(session, org_id, call.id)
    assert row.status == "failed"
    assert row.summary is None

    # Immediately again, before the cooldown - NOT yet eligible, so nothing to score.
    early_body = '{"sentiment":"neutral","score":3,"summary":"x"}'
    mock_too_soon = _anthropic_client([_text_reply(early_body)])
    async with mock_too_soon:
        counts = await scoring.score_pending_calls(
            session, settings, client=mock_too_soon, now=FROZEN + timedelta(seconds=10)
        )
    assert counts == {"done": 0, "failed": 0, "disabled": 0}

    # Past the cooldown, a SECOND failure marks it retry_exhausted - no more retries ever.
    later = FROZEN + timedelta(seconds=scoring.RETRY_AFTER_SECONDS + 1)
    mock2 = _anthropic_client([500])
    async with mock2:
        counts = await scoring.score_pending_calls(session, settings, client=mock2, now=later)
    assert counts == {"done": 0, "failed": 1, "disabled": 0}
    row = await _score_row(session, org_id, call.id)
    assert row.status == "failed"
    assert row.summary == scoring.RETRY_EXHAUSTED

    # Even much later, it is never picked up again.
    much_later = later + timedelta(days=1)
    mock3 = _anthropic_client([_text_reply('{"sentiment":"neutral","score":3,"summary":"x"}')])
    async with mock3:
        counts = await scoring.score_pending_calls(session, settings, client=mock3, now=much_later)
    assert counts == {"done": 0, "failed": 0, "disabled": 0}


async def test_a_retry_that_succeeds_leaves_a_clean_done_score(client, session):
    org_id = await _org_id(client, "sc4@example.com", "Org SC4")
    call = await _terminal_call_with_transcript(session, org_id)
    settings = make_settings(anthropic_api_key="test-anthropic-key")

    mock1 = _anthropic_client([500])
    async with mock1:
        await scoring.score_pending_calls(session, settings, client=mock1, now=FROZEN)

    later = FROZEN + timedelta(seconds=scoring.RETRY_AFTER_SECONDS + 1)
    body = '{"sentiment": "negative", "score": 1, "summary": "Angry."}'
    mock2 = _anthropic_client([_text_reply(body)])
    async with mock2:
        counts = await scoring.score_pending_calls(session, settings, client=mock2, now=later)
    assert counts == {"done": 1, "failed": 0, "disabled": 0}

    row = await _score_row(session, org_id, call.id)
    assert row.status == "done"
    assert row.sentiment == "negative"
    assert row.summary == "Angry."


async def test_call_without_a_transcript_is_not_a_candidate(client, session):
    org_id = await _org_id(client, "sc5@example.com", "Org SC5")
    set_org_context(session, org_id)
    session.add(
        Call(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="outbound",
            contact_e164="+19725550101",
            our_e164="+12145550100",
            carrier="bandwidth",
            status="completed",
            ended_at=FROZEN,
        )
    )
    await session.commit()

    settings = make_settings(anthropic_api_key="test-anthropic-key")
    counts = await scoring.score_pending_calls(session, settings, now=FROZEN)
    assert counts == {"done": 0, "failed": 0, "disabled": 0}


async def test_already_scored_call_is_not_rescanned(client, session):
    org_id = await _org_id(client, "sc6@example.com", "Org SC6")
    call = await _terminal_call_with_transcript(session, org_id)
    settings = make_settings(anthropic_api_key="test-anthropic-key")

    mock1 = _anthropic_client([_text_reply('{"sentiment":"neutral","score":3,"summary":"ok"}')])
    async with mock1:
        await scoring.score_pending_calls(session, settings, client=mock1, now=FROZEN)

    mock2 = _anthropic_client([_text_reply('{"sentiment":"positive","score":5,"summary":"nope"}')])
    async with mock2:
        counts = await scoring.score_pending_calls(session, settings, client=mock2, now=FROZEN)
    assert counts == {"done": 0, "failed": 0, "disabled": 0}

    row = await _score_row(session, org_id, call.id)
    assert row.sentiment == "neutral"  # unchanged by the second pass


async def test_openai_is_used_when_only_openai_key_is_configured(client, session):
    org_id = await _org_id(client, "sc7@example.com", "Org SC7")
    call = await _terminal_call_with_transcript(session, org_id)
    settings = make_settings(openai_api_key="test-openai-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.openai.com" in str(request.url)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"sentiment":"positive","score":4,"summary":"good"}'
                        }
                    }
                ]
            },
        )

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with mock:
        counts = await scoring.score_pending_calls(session, settings, client=mock, now=FROZEN)
    assert counts == {"done": 1, "failed": 0, "disabled": 0}

    row = await _score_row(session, org_id, call.id)
    assert row.status == "done"
    assert row.score == 4
