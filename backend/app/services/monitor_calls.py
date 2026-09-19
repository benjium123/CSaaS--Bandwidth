"""P43: call monitoring - the AI reviews call transcripts for scams.

Which calls (``choose``):
  - every call for accounts approved in the last MONITOR_NEW_ACCOUNT_DAYS ("new_account")
  - every call while the account is at watch / restricted level ("watch")
  - MONITOR_CALL_SAMPLE_PERCENT of the rest ("sample")

How we get words out of a call:
  - carrier calls (Telnyx/SignalWire/Bandwidth): the call is recorded (the recording
    announcement always plays first) and the recording is transcribed with Deepgram
  - softphone calls in LiveKit rooms: the ``call-monitor`` worker (agents/call_monitor.py)
    joins silently, plays the announcement and streams a transcript
  - AI assistant calls already have a transcript

The review (``review_tick``) happens a couple of minutes after the call ends: DeepSeek Flash
compares the conversation with the business's declared use case and returns ok /
suspicious / scam with exact quotes. Suspicious and scam calls become risk signals.

Behaviour signals that need no words (``behaviour_tick``): very short calls, very low answer
rates, sudden volume far above what the business declared, STOP spikes, angry replies and
carrier spam flags.
"""

from __future__ import annotations

import json
import random
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from time import monotonic

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    Call,
    CallRecording,
    CallReview,
    CallTranscriptSegment,
    ConsentEvent,
    Message,
    MonitorSignal,
)
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services import ai_guard, monitor_score, monitor_text

log = structlog.get_logger("monitor_calls")

MONITOR_AGENT_NAME_DEFAULT = "call-monitor"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
REVIEW_DELAY = timedelta(minutes=2)
TICK_BUDGET_SECONDS = 90
GIVE_UP_AFTER = timedelta(hours=6)
MIN_TALK_SECONDS = 15
MAX_TRANSCRIPT_CHARS = 24_000

