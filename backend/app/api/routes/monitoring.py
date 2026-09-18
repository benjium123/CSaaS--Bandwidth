"""P43 traffic monitoring routes.

/api/v1/monitoring            the workspace's own status and appeal (customers)
/api/v1/public/report-number  anyone can report a call or text from one of our numbers
/api/v1/ops/monitoring        operators: queue, case files, unpause/suspend, held texts,
                              the daily report and monitor health
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    OperatorContext,
    OrgContext,
    check_step_up,
    require_operator,
    require_permission,
)
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import (
    CallReview,
    Message,
    MonitorHealth,
    MonitorLabel,
    MonitorSignal,
    NumberReport,
    Org,
    OrgMonitoring,
    OrgNumber,
    TextVerdict,
)
from app.rate_limit import enforce_rate_limit
from app.services import identity as identity_svc
from app.services import monitor_score, monitor_text

customer_router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])
public_router = APIRouter(prefix="/api/v1/public", tags=["public"])
ops_router = APIRouter(prefix="/api/v1/ops/monitoring", tags=["ops"])

Reviewer = Annotated[OperatorContext, Depends(require_operator("reviewer"))]
Admin = Annotated[OperatorContext, Depends(require_operator("admin"))]

PUBLIC_LEVEL_TEXT = {
    "normal": None,
    "watch": None,
    "restricted": (
        "Calling and texting are limited for now while we check some recent activity. "
        "Normal limits return automatically when it looks fine."
    ),
    "paused": (
        "Calling and texting are paused while our team reviews recent activity on this "
        "account. If you think this is a mistake, tell us what you were doing below."
    ),
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------------------
class AppealIn(BaseModel):
    explanation: str = Field(min_length=20, max_length=4000)


@customer_router.get("/status")
async def my_status(
    ctx: Annotated[OrgContext, Depends(require_permission("org:read"))],
) -> dict:
    state = await monitor_score.get_state(ctx.session, ctx.org.id, create=False)
    level = state.level if state is not None else "normal"
    return {
        # watch is internal: customers only see limits that affect them.
        "level": level if level in ("restricted", "paused") else "normal",
        "message": PUBLIC_LEVEL_TEXT.get(level),
        "paused_at": _iso(state.paused_at) if state else None,
        "appealed_at": _iso(state.appealed_at) if state else None,
    }


@customer_router.post("/appeal")
async def appeal(
    payload: AppealIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    if ctx.actor_user_id is None:
        raise ValidationFailedError("A person, not an API key, must send the explanation")
    state = await monitor_score.get_state(ctx.session, ctx.org.id, create=False)
    if state is None or state.level not in ("paused", "restricted"):
        raise ConflictError("There is nothing to appeal on this account")
    state.appeal = payload.explanation.strip()
    state.appealed_at = _now()
    from app.models import SecurityAlert

    ctx.session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="monitor_appeal",
            org_id=ctx.org.id,
            status="open",
            detail={"level": state.level, "explanation": state.appeal[:500]},
        )
    )
    await ctx.session.commit()
    return {"appealed_at": _iso(state.appealed_at)}


# --------------------------------------------------------------------------------------
# Public reports
# --------------------------------------------------------------------------------------
class ReportIn(BaseModel):
    number: str = Field(min_length=7, max_length=32)
    kind: str = Field(pattern="^(call|text)$")
    description: str = Field(min_length=10, max_length=2000)
    contact: str | None = Field(default=None, max_length=255)


@public_router.post("/report-number", status_code=202)
async def report_number(
    payload: ReportIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Always 202 and the same answer - it must not reveal whether a number is ours."""
    ip = identity_svc.client_ip(request) or "unknown"
    await enforce_rate_limit(request, f"report-number:{ip}")
    from app.api.routes.numbers import to_e164

    # No region is threaded through here ON PURPOSE: this route is unauthenticated, so there
    # is no workspace whose country could resolve a bare national number. The raw string is
    # kept when parsing fails, because a report we can't normalise is still worth having.
    try:
        e164 = to_e164(payload.number) or payload.number.strip()
    except Exception:  # noqa: BLE001 - a malformed number is still accepted and stored
        e164 = payload.number.strip()
    # JUSTIFIED allow_unscoped: the public doesn't know (and must not learn) the workspace.
    number = (
        await session.execute(
            sa.select(OrgNumber)
            .where(OrgNumber.e164 == e164[:20])
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    # One report per number per sender address per day: repeating it adds nothing.
    repeat = (
        await session.execute(
            sa.select(NumberReport.id).where(
                NumberReport.reported_e164 == e164[:20],
                NumberReport.ip == ip[:64],
                NumberReport.created_at >= _now() - timedelta(days=1),
            )
        )
    ).first()
    if repeat is not None:
        return {"received": True}
    session.add(
        NumberReport(
            id=uuid.uuid4(),
            reported_e164=e164[:20],
            org_id=number.org_id if number else None,
            kind=payload.kind,
            description=payload.description.strip(),
            reporter_contact=(payload.contact or "").strip()[:255] or None,
            ip=ip[:64],
        )
    )
    await session.commit()
    return {"received": True}


REPORT_SYSTEM = """You triage reports from members of the public about a call or text they
received from a phone number on a telecom platform. Decide whether the report credibly
describes scam, fraud or abusive contact (not just an unwanted but legitimate business
message), given what the business says it does.

Return JSON: {"credible": true|false, "category": "scam"|"fraud"|"harassment"|"spam"|
"legitimate"|"unclear", "summary": "one sentence"}"""


async def _report_signals_today(session: AsyncSession, org_id: uuid.UUID) -> int:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(sa.func.count(MonitorSignal.id)).where(
                MonitorSignal.org_id == org_id,
                MonitorSignal.kind == "public_report",
                MonitorSignal.created_at >= _now() - timedelta(days=1),
            )
        )
    ).scalar_one()


