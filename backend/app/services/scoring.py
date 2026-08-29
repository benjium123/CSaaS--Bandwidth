"""LLM sentiment + call scoring (P13 DR-8).

Same shape as P12's voicemail transcription seam: no LLM key configured -> every
candidate call is honestly marked ``disabled``, never a fake score. Scoring goes through
the EXISTING ``services/llm_client.chat`` - no new LLM code path, no new provider choice
logic beyond "prefer Anthropic, fall back to OpenAI" (the same preference sms_agent's
default already encodes).

Retry-once (DR-8): ``call_scores`` has no attempts column, so "already retried once" is
encoded in ``summary``: a first failure leaves ``summary`` NULL and is eligible for one
retry after ``RETRY_AFTER_SECONDS``; a retry that ALSO fails stamps
``summary="retry_exhausted"``, which excludes it from every future candidate scan. Tests
drive this with an explicit ``now=`` rather than a real sleep (frozen-clock discipline).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import CallScore
from app.models.agent import CallTranscriptSegment
from app.models.voice import TERMINAL_CALL_STATUSES, Call
from app.services import llm_client

log = structlog.get_logger("scoring")

SCORING_TIMEOUT_SECONDS = 20.0
#: A `failed` score is retried once, no sooner than this many seconds after the failure.
RETRY_AFTER_SECONDS = 300
#: Marks a CallScore that has already had its one retry and failed again - excluded from
#: every future candidate scan.
RETRY_EXHAUSTED = "retry_exhausted"
_VALID_SENTIMENTS = ("positive", "neutral", "negative")

SYSTEM_PROMPT = (
    "You score a phone call transcript for sentiment. Respond with STRICT JSON only, no "
    "prose, no markdown fences, exactly this shape: "
    '{"sentiment": "positive" | "neutral" | "negative", "score": <integer 1-5>, '
    '"summary": "<one sentence>"}'
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pick_provider(settings: Settings) -> tuple[str, str] | None:
    """(provider, api_key), preferring Anthropic (matches sms_agent's default), or None
    when neither is configured."""
    anthropic_key = settings.anthropic_api_key.get_secret_value().strip()
    if anthropic_key:
        return "anthropic", anthropic_key
    openai_key = settings.openai_api_key.get_secret_value().strip()
    if openai_key:
        return "openai", openai_key
    return None


def _parse_result(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Non-JSON scoring response: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Scoring response was not a JSON object")

    sentiment = data.get("sentiment")
    if sentiment not in _VALID_SENTIMENTS:
        raise ValueError(f"Invalid sentiment: {sentiment!r}")

    try:
        score = int(data.get("score"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid score: {data.get('score')!r}") from exc
    if not 1 <= score <= 5:
        raise ValueError(f"Score out of range: {score}")

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Missing summary")

    return {"sentiment": sentiment, "score": score, "summary": summary.strip()[:2000]}


async def _find_candidates(
    session: AsyncSession, *, limit: int, now: datetime | None = None
) -> list[Call]:
    moment = now or _now()
    already_scored = sa.select(CallScore.call_id)
    has_transcript = sa.select(CallTranscriptSegment.call_id).distinct()

    fresh = (
        (
            await session.execute(
                sa.select(Call)
                .where(
                    Call.status.in_(TERMINAL_CALL_STATUSES),
                    Call.id.in_(has_transcript),
                    Call.id.not_in(already_scored),
                )
                .order_by(Call.ended_at)
                .limit(limit)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    remaining = limit - len(fresh)
    if remaining <= 0:
        return list(fresh)

    cutoff = moment - timedelta(seconds=RETRY_AFTER_SECONDS)
    retryable = (
        (
            await session.execute(
                sa.select(Call)
                .join(CallScore, CallScore.call_id == Call.id)
                .where(
                    CallScore.status == "failed",
                    CallScore.summary.is_(None),
                    CallScore.updated_at <= cutoff,
                )
                .order_by(CallScore.updated_at)
                .limit(remaining)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    return list(fresh) + list(retryable)


async def _get_or_create_score(session: AsyncSession, call: Call) -> tuple[CallScore, bool]:
    """Returns (row, is_retry)."""
    row = (
        await session.execute(sa.select(CallScore).where(CallScore.call_id == call.id))
    ).scalar_one_or_none()
    is_retry = row is not None and row.status == "failed"
    if row is None:
        row = CallScore(id=uuid.uuid4(), org_id=call.org_id, call_id=call.id, status="pending")
        session.add(row)
    return row, is_retry


async def _score_one(
    session: AsyncSession,
    provider: str,
    api_key: str,
    client: httpx.AsyncClient,
    call: Call,
    moment: datetime,
) -> bool:
    segments = (
        (
            await session.execute(
                sa.select(CallTranscriptSegment)
                .where(CallTranscriptSegment.call_id == call.id)
                .order_by(CallTranscriptSegment.at_ms)
            )
        )
        .scalars()
        .all()
    )
    transcript = "\n".join(f"{s.role}: {s.text}" for s in segments) or "(no speech captured)"

    row, is_retry = await _get_or_create_score(session, call)
    turns = [llm_client.ChatTurn(role="user", content=f"Transcript:\n{transcript}")]
    try:
        result = await llm_client.chat(
            client,
            provider=provider,
            model="",
            api_key=api_key,
            system=SYSTEM_PROMPT,
            turns=turns,
            tools=[],
            timeout=SCORING_TIMEOUT_SECONDS,
        )
        parsed = _parse_result(result.text)
    except (llm_client.LLMError, ValueError) as exc:
        log.warning("call_scoring_failed", call_id=str(call.id), error=str(exc))
        row.status = "failed"
        row.sentiment = None
        row.score = None
        row.summary = RETRY_EXHAUSTED if is_retry else None
        # Explicit stamp (not the ORM's onupdate, which uses the REAL wall clock): the
        # retry-cooldown cutoff below compares against `now=`, so this row's "when did it
        # fail" marker must live on the SAME clock the caller controls in tests.
        row.updated_at = moment
        return False

    row.status = "done"
    row.sentiment = parsed["sentiment"]
    row.score = parsed["score"]
    row.summary = parsed["summary"]
    row.updated_at = moment
    return True


async def score_pending_calls(
    session: AsyncSession,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    limit: int = 10,
    now: datetime | None = None,
) -> dict[str, int]:
    """Sweeper-driven. Scores every terminal call with a transcript and no CallScore row
    yet, plus any `failed` score eligible for its one retry."""
    moment = now or _now()
    counts = {"done": 0, "failed": 0, "disabled": 0}
    candidates = await _find_candidates(session, limit=limit, now=moment)
    if not candidates:
        return counts

    picked = _pick_provider(settings)
    if picked is None:
        for call in candidates:
            set_org_context(session, call.org_id)
            row, _is_retry = await _get_or_create_score(session, call)
            row.status = "disabled"
            row.sentiment = None
            row.score = None
            row.summary = None
            row.updated_at = moment
            await session.commit()
            counts["disabled"] += 1
        return counts

    provider, api_key = picked
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=SCORING_TIMEOUT_SECONDS)
    try:
        for call in candidates:
            set_org_context(session, call.org_id)
            ok = await _score_one(session, provider, api_key, client, call, moment)
            await session.commit()
            counts["done" if ok else "failed"] += 1
    finally:
        if owns_client:
            await client.aclose()
    return counts
