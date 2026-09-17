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
        if report.assessment["credible"] and report.assessment["category"] != "legitimate":
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
