"""P43: each workspace's running risk score, level and automatic pause.

Every suspicious thing the monitor sees becomes a weighted signal. The score is the sum of
signal weights over MONITOR_SIGNAL_WINDOW_DAYS (minus anything an operator cleared):

  normal      < MONITOR_WATCH_SCORE
  watch       every call is reviewed, not a sample
  restricted  daily caps drop to MONITOR_RESTRICTED_DAILY_TEXTS / _CALLS
  paused      calling and texting stop at once; the AI writes a case file; owners are
              emailed; an operator unpauses or suspends. Leaving "paused" is always human.

The automatic steps never ban anyone (operator decision 2026-09-17).
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    Call,
    CallReview,
    KycProfile,
    Message,
    MonitorSignal,
    Org,
    OrgMonitoring,
    SecurityAlert,
)
from app.services import ai_guard
from app.services import audit as audit_svc

log = structlog.get_logger("monitor")

WEIGHTS: dict[str, int] = {
    "text_blocked": 25,
    "text_held_confirmed": 20,
    "text_unchecked_flagged": 25,
    "call_suspicious": 15,
    "call_scam": 40,
    "complaint_reply": 5,
    "stop_rate": 15,
    "short_calls": 10,
    "no_answer_rate": 10,
    "volume_spike": 10,
    "carrier_spam_flag": 15,
    "public_report": 20,
}
LEVEL_ORDER = {"normal": 0, "watch": 1, "restricted": 2, "paused": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def get_state(
    session: AsyncSession, org_id: uuid.UUID, *, create: bool = True
) -> OrgMonitoring | None:
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one_or_none()
    if row is None and create:
        row = OrgMonitoring(id=uuid.uuid4(), org_id=org_id, score=0, level="normal")
        session.add(row)
        await session.flush()
    return row


def level_for(settings: Settings, score: int) -> str:
    if score >= settings.monitor_pause_score:
        return "paused"
    if score >= settings.monitor_restrict_score:
        return "restricted"
    if score >= settings.monitor_watch_score:
        return "watch"
    return "normal"


async def current_score(
    session: AsyncSession, settings: Settings, state: OrgMonitoring, *, now: datetime | None = None
) -> int:
    now = now or _now()
    since = now - timedelta(days=settings.monitor_signal_window_days)
    cleared = _aware(state.cleared_before)
    if cleared is not None and cleared > since:
        since = cleared
    total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(MonitorSignal.weight), 0)).where(
                MonitorSignal.org_id == state.org_id, MonitorSignal.created_at >= since
            )
        )
    ).scalar_one()
    return int(total or 0)


async def add_signal(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    kind: str,
    summary: str,
    *,
    detail: dict | None = None,
    message_id: uuid.UUID | None = None,
    call_id: uuid.UUID | None = None,
    weight: int | None = None,
) -> OrgMonitoring:
    set_org_context(session, org_id)
    session.add(
        MonitorSignal(
            id=uuid.uuid4(),
            org_id=org_id,
            kind=kind,
            weight=WEIGHTS.get(kind, 10) if weight is None else weight,
            summary=summary[:500],
            detail=detail,
            message_id=message_id,
            call_id=call_id,
        )
    )
    await session.flush()
    state = await get_state(session, org_id)
    await recompute(session, settings, state)
    return state


async def recompute(session: AsyncSession, settings: Settings, state: OrgMonitoring) -> None:
    state.score = await current_score(session, settings, state)
    target = level_for(settings, state.score)
    if state.level == "paused":
        return  # only an operator ends a pause
    if target == state.level:
        return
    before = state.level
    state.level = target
    state.level_changed_at = _now()
    audit_svc.record(
        session,
        state.org_id,
        action="monitor.level_changed",
        target_type="org",
        target_id=str(state.org_id),
        detail={"from": before, "to": target, "score": state.score},
    )
    if target == "paused":
        state.paused_at = _now()
        state.paused_reason = (
            f"Risk score {state.score} reached the pause threshold ({settings.monitor_pause_score})"
        )
        state.case_file = {"status": "pending"}
        session.add(
            SecurityAlert(
                id=uuid.uuid4(),
                kind="traffic_paused",
                org_id=state.org_id,
                status="open",
                detail={"score": state.score, "reason": state.paused_reason},
            )
        )
        log.warning("monitor_paused", org_id=str(state.org_id), score=state.score)


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


async def refusal(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, kind: str
) -> str | None:
    """Telephony gate hook: ``account_paused`` / ``daily_limit_reached`` or None."""
    if not settings.monitor_enforced or kind == "number":
        return None
    state = await get_state(session, org_id, create=False)
    if state is None or state.level in ("normal", "watch"):
        return None
    if state.level == "paused":
        return "account_paused"
    now = _now()
    if kind == "sms":
        sent = (
            await session.execute(
                sa.select(sa.func.count(Message.id)).where(
                    Message.org_id == org_id,
                    Message.direction == "outbound",
                    Message.created_at >= _day_start(now),
                )
            )
        ).scalar_one()
        if sent >= settings.monitor_restricted_daily_texts:
            return "daily_limit_reached"
    if kind == "call":
        placed = (
            await session.execute(
                sa.select(sa.func.count(Call.id)).where(
                    Call.org_id == org_id,
                    Call.direction == "outbound",
                    Call.created_at >= _day_start(now),
                )
            )
        ).scalar_one()
        if placed >= settings.monitor_restricted_daily_calls:
            return "daily_limit_reached"
    return None


async def is_new_account(session: AsyncSession, settings: Settings, org_id: uuid.UUID) -> bool:
    """Approved (or created) within MONITOR_NEW_ACCOUNT_DAYS."""
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    started = _aware(profile.decided_at) if profile is not None else None
    if started is None:
        org = await session.get(Org, org_id)
        started = _aware(org.created_at) if org is not None else None
    set_org_context(session, org_id)
    return started is None or _now() - started < timedelta(days=settings.monitor_new_account_days)


async def unpause(
    session: AsyncSession, state: OrgMonitoring, *, operator_id: uuid.UUID, note: str
) -> None:
    now = _now()
    state.level = "normal"
    state.level_changed_at = now
    state.paused_at = None
    state.cleared_before = now
    state.score = 0
    state.reviewed_by = operator_id
    state.reviewed_at = now
    audit_svc.record(
        session,
        state.org_id,
        action="monitor.unpaused",
        target_type="org",
        target_id=str(state.org_id),
        actor_user_id=operator_id,
        detail={"note": note[:500]},
    )


CASE_SYSTEM = """You are the fraud investigator for a telecom platform. A business's calling
and texting was paused automatically because its traffic looked like scams. Write the case
file a human operator will read before deciding to unpause it or suspend and ban it.

