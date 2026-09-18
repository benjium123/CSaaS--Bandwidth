"""On-demand review of ONE account, run because an operator asked for it.

WHY THIS IS NOT JUST THE HOURLY SWEEP WITH A BUTTON. The sweep is tuned for cost across every
workspace: 24 hours of traffic, and only the cohorts that pass a cheap pre-filter get an AI
call. An operator asking "review this account thoroughly" has already decided this one account
is worth money, so the trade inverts — look back weeks instead of a day, review EVERY campaign
rather than only the suspicious-looking ones, and put the whole picture in one place.

IT TAKES NO ACTION. It records what it learns as signals, so the score reflects the review, and
it returns a report. It never changes the account's level, never pauses, never bans. That is
the operator's decision and this function deliberately cannot make it - see
`monitor_score.recompute`, where automatic restriction is off by default.

WHY A REPORT RATHER THAN A VERDICT. With a thousand customers the scarce resource is operator
attention, and attention is spent on READING, not on clicking. So the report is ordered to be
read top-down: what the monitor currently thinks, then the campaigns with the AI's reasoning,
then the raw behavioural facts underneath. Someone should be able to decide from the first
screen and drill down only when they disagree.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import Call, KycProfile, Message, MonitorSignal, Org
from app.services import monitor_cohorts, monitor_score

log = structlog.get_logger("monitor_review")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def traffic_summary(session: AsyncSession, org_id: uuid.UUID, since: datetime) -> dict:
    """Volume facts, cheap. Context for everything else: 4 strangers out of 20 messages is a
    different account from 4 out of 4,000, and the raw score cannot tell you which."""
    set_org_context(session, org_id)
    texts = (
        await session.execute(
            sa.select(
                Message.direction,
                sa.func.count(Message.id),
                sa.func.count(sa.distinct(Message.to_e164)),
            )
            .where(Message.org_id == org_id, Message.created_at >= since)
            .group_by(Message.direction)
        )
    ).all()
    by_direction = {d: {"messages": n, "recipients": r} for d, n, r in texts}
    calls = (
        await session.execute(
            sa.select(Call.direction, sa.func.count(Call.id))
            .where(Call.org_id == org_id, Call.created_at >= since)
            .group_by(Call.direction)
        )
    ).all()
    return {
        "outbound_texts": by_direction.get("outbound", {}).get("messages", 0),
        "outbound_recipients": by_direction.get("outbound", {}).get("recipients", 0),
        "inbound_texts": by_direction.get("inbound", {}).get("messages", 0),
        "calls": dict(calls),
    }


async def recent_signals(
    session: AsyncSession, org_id: uuid.UUID, since: datetime, *, limit: int = 50
) -> list[dict]:
    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(MonitorSignal)
                .where(MonitorSignal.org_id == org_id, MonitorSignal.created_at >= since)
                .order_by(MonitorSignal.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "kind": s.kind,
            "weight": s.weight,
            "summary": s.summary,
            "at": s.created_at.isoformat() if s.created_at else None,
            "detail": s.detail,
        }
        for s in rows
    ]


async def review_account(
    session: AsyncSession,
    settings,
    org_id: uuid.UUID,
    *,
    days: int | None = None,
    thorough: bool = True,
    record_signals: bool = True,
) -> dict:
    """Assemble the whole picture for one account, and review its campaigns with the AI.

    `thorough=True` (what an operator's button means) reviews EVERY campaign, not only the ones
    that trip the cheap pre-filter, because the pre-filter exists to save money on a sweep the
    operator did not ask for. `thorough=False` matches the sweep's behaviour, which is useful
    for comparing "what would the sweep have seen" against "what is actually there".
    """
    from app.services import ai_guard, monitor_text

    window = days or settings.monitor_review_window_days
    since = _now() - timedelta(days=window)
    set_org_context(session, org_id)

    org = await session.get(Org, org_id)
    if org is None:
        from app.errors import NotFoundError

        raise NotFoundError("Organization not found")
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    state = await monitor_score.get_state(session, org_id, create=True)

    report: dict = {
        "account": {
            "org_id": str(org_id),
            "name": org.name,
            "verification_status": profile.status if profile else None,
            "declared_business": await monitor_text.business_context(session, org_id),
        },
        "monitor": {
            "level": state.level,
            "score": state.score,
            # What the monitor WOULD do. Present precisely because it has not done it.
            "recommendation": (state.case_file or {}).get("recommendation"),
            "paused_reason": state.paused_reason,
        },
        "window_days": window,
        "traffic": await traffic_summary(session, org_id, since),
        "campaigns": [],
        "signals": await recent_signals(session, org_id, since),
        "ai": {"available": ai_guard.is_available(settings), "calls": 0, "skipped": 0},
        "reviewed_at": _now().isoformat(),
        "actions_taken": [],  # always empty: this function does not act. See the docstring.
    }

    cohorts = await monitor_cohorts.build(session, org_id, since=since)
    if not thorough:
        cohorts = [
            (c, m) for c, m in cohorts if monitor_cohorts.looks_like_a_campaign(c, m)
        ]

    business = report["account"]["declared_business"]
    budget = settings.monitor_review_max_ai_calls
    new_signals = 0
    for cohort, m in cohorts:
        entry = {
            "fingerprint": cohort.fingerprint,
            "template": cohort.sample_body[:500],
            "messages": cohort.size,
            "recipients": cohort.recipient_count,
            "first_seen": cohort.first_at.isoformat() if cohort.first_at else None,
            "last_seen": cohort.last_at.isoformat() if cohort.last_at else None,
            "never_contacted_before": round(m.first_contact_ratio, 3),
            "replied": round(m.reply_rate, 3),
            "undelivered": round(m.undelivered_rate, 3),
            "area_codes": m.spread,
            "flagged_by_prefilter": monitor_cohorts.looks_like_a_campaign(cohort, m),
            "verdict": None,
        }
        if report["ai"]["available"] and report["ai"]["calls"] < budget:
            try:
                review = await monitor_cohorts.review_one(settings, cohort, m, business)
            except ai_guard.AIUnavailable as exc:
                entry["verdict"] = {"error": str(exc)}
                report["ai"]["skipped"] += 1
            else:
                report["ai"]["calls"] += 1
                entry["verdict"] = {
                    "verdict": review.verdict,
                    "confidence": review.confidence,
                    "category": review.category,
                    "impersonates": review.impersonates,
                    "reason": review.reason,
                }
                # Recording keeps the score honest about what a review found. It still takes
                # no action - recompute only RECOMMENDS while MONITOR_AUTO_ACTION is off.
                if record_signals and review.actionable:
                    weight = (
                        monitor_cohorts.WEIGHT_INCONSISTENT
                        if review.confidence >= monitor_cohorts.CONFIDENT
                        else monitor_cohorts.WEIGHT_INCONSISTENT_WEAK
                    )
                    await monitor_score.add_signal(
                        session,
                        settings,
                        org_id,
                        "cohort_inconsistent",
                        f"Operator review: {review.reason}",
                        detail={**entry, "verdict": entry["verdict"], "source": "manual_review"},
                        weight=weight,
                    )
                    new_signals += 1
        else:
            report["ai"]["skipped"] += 1
        report["campaigns"].append(entry)

    if new_signals:
        await session.commit()
        set_org_context(session, org_id)
        state = await monitor_score.get_state(session, org_id, create=True)
        report["monitor"]["score"] = state.score
        report["monitor"]["level"] = state.level
        report["monitor"]["recommendation"] = (state.case_file or {}).get("recommendation")
    report["signals_added"] = new_signals

    # The one-line answer, so nobody has to add it up themselves.
    inconsistent = [
        c for c in report["campaigns"]
        if isinstance(c["verdict"], dict) and c["verdict"].get("verdict") == "inconsistent"
    ]
    report["headline"] = (
        f"{len(inconsistent)} of {len(report['campaigns'])} campaigns do not match this business"
        if inconsistent
        else f"No campaign contradicts this business ({len(report['campaigns'])} reviewed)"
    )
    log.info(
        "monitor_review",
        org_id=str(org_id),
        campaigns=len(report["campaigns"]),
        inconsistent=len(inconsistent),
        ai_calls=report["ai"]["calls"],
    )
    return report
