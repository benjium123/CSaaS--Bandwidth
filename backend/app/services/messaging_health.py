"""P41: workspace-level daily messaging health rollup, breach notifications and ops view."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    AuditLogEntry,
    ConsentEvent,
    Message,
    MessageEvent,
    Notification,
    Org,
    OrgMembership,
    OrgMessagingDaily,
    Role,
)
from app.services import notifications as notifications_svc
from app.services import reputation as reputation_svc

log = structlog.get_logger("messaging_health")

#: Platform-level defaults. These move to settings after the first ops pass.
WINDOW_DAYS = 7
MIN_VOLUME = 100
DELIVERY_WARN, DELIVERY_CRITICAL = 0.90, 0.80
SPAM_WARN, SPAM_CRITICAL = 0.01, 0.03
OPT_OUT_WARN, OPT_OUT_CRITICAL = 0.03, 0.05
ROLLUP_TICK_INTERVAL_SECONDS = 3600
NOTIFY_ROLES = ("owner", "admin")

_FAILED_STATUSES = ("failed", "rejected")


def _as_utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _failed_class_count(failure_class: str):
    """COUNT of terminal failures carrying one class. NULL and "unknown" match none of
    these on purpose - they are counted in `failed` only, never guessed into a bucket."""
    return (
        sa.func.count(Message.id)
        .filter(
            sa.and_(
                Message.status.in_(_FAILED_STATUSES),
                Message.failure_class == failure_class,
            )
        )
        .label(f"failed_{failure_class}")
    )


async def _existing_row(
    session: AsyncSession, org_id: uuid.UUID, day: date, carrier: str
) -> OrgMessagingDaily | None:
    return (
        await session.execute(
            sa.select(OrgMessagingDaily).where(
                OrgMessagingDaily.org_id == org_id,
                OrgMessagingDaily.period_date == day,
                OrgMessagingDaily.carrier == carrier,
            )
        )
    ).scalar_one_or_none()


async def rollup_day(session: AsyncSession, org_id: uuid.UUID, day: date) -> int:
    """Recompute every carrier row for one (org, day) from source tables and upsert.

    Rows are counted, never estimated. A receipt that arrives late changes the source row
    and the next rollup corrects the counts, because the unique key makes a second rollup
    of the same day collide instead of doubling it.
    """
    set_org_context(session, org_id)
    start, end = _day_bounds(day)

    outbound_rows = (
        await session.execute(
            sa.select(
                Message.carrier,
                sa.func.count(Message.id).label("sent"),
                sa.func.count(Message.id)
                .filter(Message.status == "delivered")
                .label("delivered"),
                sa.func.count(Message.id)
                .filter(Message.status.in_(_FAILED_STATUSES))
                .label("failed"),
                _failed_class_count("spam_blocked"),
                _failed_class_count("carrier_rejected"),
                _failed_class_count("invalid_destination"),
                _failed_class_count("opted_out"),
            )
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.created_at >= start,
                Message.created_at < end,
            )
            .group_by(Message.carrier)
        )
    ).all()

    inbound_rows = (
        await session.execute(
            sa.select(
                Message.carrier,
                sa.func.count(Message.id).label("inbound"),
            )
            .where(
                Message.org_id == org_id,
                Message.direction == "inbound",
                Message.created_at >= start,
                Message.created_at < end,
            )
            .group_by(Message.carrier)
        )
    ).all()

    # A consent event belongs to the carrier of the message that triggered it; a manual or
    # API event has no message, so it lands under "unknown" rather than being dropped.
    consent_carrier = sa.func.coalesce(Message.carrier, "unknown")
    consent_rows = (
        await session.execute(
            sa.select(
                consent_carrier.label("carrier"),
                sa.func.count(ConsentEvent.id)
                .filter(ConsentEvent.event == "opt_out")
                .label("opt_outs"),
                sa.func.count(ConsentEvent.id)
                .filter(ConsentEvent.event == "help_request")
                .label("help_requests"),
                sa.func.count(ConsentEvent.id)
                .filter(ConsentEvent.event == "opt_in")
                .label("opt_ins"),
            )
            .select_from(ConsentEvent)
            .outerjoin(Message, ConsentEvent.message_id == Message.id)
            .where(
                ConsentEvent.org_id == org_id,
                ConsentEvent.created_at >= start,
                ConsentEvent.created_at < end,
                ConsentEvent.event.in_(("opt_out", "help_request", "opt_in")),
            )
            .group_by(consent_carrier)
        )
    ).all()

    defaults = {
        "sent": 0,
        "delivered": 0,
        "failed": 0,
        "failed_spam_blocked": 0,
        "failed_carrier_rejected": 0,
        "failed_invalid_destination": 0,
        "failed_opted_out": 0,
        "inbound": 0,
        "opt_outs": 0,
        "help_requests": 0,
        "opt_ins": 0,
    }
    carrier_counts: dict[str, dict[str, int]] = {}

    for carrier, sent, delivered, failed, spam, rejected, invalid, opted in outbound_rows:
        carrier_counts.setdefault(carrier, defaults.copy()).update(
            {
                "sent": int(sent),
                "delivered": int(delivered),
                "failed": int(failed),
                "failed_spam_blocked": int(spam),
                "failed_carrier_rejected": int(rejected),
                "failed_invalid_destination": int(invalid),
                "failed_opted_out": int(opted),
            }
        )

    for carrier, inbound in inbound_rows:
        carrier_counts.setdefault(carrier, defaults.copy())["inbound"] = int(inbound)

    for carrier, opt_outs, help_requests, opt_ins in consent_rows:
        key = carrier or "unknown"
        carrier_counts.setdefault(key, defaults.copy()).update(
            {
                "opt_outs": int(opt_outs),
                "help_requests": int(help_requests),
                "opt_ins": int(opt_ins),
            }
        )

    touched = 0
    for carrier, counts in carrier_counts.items():
        existing = await _existing_row(session, org_id, day, carrier)
        if existing is not None:
            for field, value in counts.items():
                setattr(existing, field, value)
            touched += 1
            continue

        fresh = OrgMessagingDaily(org_id=org_id, period_date=day, carrier=carrier, **counts)
        try:
            # SAVEPOINT, not the caller's transaction: two rollups of the same day race on
            # the unique key, and losing that race must cost a re-read, not the caller's
            # work (same pattern as services/plans.py::ensure_period).
            async with session.begin_nested():
                session.add(fresh)
                await session.flush()
            touched += 1
        except IntegrityError:
            log.debug(
                "messaging_health.rollup_race",
                org_id=str(org_id),
                day=day.isoformat(),
                carrier=carrier,
            )
            set_org_context(session, org_id)
            existing = await _existing_row(session, org_id, day, carrier)
            if existing is None:
                raise
            for field, value in counts.items():
                setattr(existing, field, value)
            touched += 1

    await session.flush()
    return touched


async def rollup_tick(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Sweeper entry point. Rolls up every org with source activity in the last two days.

    Today AND yesterday on every pass: receipts arrive late, so yesterday's row is not
    final at midnight.
    """
    moment = _as_utc(now)
    cutoff = moment - timedelta(days=2)
    today = moment.date()
    yesterday = today - timedelta(days=1)

    org_ids: set[uuid.UUID] = set()
    # Unscoped and JUSTIFIED: the tick runs across every workspace, exactly like
    # reputation.reputation_tick - there is no single caller org to scope to.
    org_ids.update(
        (
            await session.execute(
                sa.select(sa.distinct(Message.org_id))
                .where(Message.created_at >= cutoff)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    org_ids.update(
        (
            await session.execute(
                sa.select(sa.distinct(ConsentEvent.org_id))
                .where(ConsentEvent.created_at >= cutoff)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )

    counts = {"orgs": 0, "rows": 0}
    for org_id in sorted(org_ids, key=str):
        try:
            touched = await rollup_day(session, org_id, today)
            touched += await rollup_day(session, org_id, yesterday)
            await session.commit()
        except Exception:  # noqa: BLE001 - one org's failure must not abort the whole pass
            log.exception("messaging_health_rollup_org_failed", org_id=str(org_id))
            await session.rollback()
            continue
        counts["orgs"] += 1
        counts["rows"] += touched

    counts["notifications"] = await notify_breaches(session, now=moment)
    return counts


async def health(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    days: int = WINDOW_DAYS,
    now: datetime | None = None,
) -> dict:
    """Derive rates and breach reasons for one workspace from the daily rollup rows.

    No rate is stored: a receipt that lands a day late corrects the counts and the rate
    follows on the next read.
    """
    moment = _as_utc(now)
    today = moment.date()
    start_day = today - timedelta(days=days - 1)

    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(OrgMessagingDaily).where(
                    OrgMessagingDaily.org_id == org_id,
                    OrgMessagingDaily.period_date >= start_day,
                    OrgMessagingDaily.period_date <= today,
                )
            )
        )
        .scalars()
        .all()
    )

    delivered = sum(row.delivered for row in rows)
    failed = sum(row.failed for row in rows)
    opt_outs = sum(row.opt_outs for row in rows)
    failed_spam = sum(row.failed_spam_blocked for row in rows)
    failed_carrier = sum(row.failed_carrier_rejected for row in rows)
    failed_invalid = sum(row.failed_invalid_destination for row in rows)
    failed_opted_out = sum(row.failed_opted_out for row in rows)

    # Carrier-terminal rows only: a message still queued has neither succeeded nor failed
    # yet, and counting it either way would move the rate for a reason nobody can act on.
    volume = delivered + failed
    delivery_rate = delivered / volume if volume else None
    spam_block_rate = failed_spam / volume if volume else None
    opt_out_rate = opt_outs / delivered if delivered else None
    failed_unknown = max(
        failed - (failed_spam + failed_carrier + failed_invalid + failed_opted_out), 0
    )

    reasons: list[str] = []
    level = "ok"
    if volume == 0:
        level = "no_data"
    elif volume < MIN_VOLUME:
        # Below the floor nothing can breach: a handful of texts says nothing about a
        # workspace's reputation. Reasons stay empty.
        level = "ok"
    else:
        critical_hit = False
        warn_hit = False

        if delivery_rate is not None and delivery_rate < DELIVERY_WARN:
            crossed = DELIVERY_CRITICAL if delivery_rate < DELIVERY_CRITICAL else DELIVERY_WARN
            if delivery_rate < DELIVERY_CRITICAL:
                critical_hit = True
            else:
                warn_hit = True
            reasons.append(
                f"Delivery rate {delivery_rate:.1%} over the last {days} days on "
                f"{volume} texts. Below {crossed:.0%} carriers start filtering your traffic."
            )

        if spam_block_rate is not None and spam_block_rate > SPAM_WARN:
            crossed = SPAM_CRITICAL if spam_block_rate > SPAM_CRITICAL else SPAM_WARN
            if spam_block_rate > SPAM_CRITICAL:
                critical_hit = True
            else:
                warn_hit = True
            reasons.append(
                f"Spam blocks {spam_block_rate:.1%} ({failed_spam} texts). "
                f"Above {crossed:.0%} carriers may suspend the number."
            )

        if opt_out_rate is not None and opt_out_rate > OPT_OUT_WARN:
            crossed = OPT_OUT_CRITICAL if opt_out_rate > OPT_OUT_CRITICAL else OPT_OUT_WARN
            if opt_out_rate > OPT_OUT_CRITICAL:
                critical_hit = True
            else:
                warn_hit = True
            reasons.append(
                f"Opt-outs {opt_out_rate:.1%} of delivered texts. "
                f"Above {crossed:.0%} is a sign the list did not consent."
            )

        if critical_hit:
            level = "critical"
        elif warn_hit:
            level = "warn"

    # A per-number reputation breach (P14) raises the workspace to warn, never to
    # critical: one bad number is not the same as the whole workspace being filtered.
    if level != "no_data":
        window_start_dt, _ = _day_bounds(start_day)
        window_end_dt = window_start_dt + timedelta(days=days)
        alert_id = (
            await session.execute(
                sa.select(AuditLogEntry.id)
                .where(
                    AuditLogEntry.org_id == org_id,
                    AuditLogEntry.action == reputation_svc.ALERT_ACTION,
                    AuditLogEntry.created_at >= window_start_dt,
                    AuditLogEntry.created_at < window_end_dt,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if alert_id is not None:
            if level != "critical":
                level = "warn"
            reasons.append("A number triggered a reputation alert in this window.")

    return {
        "window_start": start_day,
        "window_end": today,
        "volume": volume,
        "delivery_rate": delivery_rate,
        "spam_block_rate": spam_block_rate,
        "opt_out_rate": opt_out_rate,
        "failed_by_class": {
            "spam_blocked": failed_spam,
            "carrier_rejected": failed_carrier,
            "invalid_destination": failed_invalid,
            "opted_out": failed_opted_out,
            "unknown": failed_unknown,
        },
        "level": level,
        "reasons": reasons,
        "thresholds": {
            "delivery_warn": DELIVERY_WARN,
            "delivery_critical": DELIVERY_CRITICAL,
            "spam_warn": SPAM_WARN,
            "spam_critical": SPAM_CRITICAL,
            "opt_out_warn": OPT_OUT_WARN,
            "opt_out_critical": OPT_OUT_CRITICAL,
            "min_volume": float(MIN_VOLUME),
        },
    }


async def _org_ids_in_window(
    session: AsyncSession, start_day: date, today: date
) -> list[uuid.UUID]:
    """Unscoped and JUSTIFIED: both callers below are all-workspace passes (the sweeper's
    notifier and the platform ops table), not a request made on behalf of one org."""
    return list(
        (
            await session.execute(
                sa.select(sa.distinct(OrgMessagingDaily.org_id))
                .where(
                    OrgMessagingDaily.period_date >= start_day,
                    OrgMessagingDaily.period_date <= today,
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


async def notify_breaches(session: AsyncSession, now: datetime | None = None) -> int:
    """One in-app notification per (org, level, UTC day) for owner/admin members.

    The dedupe key is what guarantees that: the tick may run every hour, and the people
    who can actually act on it are told once.
    """
    moment = _as_utc(now)
    today = moment.date()
    start_day = today - timedelta(days=WINDOW_DAYS - 1)

    created_count = 0
    for org_id in await _org_ids_in_window(session, start_day, today):
        try:
            summary = await health(session, org_id, now=moment)
            if summary["level"] not in ("warn", "critical"):
                continue

            set_org_context(session, org_id)
            user_ids = (
                (
                    await session.execute(
                        sa.select(sa.distinct(OrgMembership.user_id))
                        .select_from(OrgMembership)
                        .join(Role, OrgMembership.role_id == Role.id)
                        .where(
                            OrgMembership.org_id == org_id,
                            Role.name.in_(NOTIFY_ROLES),
                        )
                    )
                )
                .scalars()
                .all()
            )

            reasons = summary["reasons"]
            first_reason = reasons[0] if reasons else "Messaging health needs attention."
            prefix = "Critical: " if summary["level"] == "critical" else "Warning: "
            dedupe_key = f"messaging_health:{org_id}:{summary['level']}:{today.isoformat()}"

            for user_id in user_ids:
                created = await notifications_svc.create(
                    session,
                    org_id,
                    user_id=user_id,
                    kind="messaging_health",
                    body=f"{prefix}{first_reason}",
                    dedupe_key=dedupe_key,
                )
                if created is not None:
                    created_count += 1

            await session.commit()
        except Exception:  # noqa: BLE001 - one org's failure must not stop the whole pass
            log.exception("messaging_health_notify_org_failed", org_id=str(org_id))
            await session.rollback()
            continue

    return created_count


async def platform_rows(
    session: AsyncSession,
    *,
    days: int = WINDOW_DAYS,
    now: datetime | None = None,
) -> list[dict]:
    """Ops view: every workspace with a rollup row in the window, worst first."""
    moment = _as_utc(now)
    today = moment.date()
    start_day = today - timedelta(days=days - 1)
    window_start_dt, _ = _day_bounds(start_day)
    window_end_dt = window_start_dt + timedelta(days=days)

    org_ids = await _org_ids_in_window(session, start_day, today)
    if not org_ids:
        return []

    # Unscoped and JUSTIFIED: a platform operator's table spans every workspace.
    org_names = dict(
        (
            await session.execute(
                sa.select(Org.id, Org.name)
                .where(Org.id.in_(org_ids))
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    )

    breaches = dict(
        (
            await session.execute(
                sa.select(
                    Notification.org_id,
                    sa.func.min(Notification.created_at).label("first_breached_at"),
                )
                .where(
                    Notification.org_id.in_(org_ids),
                    Notification.kind == "messaging_health",
                    Notification.created_at >= window_start_dt,
                    Notification.created_at < window_end_dt,
                )
                .group_by(Notification.org_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    )

    rows: list[dict] = []
    for org_id in org_ids:
        summary = await health(session, org_id, days=days, now=moment)
        rows.append(
            {
                "org_id": org_id,
                "org_name": org_names.get(org_id, ""),
                **summary,
                "first_breached_at": breaches.get(org_id),
            }
        )

    level_rank = {"critical": 0, "warn": 1, "ok": 2, "no_data": 3}
    rows.sort(
        key=lambda r: (
            level_rank.get(r["level"], 4),
            # None last inside a level: a rate nobody could compute is not "the worst".
            r["delivery_rate"] if r["delivery_rate"] is not None else 2.0,
        )
    )
    return rows


async def receipts_check(session: AsyncSession) -> list[dict]:
    """Per carrier: the last delivery receipt actually ingested, and how many in 24h.

    "Receipts are configured" is then proven by data rather than by a dashboard
    screenshot - a carrier whose webhook URL points elsewhere shows None here.
    """
    carriers = ("bandwidth", "telnyx", "twilio", "plivo", "signalwire")
    receipt_types = ("message-delivered", "message-failed")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # Unscoped and JUSTIFIED: a carrier's webhook health is a platform fact, not an org's
    # - scoping it to one workspace would hide an outage every time.
    latest = dict(
        (
            await session.execute(
                sa.select(
                    MessageEvent.carrier,
                    sa.func.max(MessageEvent.event_time).label("last_receipt_at"),
                )
                .where(
                    MessageEvent.carrier.in_(carriers),
                    MessageEvent.event_type.in_(receipt_types),
                )
                .group_by(MessageEvent.carrier)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    )

    counts = dict(
        (
            await session.execute(
                sa.select(
                    MessageEvent.carrier,
                    sa.func.count(MessageEvent.id).label("receipts_24h"),
                )
                .where(
                    MessageEvent.carrier.in_(carriers),
                    MessageEvent.event_type.in_(receipt_types),
                    MessageEvent.event_time >= cutoff,
                )
                .group_by(MessageEvent.carrier)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    )

    return [
        {
            "carrier": carrier,
            "last_receipt_at": latest.get(carrier),
            "receipts_24h": int(counts.get(carrier, 0)),
        }
        for carrier in carriers
    ]