Be specific and fair: quote the exact evidence, say what the business claims to do, and
point out anything that suggests a false alarm.

Return JSON with exactly these keys:
{
  "summary": "3-5 plain sentences",
  "what_they_claim": "the declared business and use case in one sentence",
  "what_we_saw": ["short factual findings"],
  "evidence_quotes": [{"source": "text"|"call"|"report"|"reply", "quote": "exact words"}],
  "false_alarm_signs": ["reasons this might be legitimate, if any"],
  "recommendation": "unpause" | "keep_paused" | "suspend_and_ban",
  "confidence": integer 0-100
}"""


async def case_evidence(session: AsyncSession, org_id: uuid.UUID, state: OrgMonitoring) -> dict:
    set_org_context(session, org_id)
    since = _aware(state.cleared_before) or (_now() - timedelta(days=30))
    signals = (
        (
            await session.execute(
                sa.select(MonitorSignal)
                .where(MonitorSignal.org_id == org_id, MonitorSignal.created_at >= since)
                .order_by(MonitorSignal.created_at.desc())
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    flagged = (
        (
            await session.execute(
                sa.select(Message)
                .where(
                    Message.org_id == org_id,
                    Message.moderation_state.in_(("held", "blocked")),
                    Message.created_at >= since,
                )
                .order_by(Message.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    reviews = (
        (
            await session.execute(
                sa.select(CallReview)
                .where(
                    CallReview.org_id == org_id,
                    CallReview.verdict.in_(("suspicious", "scam")),
                    CallReview.created_at >= since,
                )
                .order_by(CallReview.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    org = await session.get(Org, org_id)
    set_org_context(session, org_id)
    return {
        "business": {
            "name": org.name if org else None,
            "legal_name": profile.legal_name if profile else None,
            "website": profile.website if profile else None,
            "use_case": profile.use_case if profile else None,
        },
        "score": state.score,
        "signals": [
            {
                "kind": s.kind,
                "weight": s.weight,
                "summary": s.summary,
                "at": s.created_at.isoformat(),
            }
            for s in signals
        ],
        "flagged_texts": [
            {
                "body": (m.body or "")[:500],
                "state": m.moderation_state,
                "reason": m.moderation_reason,
            }
            for m in flagged
        ],
        "flagged_calls": [
            {
                "verdict": r.verdict,
                "summary": r.summary,
                "evidence": r.evidence,
                "category": r.category,
            }
            for r in reviews
        ],
    }


async def _owner_emails(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    from app.services import suspension

    return await suspension._owner_emails(session, org_id)


async def case_file_tick(session: AsyncSession, settings: Settings) -> int:
    """Write the AI case file for newly paused workspaces and email their owners."""
    from app.services import account_security

    # JUSTIFIED allow_unscoped: the sweeper walks every paused workspace.
    rows = (
        (
            await session.execute(
                sa.select(OrgMonitoring)
                .where(OrgMonitoring.level == "paused")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    written = 0
    for state in rows:
        if (state.case_file or {}).get("status") not in ("pending", "unavailable"):
            continue
        org_id = state.org_id
        evidence = await case_evidence(session, org_id, state)
        first_attempt = (state.case_file or {}).get("status") == "pending"
        try:
            judgement = await ai_guard.judge(
                settings,
                task="monitor_case_file",
                system=CASE_SYSTEM,
                user=ai_guard.data_block("evidence", evidence) + "\n\nWrite the case file.",
                max_tokens=1200,
            )
            state.case_file = {
                "status": "ready",
                **{
                    k: judgement.data.get(k)
                    for k in (
                        "summary",
                        "what_they_claim",
                        "what_we_saw",
                        "evidence_quotes",
                        "false_alarm_signs",
                        "recommendation",
                        "confidence",
                    )
                },
                "evidence": evidence,
                "written_at": _now().isoformat(),
            }
        except ai_guard.AIUnavailable:
            state.case_file = {"status": "unavailable", "evidence": evidence}
        written += 1
        await session.commit()
        if first_attempt:
            emails = await _owner_emails(session, org_id)
            if emails:
                await account_security.notify_now(
                    settings,
                    emails,
                    "Calling and texting paused for review",
                    "Our automatic monitoring paused calling and texting on your account "
                    "because recent traffic looked like it could be harmful to the people "
                    "receiving it. A member of our team is reviewing it now.\n\n"
                    "If you believe this is a mistake, open the console and send us an "
                    "explanation from the banner at the top of the page.",
                )
    return written