SYSTEM = """You review a call transcript from a business using a telecom platform, to catch
scam and fraud calls hidden inside normal business calling.

"agent" is the business's side of the call; "user" is the person they called or who called
them. Compare what the business says and asks for with what the business declares it does.

Verdicts:
- "ok": normal business conversation that fits the declared business.
- "suspicious": things a legitimate business rarely does - pressure or threats, asking for
  card numbers / bank logins / one-time codes / remote computer access, impersonating
  someone else, or a conversation unrelated to the declared business.
- "scam": clearly fraudulent - impersonating a government agency, bank, tech support or
  utility; demanding gift cards, crypto or wire transfers; fake prizes, refunds or debts;
  tricking the person into sending money or codes.

Return JSON: {"verdict": "ok"|"suspicious"|"scam", "confidence": 0-100,
"category": "none"|"impersonation"|"payment_scam"|"tech_support"|"credential_theft"|
"prize_or_refund"|"threats"|"off_business"|"other",
"summary": "two plain sentences about what happened",
"evidence": [{"speaker": "agent"|"user", "quote": "exact words from the transcript"}]}"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def agent_name(settings: Settings) -> str:
    return getattr(settings, "monitor_agent_name", "") or MONITOR_AGENT_NAME_DEFAULT


async def choose(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, *, rng=random.random
) -> str | None:
    """Why this call should be monitored, or None."""
    if not settings.monitor_enforced:
        return None
    state = await monitor_score.get_state(session, org_id, create=False)
    if state is not None and state.level in ("watch", "restricted", "paused"):
        return "watch"
    if await monitor_score.is_new_account(session, settings, org_id):
        return "new_account"
    if rng() * 100 < settings.monitor_call_sample_percent:
        return "sample"
    return None


def mark(call: Call, reason: str, *, record: bool) -> None:
    """Stamp a call as monitored. Carrier calls also record (announcement first)."""
    extra = dict(call.extra or {})
    extra["monitor"] = reason
    if record:
        extra["record"] = True
    call.extra = extra


async def queue_review(session: AsyncSession, call: Call, reason: str) -> None:
    set_org_context(session, call.org_id)
    exists = (
        await session.execute(sa.select(CallReview.id).where(CallReview.call_id == call.id))
    ).first()
    if exists is None:
        session.add(CallReview(id=uuid.uuid4(), org_id=call.org_id, call_id=call.id, reason=reason))


async def dispatch_listener(session: AsyncSession, api, settings: Settings, call: Call) -> bool:  # noqa: ANN001
    """Put the call-monitor worker into a monitored LiveKit room call. Never raises."""
    if api is None or not (call.extra or {}).get("monitor"):
        return False
    extra = call.extra or {}
    if extra.get("via") != "livekit" or not extra.get("room"):
        return False
    if (extra.get("assistant") or {}).get("dispatched") or extra.get("monitor_dispatched"):
        return False
    from app.models import Org
    from app.services import agent as agent_svc
    from app.services import calling_settings as calling_settings_svc

    token = agent_svc.mint_call_worker_token(settings, call_id=call.id, org_id=call.org_id)
    if not token:
        return False
    org = await session.get(Org, call.org_id)
    metadata = json.dumps(
        {
            "call_id": str(call.id),
            "org_id": str(call.org_id),
            "worker_token": token,
            "announcement": calling_settings_svc.announcement_text_for(org),
        }
    )
    try:
        await api.create_agent_dispatch(
            room=extra["room"], agent_name=agent_name(settings), metadata=metadata
        )
    except Exception:  # noqa: BLE001 - monitoring must never break a call
        log.exception("call_monitor_dispatch_failed", call_id=str(call.id))
        return False
    set_org_context(session, call.org_id)
    call.extra = {**(call.extra or {}), "monitor_dispatched": True}
    return True


# --------------------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------------------
async def _segments(session: AsyncSession, call_id: uuid.UUID) -> list[CallTranscriptSegment]:
    return list(
        (
            await session.execute(
                sa.select(CallTranscriptSegment)
                .where(CallTranscriptSegment.call_id == call_id)
                .order_by(CallTranscriptSegment.at_ms)
            )
        )
        .scalars()
        .all()
    )


def _utterances_to_segments(call: Call, payload: dict) -> list[tuple[str, str, int]]:
    """Deepgram diarized utterances -> (role, text, at_ms). On an outbound call the first
    voice is almost always the person answering ("Hello?"), so that speaker is "user"."""
    utterances = (payload.get("results") or {}).get("utterances") or []
    if not utterances:
        alt = (
            ((payload.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}]
        )[0]
        text = (alt.get("transcript") or "").strip()
        return [("agent", text, 0)] if text else []
    first_speaker = utterances[0].get("speaker")
    out = []
    for u in utterances:
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        is_first = u.get("speaker") == first_speaker
        if call.direction == "outbound":
            role = "user" if is_first else "agent"
        else:
            role = "agent" if is_first else "user"
        out.append((role, text, int(float(u.get("start") or 0) * 1000)))
    return out


async def transcribe_recording(
    session: AsyncSession,
    settings: Settings,
    store,  # noqa: ANN001
    call: Call,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """'done' | 'waiting' (recording not stored yet) | 'none' (no recording) | 'failed'."""
    from app.services import agent as agent_svc
    from app.services import recordings as recordings_svc

    recording = (
        await session.execute(
            sa.select(CallRecording)
            .where(CallRecording.call_id == call.id)
            .order_by(CallRecording.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recording is None:
        return "none"
    if recording.status != "stored":
        return "waiting" if recording.status == "pending" else "none"
    api_key = settings.deepgram_api_key.get_secret_value().strip()
    if not api_key:
        return "failed"
    try:
        data = await recordings_svc.load_recording_bytes(store, recording)
    except KeyError:
        return "failed"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=120.0)
    try:
        resp = await client.post(
            DEEPGRAM_URL,
            params={
                "model": "nova-2",
                "smart_format": "true",
                "diarize": "true",
                "utterances": "true",
            },
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": recording.content_type or "audio/mpeg",
            },
            content=data,
        )
        if resp.status_code >= 400:
            # 429/5xx are worth retrying; any other 4xx will fail the same way every time.
            retryable = resp.status_code in (408, 429) or resp.status_code >= 500
            return "failed" if retryable else "rejected"
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return "failed"
    finally:
        if owns:
            await client.aclose()

    class _Seg:
        def __init__(self, role, text, at_ms):
            self.role, self.text, self.at_ms = role, text, at_ms

    rows = [_Seg(*s) for s in _utterances_to_segments(call, payload)]
    await agent_svc.upsert_transcript_segments(session, call, rows)
    return "done"


# --------------------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------------------
async def judge_call(
    settings: Settings, context: dict, transcript: str, call_meta: dict
) -> dict:
    """The AI verdict on one call transcript - no database, so the exam and canary use the
    same judgement as live calls. Raises AIUnavailable on no/invalid answer."""
    judgement = await ai_guard.judge(
        settings,
        task="call_review",
        system=SYSTEM,
        user="\n\n".join(
            [
                ai_guard.data_block("business", context),
                ai_guard.data_block("call", call_meta),
                ai_guard.data_block("call transcript", transcript[:MAX_TRANSCRIPT_CHARS]),
                "Review this call.",
            ]
        ),
        max_tokens=700,
    )
    data = judgement.data
    verdict = data.get("verdict")
    if verdict not in ("ok", "suspicious", "scam"):
        raise ai_guard.AIUnavailable("AI gave no valid verdict")
    try:
        confidence = max(0, min(100, int(data.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0
    return {
        "verdict": verdict,
        "confidence": confidence,
        "category": str(data.get("category") or "none")[:32],
        "summary": str(data.get("summary") or "")[:1000],
        "evidence": [
            {
                "speaker": str(e.get("speaker") or "")[:8],
                "quote": str(e.get("quote") or "")[:500],
            }
            for e in (data.get("evidence") or [])[:8]
            if isinstance(e, dict)
        ],
        "tokens": (judgement.tokens_in, judgement.tokens_out),
    }


async def review_one(
    session: AsyncSession,
    settings: Settings,
    store,  # noqa: ANN001
    review: CallReview,
    call: Call,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    set_org_context(session, call.org_id)
    now = _now()
    review.updated_at = now  # rotate: a waiting review goes to the back of the queue
    if call.status not in TERMINAL_CALL_STATUSES:
        started = _aware(call.created_at) or now
        if now - started > GIVE_UP_AFTER:
            review.status = "skipped"
            review.error = "The call never reached a final status"
            return "skipped"
        return "waiting"
    ended = _aware(call.ended_at) or _aware(call.updated_at) or now
    if now - ended < REVIEW_DELAY:
        return "waiting"
    if call.status != "completed" or (call.duration_seconds or 0) < MIN_TALK_SECONDS:
        review.status = "skipped"
        review.error = "Call too short or not connected"
        return "skipped"

    segments = await _segments(session, call.id)
    if not segments:
        outcome = await transcribe_recording(session, settings, store, call, client=client)
        if outcome == "waiting" and now - ended < GIVE_UP_AFTER:
            return "waiting"
        segments = await _segments(session, call.id)
        if not segments:
            if outcome == "failed" and now - ended < GIVE_UP_AFTER:
                review.attempts += 1
                return "waiting"
            review.status = "skipped"
            review.error = (
                "The recording could not be transcribed"
                if outcome == "rejected"
                else "No transcript available"
            )
            return "skipped"

    transcript = "\n".join(f"{s.role}: {s.text}" for s in segments)[:MAX_TRANSCRIPT_CHARS]
    context = await monitor_text.business_context(session, call.org_id)
    set_org_context(session, call.org_id)
    try:
        result = await judge_call(
            settings,
            context,
            transcript,
            {"direction": call.direction, "duration_seconds": call.duration_seconds},
        )
    except ai_guard.AIUnavailable as exc:
        # Stays pending and is retried as the queue rotates; only a call that still can't be
        # reviewed a day later is given up on.
        review.attempts += 1
        review.error = str(exc)[:255]
        if now - ended > timedelta(hours=24):
            review.status = "error"
        return "error"

    verdict = result["verdict"]
    confidence = result["confidence"]
    evidence = result["evidence"]
    review.status = "reviewed"
    review.verdict = verdict
    review.confidence = confidence
    review.category = result["category"]
    review.summary = result["summary"]
    review.evidence = evidence
    review.tokens_in, review.tokens_out = result["tokens"]
    review.reviewed_at = now
    review.error = None
    if verdict in ("suspicious", "scam"):
        await monitor_score.add_signal(
            session,
            settings,
            call.org_id,
            "call_scam" if verdict == "scam" else "call_suspicious",
            f"{verdict.capitalize()} call: {review.summary or review.category}",
            detail={"category": review.category, "confidence": confidence, "evidence": evidence},
            call_id=call.id,
        )
    return verdict


async def review_tick(
    session: AsyncSession,
    settings: Settings,
    store,  # noqa: ANN001
    *,
    client: httpx.AsyncClient | None = None,
    limit: int = 20,
) -> dict:
    counts: dict[str, int] = {}
    # JUSTIFIED allow_unscoped: the sweeper walks pending reviews across workspaces.
    pending = (
        (
            await session.execute(
                sa.select(CallReview)
                .where(CallReview.status == "pending")
                .order_by(CallReview.updated_at)
                .limit(limit)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    started = monotonic()
    for review in pending:
        if monotonic() - started > TICK_BUDGET_SECONDS:
            break  # the rest waits for the next pass; the sweeper must not stall
        set_org_context(session, review.org_id)
        call = await session.get(Call, review.call_id)
        if call is None:
            await session.delete(review)
            await session.commit()
            continue
        try:
            outcome = await review_one(session, settings, store, review, call, client=client)
        except Exception:  # noqa: BLE001 - one bad call must not stop the queue
            log.exception("call_review_failed", call_id=str(call.id))
            await session.rollback()
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
        await session.commit()
        if outcome == "error":
            break  # the AI is unavailable: don't spend this pass retrying every call
    return counts


# --------------------------------------------------------------------------------------
# Behaviour signals (no words needed)
# --------------------------------------------------------------------------------------
COMPLAINT_RE = re.compile(
    r"(?i)\b(scam(mer)?|fraud|stop (calling|texting)|who is this|reported? (you|this)|"
    r"police|ftc|do not (call|text)|harass(ing|ment)?|fake)\b"
)
DAILY_CAPS = {"complaint_reply": 4}


async def _signalled_today(session: AsyncSession, org_id: uuid.UUID, kind: str) -> int:
    start = datetime.combine(_now().date(), time.min, tzinfo=timezone.utc)
    return (
        await session.execute(
            sa.select(sa.func.count(MonitorSignal.id)).where(
                MonitorSignal.org_id == org_id,
                MonitorSignal.kind == kind,
                MonitorSignal.created_at >= start,
            )
        )
    ).scalar_one()


async def _signal_once_a_day(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    kind: str,
    summary: str,
    detail: dict,
) -> bool:
    set_org_context(session, org_id)
    if await _signalled_today(session, org_id, kind) >= DAILY_CAPS.get(kind, 1):
        return False
    await monitor_score.add_signal(session, settings, org_id, kind, summary, detail=detail)
    # Commit per workspace: the next org's context must never flush this org's rows.
    await session.commit()
    return True


async def behaviour_tick(
    session: AsyncSession, settings: Settings, *, since: datetime | None = None
) -> dict:
    """Hourly: look at the last 24 hours of each active workspace's traffic."""
    if not settings.monitor_enforced:
        return {}
    now = _now()
    day_ago = now - timedelta(hours=24)
    counts = {"signals": 0}

    # JUSTIFIED allow_unscoped: platform-wide sweep, grouped by org, aggregates only.
    call_rows = (
        await session.execute(
            sa.select(
                Call.org_id,
                sa.func.count(Call.id),
                sa.func.sum(
                    sa.case(
                        (sa.and_(Call.status == "completed", Call.duration_seconds < 10), 1),
                        else_=0,
                    )
                ),
                sa.func.sum(
                    sa.case((Call.status.in_(("no_answer", "busy", "canceled")), 1), else_=0)
                ),
            )
            .where(Call.direction == "outbound", Call.created_at >= day_ago)
            .group_by(Call.org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    for org_id, total, short, unanswered in call_rows:
        total, short, unanswered = int(total or 0), int(short or 0), int(unanswered or 0)
        if total < 30:
            continue
        if short / total > 0.5 and await _signal_once_a_day(
            session,
            settings,
            org_id,
            "short_calls",
            f"{short} of {total} calls in 24h lasted under 10 seconds",
            {"total": total, "short": short},
        ):
            counts["signals"] += 1
        if unanswered / total > 0.8 and await _signal_once_a_day(
            session,
            settings,
            org_id,
            "no_answer_rate",
            f"{unanswered} of {total} calls in 24h were not answered",
            {"total": total, "unanswered": unanswered},
        ):
            counts["signals"] += 1
        declared = await _declared_daily(session, org_id, "monthly_calls")
        if (
            declared
            and total > max(50, declared * 3)
            and await _signal_once_a_day(
                session,
                settings,
                org_id,
                "volume_spike",
                f"{total} calls in 24h vs about {declared}/day declared",
                {"total": total, "declared_daily": declared},
            )
        ):
            counts["signals"] += 1

    text_rows = (
        await session.execute(
            sa.select(Message.org_id, sa.func.count(Message.id))
            .where(Message.direction == "outbound", Message.created_at >= day_ago)
            .group_by(Message.org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    for org_id, total in text_rows:
        total = int(total or 0)
        if total < 50:
            continue
        set_org_context(session, org_id)
        opt_outs = (
            await session.execute(
                sa.select(sa.func.count(ConsentEvent.id)).where(
                    ConsentEvent.org_id == org_id,
                    ConsentEvent.created_at >= day_ago,
                    ConsentEvent.event == "opt_out",
                )
            )
        ).scalar_one()
        if opt_outs / total > 0.05 and await _signal_once_a_day(
            session,
            settings,
            org_id,
            "stop_rate",
            f"{opt_outs} opt-outs from {total} texts in 24h",
            {"total": total, "opt_outs": opt_outs},
        ):
            counts["signals"] += 1
        spam = (
            await session.execute(
                sa.select(sa.func.count(Message.id)).where(
                    Message.org_id == org_id,
                    Message.direction == "outbound",
                    Message.created_at >= day_ago,
                    Message.error_code.in_(_carrier_spam_codes()),
                )
            )
        ).scalar_one()
        if spam >= 5 and await _signal_once_a_day(
            session,
            settings,
            org_id,
            "carrier_spam_flag",
            f"Carriers flagged {spam} texts as spam in 24h",
            {"spam_rejections": spam},
        ):
            counts["signals"] += 1

    replies = (
        (
            await session.execute(
                sa.select(Message)
                .where(
                    Message.direction == "inbound",
                    Message.created_at >= (since or now - timedelta(hours=1)),
                )
                .limit(2000)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    counted: dict[uuid.UUID, set[str]] = {}
    for reply in replies:
        if not COMPLAINT_RE.search(reply.body or ""):
            continue
        if reply.org_id not in counted:
            set_org_context(session, reply.org_id)
            counted[reply.org_id] = {
                str((row.detail or {}).get("message_id"))
                for row in (
                    await session.execute(
                        sa.select(MonitorSignal).where(
                            MonitorSignal.org_id == reply.org_id,
                            MonitorSignal.kind == "complaint_reply",
                            MonitorSignal.created_at >= now - timedelta(days=2),
                        )
                    )
                ).scalars()
            }
        if str(reply.id) in counted[reply.org_id]:
            continue  # this reply was already counted
        counted[reply.org_id].add(str(reply.id))
        if await _signal_once_a_day(
            session,
            settings,
            reply.org_id,
            "complaint_reply",
            f'A recipient replied: "{(reply.body or "")[:120]}"',
            {"message_id": str(reply.id)},
        ):
            counts["signals"] += 1
    await session.commit()
    return counts


async def _declared_daily(session: AsyncSession, org_id: uuid.UUID, key: str) -> int | None:
    from app.models import KycProfile

    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    try:
        monthly = int(((profile.use_case or {}) if profile else {}).get(key) or 0)
    except (TypeError, ValueError):
        return None
    return max(1, monthly // 30) if monthly else None


def _carrier_spam_codes() -> tuple[str, ...]:
    from app.services import reputation

    return tuple(sorted(reputation.BANDWIDTH_SPAM_CLASS_CODES | reputation.TELNYX_SPAM_CLASS_CODES))


async def on_livekit_event(session: AsyncSession, api, settings: Settings, event: dict) -> None:  # noqa: ANN001
    """When the phone side of a room call joins, monitored calls get the listener.

    Outbound softphone calls were marked when they were placed; inbound room calls are
    chosen here, as they arrive. AI assistant calls already produce a transcript."""
    if not settings.monitor_enforced or event.get("event") != "participant_joined":
        return
    participant = event.get("participant") or {}
    sip_call_id = (participant.get("attributes") or {}).get("sip.callID") or ""
    if not sip_call_id:
        return
    from app.models import CallLeg

    # JUSTIFIED allow_unscoped: resolve the call from the SIP id before any org context.
    leg = (
        await session.execute(
            sa.select(CallLeg)
            .where(CallLeg.provider_call_id == sip_call_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    room = ((event.get("room") or {}).get("name")) or ""
    call = None
    if leg is not None:
        set_org_context(session, leg.org_id)
        call = await session.get(Call, leg.call_id)
    elif room.startswith("call-"):
        try:
            call_id = uuid.UUID(room[len("call-") :])
        except ValueError:
            call_id = None
        if call_id is not None:
            call = (
                await session.execute(
                    sa.select(Call)
                    .where(Call.id == call_id)
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            ).scalar_one_or_none()
    if call is None or (call.extra or {}).get("via") != "livekit":
        return
    set_org_context(session, call.org_id)
    reason = (call.extra or {}).get("monitor")
    if reason is None and call.direction == "inbound":
        reason = await choose(session, settings, call.org_id)
        if reason is None:
            return
        mark(call, reason, record=False)
        await queue_review(session, call, reason)
    if reason is None:
        return
    if not ((call.extra or {}).get("assistant") or {}).get("profile_id"):
        await dispatch_listener(session, api, settings, call)
    await session.commit()
