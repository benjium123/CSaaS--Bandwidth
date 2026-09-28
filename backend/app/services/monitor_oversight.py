"""Monitoring oversight: the jobs that watch the whole platform rather than one message.

    rescore_tick     hourly   re-derive every workspace's score, so signals that age out of
                              the window lower it (today a score only moves when a NEW
                              signal arrives, so an old burst kept an account on "watch")
    overlap_tick     hourly   the same recipients worked from several workspaces in 24h -
                              one list, many accounts - becomes a shared_recipients signal
    digest_tick      daily    one email to ops admins: new pauses, recommendations waiting
                              for a human, and the highest scores
    spot_check_tick  weekly   a few random active workspaces queued for a thorough human
                              look, so review is not only ever triggered by the score

None of these pauses, restricts or deletes anything (operator policy: such actions are for
humans; the confirmed-evidence auto-pause in monitor_score.recompute is the only exception,
and the hourly rescore reaches it only on signals that would already have triggered it).
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    Call,
    Message,
    MonitorHealth,
    MonitorSignal,
    Org,
    OrgMonitoring,
    OrgNumber,
    SecurityAlert,
)

log = structlog.get_logger("monitor_oversight")

_UNSCOPED = {ALLOW_UNSCOPED_KEY: True}
SHARED_KIND = "shared_recipients"
SPOT_CHECK_KIND = "monitor_spot_check"
DIGEST_EVERY = timedelta(hours=23, minutes=55)
SPOT_CHECK_EVERY = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _due(session: AsyncSession, kind: str, every: timedelta) -> bool:
    from app.services import monitor_exam

    return await monitor_exam.due(session, kind, every)


def _record_run(session: AsyncSession, kind: str, detail: dict) -> None:
    session.add(MonitorHealth(id=uuid.uuid4(), kind=kind, passed=True, detail=detail))


# ------------------------------------------------------------------------------------ 2
async def rescore_tick(session: AsyncSession, settings: Settings) -> int:
    """Recompute every monitored workspace. Returns how many changed score or level."""
    from app.services import monitor_score

    ids = (
        (await session.execute(sa.select(OrgMonitoring.org_id).execution_options(**_UNSCOPED)))
        .scalars()
        .all()
    )
    changed = 0
    for org_id in ids:
        try:
            state = await monitor_score.get_state(session, org_id, create=False)
            if state is None:
                continue
            before = (state.score, state.level)
            await monitor_score.recompute(session, settings, state, repeat_recommendation=False)
            if (state.score, state.level) != before:
                changed += 1
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("monitor_rescore_failed", org_id=str(org_id))
    return changed


# ------------------------------------------------------------------------------------ 3
async def overlap_tick(session: AsyncSession, settings: Settings, *, hours: int = 24) -> int:
    """Signal workspaces whose recipients many other workspaces also reached. Returns the
    number of signals added (at most one per workspace per day)."""
    from app.services import monitor_score

    since = _now() - timedelta(hours=hours)
    texts = sa.select(Message.org_id.label("org_id"), Message.to_e164.label("peer")).where(
        Message.direction == "outbound", Message.created_at >= since
    )
    calls = sa.select(Call.org_id.label("org_id"), Call.contact_e164.label("peer")).where(
        Call.direction == "outbound", Call.created_at >= since
    )
    pairs = sa.union(texts, calls).subquery()
    # Numbers on the platform itself (one workspace calling another, or a test between two
    # of your own) and short codes such as 911 are not a shared list.
    own = sa.select(OrgNumber.e164)
    shared = (
        sa.select(pairs.c.peer)
        .where(pairs.c.peer.like("+%"), sa.func.length(pairs.c.peer) >= 9, pairs.c.peer.not_in(own))
        .group_by(pairs.c.peer)
        .having(
            sa.func.count(sa.distinct(pairs.c.org_id)) >= settings.monitor_overlap_min_workspaces
        )
    ).subquery()
    rows = (
        await session.execute(
            sa.select(pairs.c.org_id, sa.func.count(sa.distinct(pairs.c.peer)))
            .where(pairs.c.peer.in_(sa.select(shared.c.peer)))
            .group_by(pairs.c.org_id)
            .having(
                sa.func.count(sa.distinct(pairs.c.peer)) >= settings.monitor_overlap_min_recipients
            )
            .execution_options(**_UNSCOPED)
        )
    ).all()
    added = 0
    for org_id, count in rows:
        recent = (
            await session.execute(
                sa.select(MonitorSignal.id)
                .where(
                    MonitorSignal.org_id == org_id,
                    MonitorSignal.kind == SHARED_KIND,
                    MonitorSignal.created_at >= _now() - timedelta(hours=24),
                )
                .limit(1)
                .execution_options(**_UNSCOPED)
            )
        ).first()
        if recent is not None:
            continue
        await monitor_score.add_signal(
            session,
            settings,
            org_id,
            SHARED_KIND,
            f"{count} recipients in the last {hours}h were also contacted by at least "
            f"{settings.monitor_overlap_min_workspaces} other workspaces",
            detail={"shared_recipients": int(count), "hours": hours},
        )
        await session.commit()
        added += 1
    return added


# ------------------------------------------------------------------------------------ 4
async def digest_tick(session: AsyncSession, settings: Settings) -> bool | None:
    """Once a day: email ops admins what needs a human. None when not due; False when due
    but there was nothing to say (the run is still recorded, so it waits another day)."""
    from app.services import break_glass, mailer

    if not await _due(session, "digest", DIGEST_EVERY):
        return None
    since = _now() - timedelta(hours=24)
    names = dict(
        (await session.execute(sa.select(Org.id, Org.name).execution_options(**_UNSCOPED))).all()
    )
    paused = (
        (
            await session.execute(
                sa.select(OrgMonitoring)
                .where(OrgMonitoring.level == "paused", OrgMonitoring.paused_at >= since)
                .execution_options(**_UNSCOPED)
            )
        )
        .scalars()
        .all()
    )
    waiting = (
        (
            await session.execute(
                sa.select(SecurityAlert)
                .where(
                    SecurityAlert.kind == "monitor_action_recommended",
                    SecurityAlert.status == "open",
                )
                .execution_options(**_UNSCOPED)
            )
        )
        .scalars()
        .all()
    )
    top = (
        (
            await session.execute(
                sa.select(OrgMonitoring)
                .where(OrgMonitoring.score > 0)
                .order_by(OrgMonitoring.score.desc())
                .limit(10)
                .execution_options(**_UNSCOPED)
            )
        )
        .scalars()
        .all()
    )
    detail = {"paused": len(paused), "recommendations": len(waiting), "scored": len(top)}
    _record_run(session, "digest", detail)
    await session.commit()
    if not (paused or waiting or top):
        return False
    lines = ["Ringlite monitoring - last 24 hours", ""]
    lines.append(f"New pauses: {len(paused)}")
    for s in paused:
        lines.append(f"  - {names.get(s.org_id, s.org_id)}: {s.paused_reason or 'paused'}")
    lines += ["", f"Recommendations waiting for a human: {len(waiting)}"]
    for a in waiting:
        d = a.detail or {}
        lines.append(
            f"  - {names.get(a.org_id, a.org_id)}: {d.get('current', '?')} -> "
            f"{d.get('recommended', '?')} (score {d.get('score', '?')})"
        )
    lines += ["", "Highest risk scores:"]
    for s in top:
        lines.append(f"  - {names.get(s.org_id, s.org_id)}: {s.score} ({s.level})")
    lines += ["", "Review them in Switchboard > Monitoring."]
    admins = await break_glass.admin_emails(session)
    if not admins:
        return False
    await mailer.send(settings, admins, "Ringlite monitoring digest", "\n".join(lines))
    return True


# ------------------------------------------------------------------------------------ 5
async def spot_check_tick(
    session: AsyncSession, settings: Settings, *, rng: random.Random | None = None
) -> int | None:
    """Once a week: queue a few random workspaces that were active in the last week for a
    thorough human review (a SecurityAlert in the Alerts tab). None when not due."""
    if not await _due(session, "spot_check", SPOT_CHECK_EVERY):
        return None
    since = _now() - timedelta(days=7)
    active = set(
        (
            await session.execute(
                sa.union(
                    sa.select(Message.org_id).where(
                        Message.direction == "outbound", Message.created_at >= since
                    ),
                    sa.select(Call.org_id).where(
                        Call.direction == "outbound", Call.created_at >= since
                    ),
                ).execution_options(**_UNSCOPED)
            )
        ).scalars()
    )
    already = set(
        (
            await session.execute(
                sa.select(SecurityAlert.org_id)
                .where(SecurityAlert.kind == SPOT_CHECK_KIND, SecurityAlert.status == "open")
                .execution_options(**_UNSCOPED)
            )
        ).scalars()
    )
    live = set(
        (
            await session.execute(
                sa.select(Org.id).where(Org.is_active.is_(True)).execution_options(**_UNSCOPED)
            )
        ).scalars()
    )
    pool = sorted((active & live) - already, key=str)
    picked = (rng or random.SystemRandom()).sample(
        pool, min(settings.monitor_spot_check_count, len(pool))
    )
    for org_id in picked:
        set_org_context(session, org_id)
        texts = (
            await session.execute(
                sa.select(sa.func.count(Message.id)).where(
                    Message.org_id == org_id,
                    Message.direction == "outbound",
                    Message.created_at >= since,
                )
            )
        ).scalar_one()
        calls = (
            await session.execute(
                sa.select(sa.func.count(Call.id)).where(
                    Call.org_id == org_id, Call.direction == "outbound", Call.created_at >= since
                )
            )
        ).scalar_one()
        state = (
            await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
        ).scalar_one_or_none()
        session.add(
            SecurityAlert(
                id=uuid.uuid4(),
                kind=SPOT_CHECK_KIND,
                org_id=org_id,
                status="open",
                detail={
                    "reason": "Weekly random spot check: not triggered by the risk score.",
                    "texts_7d": int(texts),
                    "calls_7d": int(calls),
                    "score": state.score if state else 0,
                    "level": state.level if state else "normal",
                },
            )
        )
    _record_run(session, "spot_check", {"picked": [str(o) for o in picked], "pool": len(pool)})
    await session.commit()
    return len(picked)