async def assess_reports_tick(session: AsyncSession, settings) -> int:  # noqa: ANN001
    from app.services import ai_guard

    rows = (
        (
            await session.execute(
                sa.select(NumberReport)
                .where(NumberReport.assessment.is_(None))
                .order_by(NumberReport.created_at)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    done = 0
    for report in rows:
        if report.org_id is None:
            report.assessment = {
                "credible": False,
                "category": "unclear",
                "summary": "Not our number",
            }
            await session.commit()
            continue
        context = await monitor_text.business_context(session, report.org_id)
        try:
            judgement = await ai_guard.judge(
                settings,
                task="report_triage",
                system=REPORT_SYSTEM,
                user=ai_guard.data_block("business", context)
                + "\n\n"
                + ai_guard.data_block(
                    "report", {"kind": report.kind, "description": report.description}
                )
                + "\n\nTriage this report.",
                max_tokens=200,
            )
        except ai_guard.AIUnavailable:
            break
        data = judgement.data
        report.assessment = {
            "credible": bool(data.get("credible")),
            "category": str(data.get("category") or "unclear")[:32],
            "summary": str(data.get("summary") or "")[:300],
        }
        if (
            report.assessment["credible"]
            and report.assessment["category"] != "legitimate"
            and await _report_signals_today(session, report.org_id)
            < monitor_score.PUBLIC_REPORT_DAILY_CAP
        ):
            await monitor_score.add_signal(
                session,
                settings,
                report.org_id,
                "public_report",
                f"Public report ({report.kind}): {report.assessment['summary']}",
                detail={"report_id": str(report.id), "category": report.assessment["category"]},
            )
        await session.commit()
        done += 1
    return done


# --------------------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------------------
class DecisionIn(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class HeldDecisionIn(BaseModel):
    decision: str = Field(pattern="^(release|block)$")
    note: str = Field(default="", max_length=500)


@ops_router.get("")
async def queue(op: Reviewer, level: str | None = Query(default=None)) -> list[dict]:
    stmt = sa.select(OrgMonitoring, Org).join(Org, Org.id == OrgMonitoring.org_id)
    if level:
        stmt = stmt.where(OrgMonitoring.level == level)
    else:
        stmt = stmt.where(OrgMonitoring.level != "normal")
    rows = (
        await op.session.execute(
            stmt.order_by(OrgMonitoring.score.desc()).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).all()
    return [
        {
            "org_id": str(state.org_id),
            "org_name": org.name,
            "level": state.level,
            "score": state.score,
            "paused_at": _iso(state.paused_at),
            "appealed": state.appealed_at is not None,
            "case_status": (state.case_file or {}).get("status"),
            "recommendation": (state.case_file or {}).get("recommendation"),
        }
        for state, org in rows
    ]


@ops_router.get("/orgs/{org_id}")
async def case(org_id: uuid.UUID, op: Reviewer) -> dict:
    org = await op.session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Organization not found")
    state = await monitor_score.get_state(op.session, org_id)
    set_org_context(op.session, org_id)
    since = _now() - timedelta(days=30)
    signals = (
        (
            await op.session.execute(
                sa.select(MonitorSignal)
                .where(MonitorSignal.org_id == org_id, MonitorSignal.created_at >= since)
                .order_by(MonitorSignal.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    held = (
        (
            await op.session.execute(
                sa.select(Message)
                .where(
                    Message.org_id == org_id,
                    Message.moderation_state.in_(("held", "blocked")),
                    Message.created_at >= since,
                )
                .order_by(Message.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    reviews = (
        (
            await op.session.execute(
                sa.select(CallReview)
                .where(CallReview.org_id == org_id, CallReview.status == "reviewed")
                .order_by(CallReview.reviewed_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    await op.session.commit()
    return {
        "org_id": str(org_id),
        "org_name": org.name,
        "level": state.level,
        "score": state.score,
        "paused_at": _iso(state.paused_at),
        "paused_reason": state.paused_reason,
        "case_file": state.case_file,
        "appeal": state.appeal,
        "appealed_at": _iso(state.appealed_at),
        "signals": [
            {
                "id": str(s.id),
                "kind": s.kind,
                "weight": s.weight,
                "summary": s.summary,
                "at": _iso(s.created_at),
                "message_id": str(s.message_id) if s.message_id else None,
                "call_id": str(s.call_id) if s.call_id else None,
            }
            for s in signals
        ],
        "texts": [
            {
                "id": str(m.id),
                "body": m.body,
                "to": m.to_e164,
                "state": m.moderation_state,
                "reason": m.moderation_reason,
                "status": m.status,
                "at": _iso(m.created_at),
            }
            for m in held
        ],
        "calls": [
            {
                "call_id": str(r.call_id),
                "verdict": r.verdict,
                "confidence": r.confidence,
                "category": r.category,
                "summary": r.summary,
                "evidence": r.evidence,
                "reason": r.reason,
                "at": _iso(r.reviewed_at),
            }
            for r in reviews
        ],
    }


def _label(
    op: OperatorContext, kind: str, label: str, content: str, org_id, source_id, context=None
) -> None:  # noqa: ANN001
    if not (content or "").strip():
        return
    op.session.add(
        MonitorLabel(
            id=uuid.uuid4(),
            kind=kind,
            label=label,
            content=content[:8000],
            context=context,
            org_id=org_id,
            source_id=source_id,
            decided_by=op.user.id,
        )
    )


async def _label_case(op: OperatorContext, org_id: uuid.UUID, label: str) -> None:
    """The operator's call on a case labels every flagged text and call in it."""
    detail = await case(org_id, op)
    context = {"business": await monitor_text.business_context(op.session, org_id)}
    for text in detail["texts"]:
        _label(op, "text", label, text["body"] or "", org_id, uuid.UUID(text["id"]), context)
    for call in detail["calls"]:
        if call["verdict"] in ("suspicious", "scam"):
            quotes = " / ".join(e.get("quote", "") for e in (call["evidence"] or []))
            _label(
                op,
                "call",
                label,
                f"{call['summary']}\n{quotes}",
                org_id,
                uuid.UUID(call["call_id"]),
                context,
            )


@ops_router.post("/orgs/{org_id}/unpause")
async def unpause(org_id: uuid.UUID, payload: DecisionIn, request: Request, op: Admin) -> dict:
    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="monitor_unpause")
    state = await monitor_score.get_state(op.session, org_id, create=False)
    if state is None or state.level == "normal":
        raise ConflictError("This account is not paused or restricted")
    await _label_case(op, org_id, "legit")
    set_org_context(op.session, org_id)
    await monitor_score.unpause(op.session, state, operator_id=op.user.id, note=payload.note)
    await op.session.commit()
    return await case(org_id, op)


@ops_router.post("/orgs/{org_id}/suspend")
async def suspend(org_id: uuid.UUID, payload: DecisionIn, request: Request, op: Admin) -> dict:
    """Confirmed abuse: label the evidence as scam and suspend (+ ban) the business."""
    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="suspend")
    from app.services import suspension

    await _label_case(op, org_id, "scam")
    set_org_context(op.session, org_id)
    state = await monitor_score.get_state(op.session, org_id)
    state.reviewed_by = op.user.id
    state.reviewed_at = _now()
    await op.session.commit()
    try:
        await suspension.suspend(
            op.session,
            request.app,
            org_id,
            operator_id=op.user.id,
            reason=f"Traffic monitoring: {payload.note}",
            ban=True,
        )
    except (NotFoundError, ValidationFailedError, ConflictError):
        # No verification profile to suspend (e.g. verification not enforced): the
        # monitoring pause stays in place, which already stops all calling and texting.
        set_org_context(op.session, org_id)
        state = await monitor_score.get_state(op.session, org_id)
        state.level = "paused"
        state.paused_at = state.paused_at or _now()
        state.paused_reason = f"Suspended by operator: {payload.note}"
        await op.session.commit()
    return await case(org_id, op)


@ops_router.post("/texts/{message_id}")
async def decide_held_text(
    message_id: uuid.UUID, payload: HeldDecisionIn, request: Request, op: Reviewer
) -> dict:
    # JUSTIFIED allow_unscoped: operators act across workspaces; the row names its org.
    message = (
        await op.session.execute(
            sa.select(Message)
            .where(Message.id == message_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if message is None or message.moderation_state != "held":
        raise NotFoundError("Held text not found")
    org_id = message.org_id
    set_org_context(op.session, org_id)
    settings = request.app.state.settings
    digest = monitor_text.body_hash(message.body or "")
    if payload.decision == "release":
        await monitor_text._store(
            op.session,
            org_id,
            digest,
            monitor_text.Screening(
                action="allow", reason=payload.note or "Released by operator", source="operator"
            ),
        )
        message.moderation_state = "cleared"
        message.failure_reason_public = None
        message.hold_until = _now()
        _label(
            op,
            "text",
            "legit",
            message.body or "",
            org_id,
            message.id,
            {"business": await monitor_text.business_context(op.session, org_id)},
        )
    else:
        await monitor_text._store(
            op.session,
            org_id,
            digest,
            monitor_text.Screening(
                action="block", reason=payload.note or "Blocked by operator", source="operator"
            ),
        )
        message.status = "rejected"
        message.moderation_state = "blocked"
        message.error_code = "blocked_scam"
        message.failure_reason_public = monitor_text.PUBLIC_BLOCKED
        _label(
            op,
            "text",
            "scam",
            message.body or "",
            org_id,
            message.id,
            {"business": await monitor_text.business_context(op.session, org_id)},
        )
        await monitor_score.add_signal(
            op.session,
            settings,
            org_id,
            "text_held_confirmed",
            "Operator blocked a held text",
            message_id=message.id,
        )
    await op.session.commit()
    return {"id": str(message.id), "state": message.moderation_state, "status": message.status}


@ops_router.get("/held-texts")
async def held_texts(op: Reviewer) -> list[dict]:
    rows = (
        (
            await op.session.execute(
                sa.select(Message)
                .where(Message.moderation_state == "held", Message.status == "queued")
                .order_by(Message.created_at)
                .limit(100)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(m.id),
            "org_id": str(m.org_id),
            "body": m.body,
            "to": m.to_e164,
            "reason": m.moderation_reason,
            "at": _iso(m.created_at),
        }
        for m in rows
    ]


@ops_router.get("/report")
async def daily_report(op: Reviewer, days: int = Query(default=1, ge=1, le=30)) -> dict:
    """What the monitor did, and whether it still works."""
    since = _now() - timedelta(days=days)
    unscoped = {ALLOW_UNSCOPED_KEY: True}

    async def count(stmt) -> int:  # noqa: ANN001
        return int((await op.session.execute(stmt.execution_options(**unscoped))).scalar_one() or 0)

    texts_checked = await count(
        sa.select(sa.func.count(Message.id)).where(
            Message.direction == "outbound",
            Message.created_at >= since,
            Message.moderation_state.is_not(None),
        )
    )
    by_state = dict(
        (
            await op.session.execute(
                sa.select(Message.moderation_state, sa.func.count(Message.id))
                .where(Message.direction == "outbound", Message.created_at >= since)
                .group_by(Message.moderation_state)
                .execution_options(**unscoped)
            )
        ).all()
    )
    calls = dict(
        (
            await op.session.execute(
                sa.select(CallReview.verdict, sa.func.count(CallReview.id))
                .where(CallReview.created_at >= since)
                .group_by(CallReview.verdict)
                .execution_options(**unscoped)
            )
        ).all()
    )
    ai_tokens = (
        await op.session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(TextVerdict.tokens_in), 0),
                sa.func.coalesce(sa.func.sum(TextVerdict.tokens_out), 0),
            )
            .where(TextVerdict.created_at >= since)
            .execution_options(**unscoped)
        )
    ).one()
    call_tokens = (
        await op.session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(CallReview.tokens_in), 0),
                sa.func.coalesce(sa.func.sum(CallReview.tokens_out), 0),
            )
            .where(CallReview.created_at >= since)
            .execution_options(**unscoped)
        )
    ).one()
    levels = dict(
        (
            await op.session.execute(
                sa.select(OrgMonitoring.level, sa.func.count(OrgMonitoring.id))
                .group_by(OrgMonitoring.level)
                .execution_options(**unscoped)
            )
        ).all()
    )
    latest = {}
    for kind in ("canary", "exam"):
        row = (
            await op.session.execute(
                sa.select(MonitorHealth)
                .where(MonitorHealth.kind == kind)
                .order_by(MonitorHealth.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        latest[kind] = (
            {"passed": row.passed, "at": _iso(row.created_at), "detail": row.detail}
            if row
            else None
        )
    reports = await count(
        sa.select(sa.func.count(NumberReport.id)).where(NumberReport.created_at >= since)
    )
    tokens_in = int(ai_tokens[0]) + int(call_tokens[0])
    tokens_out = int(ai_tokens[1]) + int(call_tokens[1])
    return {
        "since": _iso(since),
        "texts": {
            "checked": texts_checked,
            "allowed": int(by_state.get("allowed", 0)) + int(by_state.get("cleared", 0)),
            "held_now": int(by_state.get("held", 0)),
            "blocked": int(by_state.get("blocked", 0)),
        },
        "calls": {
            "reviewed": int(sum(v for k, v in calls.items() if k)),
            "ok": int(calls.get("ok", 0)),
            "suspicious": int(calls.get("suspicious", 0)),
            "scam": int(calls.get("scam", 0)),
            "waiting": int(calls.get(None, 0)),
        },
        "accounts": {level: int(n) for level, n in levels.items()},
        "public_reports": reports,
        "ai_tokens": {"in": tokens_in, "out": tokens_out},
        "health": latest,
    }


# --------------------------------------------------------------------------------------
# P44: per-account monitoring for an operator with hundreds of customers
# --------------------------------------------------------------------------------------
#: How many rows a `needs_decision` scan will look at before giving up. Recommendations are
#: rare by design, so this is generous; if it is ever hit, the queue is the problem.
_DECISION_SCAN_CAP = 5000


class ReviewRequestIn(BaseModel):
    #: How far back to look. Defaults to MONITOR_REVIEW_WINDOW_DAYS.
    days: int | None = Field(default=None, ge=1, le=365)
    #: True (the default) reviews EVERY campaign. False reproduces what the hourly sweep
    #: would have seen, which is how you tell "the sweep missed it" from "it was not there".
    thorough: bool = True


class DecisionIn2(BaseModel):
    #: apply = do what the monitor recommended. reject = discard it and leave the account be.
    action: str = Field(pattern="^(apply|reject)$")
    note: str = Field(min_length=3, max_length=500)


@ops_router.get("/accounts")
async def accounts(
    request: Request,
    op: Reviewer,
    level: str | None = Query(default=None),
    q: str | None = Query(default=None),
    needs_decision: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """EVERY customer with its own monitor, not just the ones above `normal`.

    The queue at `GET ""` deliberately shows only accounts that have tripped something, which
    is right for triage but wrong for an operator running a book of 100-1000 customers who
    wants to see the whole book. Paged, filterable, and ordered so the accounts that need a
    human come first: those with a pending recommendation, then by score.
    """
    # OUTER join from Org, not inner from OrgMonitoring: a monitoring row is only created when
    # something happens, so an inner join lists only customers that have already tripped a
    # signal - which is the queue endpoint again, and hides the quiet majority this endpoint
    # exists to show. Every customer appears; those with no row read as normal/0.
    stmt = (
        sa.select(OrgMonitoring, Org)
        .select_from(Org)
        .outerjoin(OrgMonitoring, OrgMonitoring.org_id == Org.id)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    if level:
        if level not in monitor_score.LEVEL_ORDER:
            raise ValidationFailedError(f"Unknown level: {level}")
        stmt = stmt.where(OrgMonitoring.level == level)
    if q:
        stmt = stmt.where(Org.name.ilike(f"%{q.strip()}%"))
    total = (
        await op.session.execute(
            sa.select(sa.func.count()).select_from(stmt.subquery()).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalar_one()
    # `needs_decision` lives inside a JSON column, which cannot be filtered portably across
    # SQLite and PostgreSQL, so it is filtered in Python. That means paginating AFTER the
    # filter, not before - otherwise "show me what is waiting on me" returns only the waiting
    # accounts that happen to fall in the first page, which at 1000 customers is usually none.
    scan = stmt.order_by(OrgMonitoring.score.desc().nullslast(), Org.name.asc())
    if not needs_decision:
        scan = scan.limit(limit).offset(offset)
    else:
        scan = scan.limit(_DECISION_SCAN_CAP)
    rows = (await op.session.execute(scan)).all()

    items = []
    for state, org in rows:
        recommendation = (
            monitor_score.recommended_level(state)
            and (state.case_file or {}).get(monitor_score.PENDING_ACTION)
            if state is not None
            else None
        )
        if needs_decision and not recommendation:
            continue
        items.append(
            {
                "org_id": str(org.id),
                "name": org.name,
                "level": state.level if state is not None else "normal",
                "score": state.score if state is not None else 0,
                # The whole point of the no-auto-action policy: what the monitor WOULD do,
                # sitting here waiting for a person rather than already done.
                "recommendation": recommendation,
                "needs_decision": bool(recommendation),
                "reviewed_at": _iso(state.reviewed_at) if state is not None else None,
                "level_changed_at": (
                    _iso(state.level_changed_at) if state is not None else None
                ),
            }
        )
    if needs_decision:
        total = len(items)
        items = items[offset : offset + limit]
    return {
        "accounts": items,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        # Shown so an operator is never guessing whether the monitor is acting on its own.
        "auto_action": bool(request.app.state.settings.monitor_auto_action),
    }


@ops_router.post("/orgs/{org_id}/review")
async def review_now(
    org_id: uuid.UUID, payload: ReviewRequestIn, request: Request, op: Reviewer
) -> dict:
    """Run a thorough review of ONE account, right now, because an operator asked.

    Records what it finds as signals so the score reflects the review, and takes NO action:
    with MONITOR_AUTO_ACTION off, reaching a restricting score produces a recommendation for
    a human, never a restriction. Deliberately available to a Reviewer rather than an Admin -
    looking harder at an account is not a privileged action; acting on it is.
    """
    from app.services import audit as audit_svc
    from app.services import monitor_review

    settings = request.app.state.settings
    report = await monitor_review.review_account(
        op.session, settings, org_id, days=payload.days, thorough=payload.thorough
    )
    set_org_context(op.session, org_id)
    audit_svc.record(
        op.session,
        org_id,
        action="monitor.reviewed_on_demand",
        target_type="org",
        target_id=str(org_id),
        actor_user_id=op.user.id,
        detail={
            "window_days": report["window_days"],
            "thorough": payload.thorough,
            "campaigns": len(report["campaigns"]),
            "ai_calls": report["ai"]["calls"],
            "signals_added": report["signals_added"],
        },
    )
    state = await monitor_score.get_state(op.session, org_id, create=True)
    state.reviewed_by = op.user.id
    state.reviewed_at = _now()
    await op.session.commit()
    return report


@ops_router.post("/orgs/{org_id}/decision")
async def decide_recommendation(
    org_id: uuid.UUID, payload: DecisionIn2, request: Request, op: Admin
) -> dict:
    """Apply or reject what the monitor recommended. This is the human decision.

    Admin, not Reviewer, and behind the same `recent_2fa` step-up as unpause and suspend:
    applying a recommendation restricts a paying customer, which is exactly the class of
    action the no-auto-action policy exists to keep in human hands.
    """
    from app.services import audit as audit_svc

    await check_step_up(
        request, op.session, op.user, kind="recent_2fa", action="monitor_decision"
    )
    set_org_context(op.session, org_id)
    state = await monitor_score.get_state(op.session, org_id, create=False)
    if state is None:
        raise NotFoundError("This account has no monitoring state")
    recommended = monitor_score.recommended_level(state)
    if recommended is None:
        raise ConflictError("There is no recommendation to decide on")

    applied = None
    if payload.action == "apply":
        before = state.level
        if recommended == "paused":
            # Through the SHARED path, so an operator-applied pause leaves exactly the state an
            # automatic one does - including case_file["status"], which is what makes the
            # sweeper write the evidence pack and email the owners.
            monitor_score.enter_pause(
                state, reason=f"Operator applied the monitor's recommendation: {payload.note}"
            )
        else:
            state.level = recommended
            state.level_changed_at = _now()
        applied = recommended
        audit_svc.record(
            op.session,
            org_id,
            action="monitor.recommendation_applied",
            target_type="org",
            target_id=str(org_id),
            actor_user_id=op.user.id,
            detail={"from": before, "to": recommended, "note": payload.note},
        )
    else:
        audit_svc.record(
            op.session,
            org_id,
            action="monitor.recommendation_rejected",
            target_type="org",
            target_id=str(org_id),
            actor_user_id=op.user.id,
            detail={"rejected": recommended, "note": payload.note},
        )
    # Either way the recommendation is spent. A rejected one must not reappear on the next
    # recompute as if nobody had looked, so the score is cleared to the decision point -
    # the same mechanism `unpause` uses to stop old signals re-pausing an account.
    monitor_score.clear_recommendation(state)
    if payload.action == "reject":
        state.cleared_before = _now()
        state.score = 0
    state.reviewed_by = op.user.id
    state.reviewed_at = _now()
    await op.session.commit()
    return {
        "org_id": str(org_id),
        "decision": payload.action,
        "applied_level": applied,
        "level": state.level,
        "score": state.score,
    }


# --------------------------------------------------------------------------------------
# Entering a customer's account by hand
# --------------------------------------------------------------------------------------
# WHAT THIS IS AND IS NOT. The operator asked to "enter their accounts manually whenever we
# decide". This implements that as READ-ONLY INSPECTION: an operator can look at everything a
# customer's traffic contains and judge for themselves. It does NOT impersonate. No customer
# session is minted, no request is ever made AS the customer, and nothing here can send,
# cancel, refund or change a setting on their behalf.
#
# That is a deliberate narrowing of the literal ask, for two reasons worth stating rather than
# burying. First, every action an operator needs is already its own audited endpoint (pause,
# unpause, hold decisions, the recommendation decision) - impersonation would add no
# capability, only a way to take those actions without the audit trail naming them. Second, an
# impersonation token is the most valuable thing an attacker can steal from a platform like
# this: one stolen operator cookie becomes write access to every customer. Reading is
# recoverable and reviewable; acting as someone is neither.
#
# WHY IT IS STILL GATED HARD. Reading a customer's private message bodies is itself sensitive -
# more so than the case file, which shows only what was already flagged. So: `recent_2fa`
# step-up, the same as pausing an account, and EVERY access is audited with how many bodies
# were exposed. Not sampled. The audit row is the customer's only protection against an
# operator browsing their messages out of curiosity, and a sampled audit protects nobody.

#: The inspector never returns more than this in one page. Reading a customer's messages should
#: feel like reading, with each page a deliberate act that lands in the audit log - not like an
#: export that quietly drains an entire account in one request.
_INSPECT_MAX_PAGE = 200


def _inspect_audit(op: OperatorContext, org_id: uuid.UUID, *, what: str, detail: dict) -> None:
    """One audit row per access. Called before the commit that returns the data."""
    from app.services import audit as audit_svc

    set_org_context(op.session, org_id)
    audit_svc.record(
        op.session,
        org_id,
        action=f"monitor.account_inspected.{what}",
        target_type="org",
        target_id=str(org_id),
        actor_user_id=op.user.id,
        detail=detail,
    )


@ops_router.get("/orgs/{org_id}/inspect")
async def inspect_account(
    org_id: uuid.UUID,
    request: Request,
    op: Reviewer,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """The account as a person needs to see it to form their own judgement.

    Distinct from `GET /orgs/{org_id}`, which is the CASE file - what the system already
    flagged. The whole premise of this design is that a scammer's messages mostly do not get
    flagged (four scams in four hundred, none of them blocked), so an operator who can only see
    flagged traffic can only ever confirm what the machine already thought. This shows the real
    shape of the account: its numbers, its volumes, and the campaign structure of its outbound
    with no AI verdict attached - the operator reads the templates and decides.
    """
    org = await op.session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Organization not found")
    await check_step_up(
        request, op.session, op.user, kind="recent_2fa", action="monitor_inspect_account"
    )

    from app.services import monitor_cohorts, monitor_review

    settings = request.app.state.settings
    since = _now() - timedelta(days=days)
    set_org_context(op.session, org_id)
    numbers = (
        (
            await op.session.execute(
                sa.select(OrgNumber).where(OrgNumber.org_id == org_id).limit(200)
            )
        )
        .scalars()
        .all()
    )
    state = await monitor_score.get_state(op.session, org_id, create=False)
    built = await monitor_cohorts.build(op.session, org_id, since=since)
    campaigns = [
        {
            "fingerprint": cohort.fingerprint,
            # Every sample, not just one: an operator reading a template needs to see how much
            # the variants differ, which is the whole basis for judging whether it is one
            # campaign or a legitimate template that happened to cluster.
            "samples": [b[:500] for b in cohort.bodies_for_review()],
            "messages": cohort.size,
            "recipients": cohort.recipient_count,
            "first_seen": _iso(cohort.first_at),
            "last_seen": _iso(cohort.last_at),
            "never_contacted_before": round(m.first_contact_ratio, 3),
            "replied": round(m.reply_rate, 3),
            "undelivered": round(m.undelivered_rate, 3),
            "area_codes": m.spread,
            "flagged_by_prefilter": monitor_cohorts.looks_like_a_campaign(cohort, m),
        }
        for cohort, m in built
    ]
    _inspect_audit(
        op,
        org_id,
        what="overview",
        detail={
            "days": days,
            "campaigns": len(campaigns),
            "bodies_shown": sum(len(c["samples"]) for c in campaigns),
        },
    )
    await op.session.commit()
    return {
        "org_id": str(org_id),
        "org_name": org.name,
        "read_only": True,
        "window_days": days,
        "monitor": {
            "level": state.level if state else "normal",
            "score": state.score if state else 0,
            "recommendation": (
                (state.case_file or {}).get(monitor_score.PENDING_ACTION) if state else None
            ),
        },
        "declared_business": await monitor_text.business_context(op.session, org_id),
        "numbers": [
            {"e164": n.e164, "type": n.number_type, "active": n.is_active} for n in numbers
        ],
        "traffic": await monitor_review.traffic_summary(op.session, org_id, since),
        "campaigns": campaigns,
        "coverage": {
            "messages_scanned": built.scanned,
            "truncated": built.truncated,
            "one_off_messages_not_grouped": built.singletons,
        },
        "auto_action": bool(settings.monitor_auto_action),
    }


@ops_router.get("/orgs/{org_id}/messages")
async def inspect_messages(
    org_id: uuid.UUID,
    request: Request,
    op: Reviewer,
    days: int = Query(default=30, ge=1, le=365),
    direction: str | None = Query(default=None, pattern="^(inbound|outbound)$"),
    contains: str | None = Query(default=None, min_length=2, max_length=100),
    to: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=_INSPECT_MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """The raw message log, paged. What "reviewing it myself" actually requires.

    `contains` is a plain substring over the body. It is NOT the template fingerprint: the
    cohort id is derived from the shingles a settled cohort SHARES, so it cannot be recomputed
    from one message's text, and a filter that claimed to match it would silently return the
    wrong rows. To walk a specific campaign, read its samples from `/inspect` and search on a
    distinctive phrase from them.
    """
    if await op.session.get(Org, org_id) is None:
        raise NotFoundError("Organization not found")
    await check_step_up(
        request, op.session, op.user, kind="recent_2fa", action="monitor_inspect_account"
    )
    set_org_context(op.session, org_id)
    where = [Message.org_id == org_id, Message.created_at >= _now() - timedelta(days=days)]
    if direction:
        where.append(Message.direction == direction)
    if to:
        where.append(Message.to_e164 == to)
    if contains:
        # ESCAPED, with an explicit escape character. An operator searching for a scam's
        # "50% off_now" would otherwise have the _ and % read as LIKE wildcards and get back a
        # different set of messages than the one they asked for - and, reading a message log to
        # decide whether to pause a business, believe they had seen everything that matched.
        pattern = contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(Message.body.ilike(f"%{pattern}%", escape="\\"))

    total = (
        await op.session.execute(sa.select(sa.func.count(Message.id)).where(*where))
    ).scalar_one()
    rows = (
        (
            await op.session.execute(
                sa.select(Message)
                .where(*where)
                .order_by(Message.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    _inspect_audit(
        op,
        org_id,
        what="messages",
        detail={
            "days": days,
            "direction": direction,
            "contains": contains,
            "to": to,
            "bodies_shown": len(rows),
            "matching": int(total or 0),
            "offset": offset,
        },
    )
    await op.session.commit()
    return {
        "org_id": str(org_id),
        "read_only": True,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "messages": [
            {
                "id": str(m.id),
                "direction": m.direction,
                "from": m.from_e164,
                "to": m.to_e164,
                "body": m.body,
                "status": m.status,
                "moderation_state": m.moderation_state,
                "moderation_reason": m.moderation_reason,
                "at": _iso(m.created_at),
            }
            for m in rows
        ],
    }


class AuditSampleIn(BaseModel):
    #: How many clean accounts to review. Small by default: each one is a real review with real
    #: AI calls, and the figure this produces is a sanity check, not a statistic.
    sample_size: int = Field(default=9, ge=1, le=50)
    days: int = Field(default=7, ge=1, le=90)
    #: Fixing the seed makes a sample reproducible, which is what you want when comparing two
    #: runs of a changed detector over the same accounts.
    seed: int | None = None


@ops_router.post("/audit-sample")
async def audit_sample(payload: AuditSampleIn, request: Request, op: Admin) -> dict:
    """Review a stratified sample of accounts the monitor thinks are CLEAN, and report the misses.

    Every other number on the ops dashboard measures what the monitor caught, and none of them
    can move when it stops seeing something. This is the only one that can: it looks where the
    system says there is nothing to find.

    Admin rather than Reviewer, and not because it is dangerous - it changes nothing about any
    account and records nothing against one. It spends money: `sample_size` thorough reviews,
    each one several AI calls, on accounts nobody reported. That is a budget decision.

    The result lands in `monitor_health` under kind `audit_sample`, written by the service, so
    the miss rate becomes a series next to the canary and the exam rather than a number that
    scrolls past once. No audit row is written into any sampled customer's log - that would tell
    them they were picked.
    """
    from app.services import monitor_review

    report = await monitor_review.audit_sample(
        op.session,
        request.app.state.settings,
        sample_size=payload.sample_size,
        days=payload.days,
        seed=payload.seed,
    )
    await op.session.commit()
    return report
