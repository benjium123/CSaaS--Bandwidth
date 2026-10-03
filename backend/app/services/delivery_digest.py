"""D2: delivery reporting.

One report matters by default: the platform OPS digest, built once a day from the P41
per-workspace rollup and mailed to operators, who are the only people who can act on a
carrier or a workspace going bad. The CUSTOMER digest is opt-in - a platform operator
switches it on for a workspace from the ops console - and a customer otherwise hears from
us only when their own delivery rate breaches (services/messaging_health.notify_breaches).

Counts always come from ``OrgMessagingDaily`` rather than the raw message tables, so a
receipt that arrives late corrects them on the next read. A customer-facing mail never
names a carrier: an owner reading "Blocked as spam" can act on it, whereas a carrier's
name is our plumbing, not theirs. The ops digest is the one place carrier names belong -
its audience is us.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ValidationFailedError
from app.models import (
    Message,
    Notification,
    Org,
    OrgMembership,
    OrgMessagingDaily,
    PlatformSetting,
    ReportSchedule,
    Role,
    User,
)
from app.services import email_delivery, mailer, messaging_health

log = structlog.get_logger("delivery_digest")

DEFAULT_TZ = "America/Chicago"
DEFAULT_HOUR = 8
TICK_INTERVAL_SECONDS = 3600
MAX_RECIPIENTS = 20
TOP_NUMBERS = 10
#: Delivery-floor thresholds: below CRITICAL a workspace is red, below LOW it is amber.
OPS_CRITICAL_RATE = 0.80
OPS_LOW_RATE = 0.90
OPS_MIN_VOLUME = 20
#: PlatformSetting key holding {"date": "YYYY-MM-DD"} - the last Chicago-local day the ops
#: digest went out, so a second sweeper pass the same day is a no-op.
OPS_SETTING_KEY = "delivery_digest_ops_last"

FAILURE_LABELS = {
    "failed_spam_blocked": "Blocked as spam",
    "failed_carrier_rejected": "Rejected by the carrier",
    "failed_invalid_destination": "Invalid or unreachable number",
    "failed_opted_out": "Recipient opted out",
}

#: Roles that receive the customer digest when the workspace has not named anyone.
DEFAULT_RECIPIENT_ROLES = ("owner", "admin")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _as_utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _as_aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - any failure just means "not a usable zone"
        return False
    return True


def _params_enabled(params) -> bool:  # noqa: ANN001
    """The one rule for "is this workspace opted in": the stored bool, or nothing."""
    return isinstance(params, dict) and params.get("enabled") is True


def _active_app_name() -> str:
    """Product name for a mail built outside a request (the sweeper, the ops test-send).
    Read from the active settings exactly as the other request-less services do; a
    deployment with none pinned just gets the default the mailer itself uses."""
    try:
        from app.config import get_active_settings

        settings = get_active_settings()
    except Exception:  # noqa: BLE001 - no pinned settings must never fail a digest
        settings = None
    return getattr(settings, "app_name", None) or "Ringlite"


def _format_number(e164: str | None) -> str:
    digits = re.sub(r"\D", "", e164 or "")
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:11]}"
    return e164 or ""


# ==================================================================================
# Schedule
# ==================================================================================
def schedule_view(row: ReportSchedule | None) -> dict:
    """The schedule as the API and the tick both read it. The customer digest is OPT-IN:
    with no row, or with no ``enabled`` in the stored params, it is OFF. An invalid stored
    tz falls back to DEFAULT_TZ rather than silently disabling an opted-in send."""
    params = (row.params or {}) if row is not None else {}
    if not isinstance(params, dict):
        params = {}

    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        enabled = False

    hour = params.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or not (0 <= hour <= 23):
        hour = DEFAULT_HOUR

    tz = params.get("tz")
    if not isinstance(tz, str) or not _valid_tz(tz):
        tz = DEFAULT_TZ

    recipients = list(row.recipients or []) if row is not None else []
    last_sent_at = _as_aware(row.last_sent_at) if row is not None else None
    return {
        "enabled": enabled,
        "hour": hour,
        "tz": tz,
        "recipients": recipients,
        "last_sent_at": last_sent_at.isoformat() if last_sent_at is not None else None,
    }


async def get_schedule(session: AsyncSession, org_id: uuid.UUID) -> ReportSchedule | None:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(ReportSchedule)
            .where(
                ReportSchedule.org_id == org_id,
                ReportSchedule.report == "delivery",
            )
            .order_by(ReportSchedule.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def save_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    enabled: bool,
    hour: int,
    tz: str,
    recipients: list[str],
    user_id: uuid.UUID | None,
) -> ReportSchedule:
    """Validate and upsert the delivery schedule. Recipients are lower-cased and
    de-duplicated; a bad value is a human-readable ValidationFailedError, never a 500."""
    if isinstance(hour, bool) or not isinstance(hour, int) or not (0 <= hour <= 23):
        raise ValidationFailedError("Pick an hour between 0 and 23.")
    if not isinstance(tz, str) or not _valid_tz(tz):
        raise ValidationFailedError("Pick a valid time zone, like America/Chicago.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in recipients or []:
        addr = (raw or "").strip().lower()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            raise ValidationFailedError(f"{raw} is not a valid email address.")
        if addr in seen:
            continue
        seen.add(addr)
        cleaned.append(addr)
    if len(cleaned) > MAX_RECIPIENTS:
        raise ValidationFailedError(f"At most {MAX_RECIPIENTS} recipients are allowed.")

    row = await get_schedule(session, org_id)
    params = {"enabled": bool(enabled), "hour": int(hour), "tz": tz}
    if row is None:
        row = ReportSchedule(
            id=uuid.uuid4(),
            org_id=org_id,
            report="delivery",
            params=params,
            cadence="daily",
            recipients=cleaned,
            created_by=user_id,
        )
        session.add(row)
    else:
        row.params = params
        row.recipients = cleaned
        row.cadence = "daily"
    await session.commit()
    return row


async def default_recipients(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Owner and admin emails - the same query shape as billing_alerts._recipients, because
    "who can act on this workspace" is answered in exactly one place per surface."""
    rows = (
        await session.execute(
            sa.select(User.id, User.email)
            .join(OrgMembership, OrgMembership.user_id == User.id)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.org_id == org_id, Role.name.in_(DEFAULT_RECIPIENT_ROLES))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    emails: dict[str, None] = {}
    for _uid, email in rows:
        addr = (email or "").strip().lower()
        if addr:
            emails[addr] = None
    return sorted(emails)


# ==================================================================================
# The customer digest itself
# ==================================================================================
async def build(session: AsyncSession, org_id: uuid.UUID, day: date) -> dict | None:
    """One day's digest for one workspace, or None when nothing was sent that day."""
    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(OrgMessagingDaily).where(
                    OrgMessagingDaily.org_id == org_id,
                    OrgMessagingDaily.period_date == day,
                )
            )
        )
        .scalars()
        .all()
    )

    sent = sum(int(row.sent) for row in rows)
    if sent == 0:
        return None

    delivered = sum(int(row.delivered) for row in rows)
    failed = sum(int(row.failed) for row in rows)
    failed_by_class = {
        "failed_spam_blocked": sum(int(row.failed_spam_blocked) for row in rows),
        "failed_carrier_rejected": sum(int(row.failed_carrier_rejected) for row in rows),
        "failed_invalid_destination": sum(int(row.failed_invalid_destination) for row in rows),
        "failed_opted_out": sum(int(row.failed_opted_out) for row in rows),
    }
    # NULL and "unknown" land in `failed` only; subtracting them must never go negative.
    other_failed = max(failed - sum(failed_by_class.values()), 0)
    volume = delivered + failed
    rate = delivered / volume if volume else None
    pending = max(sent - delivered - failed, 0)

    start, end = _day_bounds(day)
    number_rows = (
        await session.execute(
            sa.select(
                Message.from_e164,
                sa.func.count(Message.id).label("sent"),
                sa.func.count(Message.id)
                .filter(Message.status == "delivered")
                .label("delivered"),
                sa.func.count(Message.id)
                .filter(Message.status.in_(("failed", "rejected")))
                .label("failed"),
            )
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.created_at >= start,
                Message.created_at < end,
            )
            .group_by(Message.from_e164)
            .order_by(sa.desc(sa.func.count(Message.id)))
            .limit(TOP_NUMBERS)
        )
    ).all()
    numbers = [
        {
            "number": number,
            "sent": int(sent_count),
            "delivered": int(delivered_count),
            "failed": int(failed_count),
        }
        for number, sent_count, delivered_count, failed_count in number_rows
    ]

    trend_start = day - timedelta(days=6)
    trend_rows = (
        (
            await session.execute(
                sa.select(OrgMessagingDaily).where(
                    OrgMessagingDaily.org_id == org_id,
                    OrgMessagingDaily.period_date >= trend_start,
                    OrgMessagingDaily.period_date <= day,
                )
            )
        )
        .scalars()
        .all()
    )
    per_day: dict[date, dict[str, int]] = {}
    for row in trend_rows:
        bucket = per_day.setdefault(
            row.period_date, {"sent": 0, "delivered": 0, "failed": 0}
        )
        bucket["sent"] += int(row.sent)
        bucket["delivered"] += int(row.delivered)
        bucket["failed"] += int(row.failed)
    trend: list[dict] = []
    for offset in range(7):
        point = trend_start + timedelta(days=offset)
        counts = per_day.get(point, {"sent": 0, "delivered": 0, "failed": 0})
        trend.append({"date": point.isoformat(), **counts})

    summary = await messaging_health.health(
        session,
        org_id,
        now=datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc),
    )

    return {
        "day": day.isoformat(),
        "sent": sent,
        "delivered": delivered,
        "failed": failed,
        "failed_by_class": failed_by_class,
        "other_failed": other_failed,
        "rate": rate,
        "pending": pending,
        "numbers": numbers,
        "trend": trend,
        "level": summary["level"],
    }


def render(digest: dict, *, app_name: str, day: date, base_url: str) -> tuple[str, str]:
    """(subject, plain-text body). Carrier names never appear here - the mailer wraps the
    plain body in the branded HTML, so this string is the only content we control."""
    sent = int(digest["sent"])
    rate = digest["rate"]
    rate_text = f"{rate:.0%}" if rate is not None else "n/a"
    subject = f"{app_name}: texts on {day:%a %b %d} \u2014 {sent} sent, {rate_text} delivered"

    lines: list[str] = [
        f"{sent} texts sent on {day:%a %b %d}: {int(digest['delivered'])} delivered, "
        f"{int(digest['failed'])} failed, {rate_text} delivery rate."
    ]
    pending = int(digest.get("pending", 0))
    if pending:
        lines.append(f"{pending} texts are still waiting on a carrier confirmation.")

    failure_lines: list[str] = []
    by_class = digest.get("failed_by_class", {})
    for key, label in FAILURE_LABELS.items():
        count = int(by_class.get(key, 0))
        if count:
            failure_lines.append(f"{label}: {count}")
    other = int(digest.get("other_failed", 0))
    if other:
        failure_lines.append(f"Other: {other}")
    if failure_lines:
        lines.append("")
        lines.append("Why texts failed")
        lines.extend(failure_lines)

    numbers = digest.get("numbers", [])
    if numbers:
        lines.append("")
        lines.append("Your busiest numbers")
        for row in numbers:
            lines.append(
                f"{_format_number(row['number'])}: {int(row['sent'])} sent, "
                f"{int(row['delivered'])} delivered, {int(row['failed'])} failed"
            )

    lines.append("")
    lines.append("Last 7 days")
    for point in digest.get("trend", []):
        lines.append(
            f"{point['date']}: {int(point['sent'])} sent, {int(point['delivered'])} delivered"
        )

    if digest.get("level") in ("warn", "critical"):
        lines.append("")
        lines.append("Delivery needs attention: see Analytics")

    base = (base_url or "").rstrip("/")
    lines.append("")
    lines.append(f"Open {base}/settings/messaging to change or turn off this email.")
    return subject, "\n".join(lines)


# ==================================================================================
# The operator digest
# ==================================================================================
async def _digest_enabled_org_names(session: AsyncSession) -> list[str]:
    """Workspaces with the customer digest switched ON, by name. No row (or a row without
    an explicit bool) means OFF."""
    # Unscoped and JUSTIFIED: which workspaces have the daily email on is a platform-wide
    # question for the ops report, not one caller's org.
    rows = (
        await session.execute(
            sa.select(ReportSchedule.org_id, ReportSchedule.params)
            .where(ReportSchedule.report == "delivery")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    enabled_ids: list[uuid.UUID] = []
    for org_id, params in rows:
        if _params_enabled(params) and org_id not in enabled_ids:
            enabled_ids.append(org_id)
    if not enabled_ids:
        return []
    # Unscoped and JUSTIFIED: names for the platform-wide list above.
    names = dict(
        (
            await session.execute(
                sa.select(Org.id, Org.name)
                .where(Org.id.in_(enabled_ids))
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    )
    return sorted(names.get(org_id) or str(org_id) for org_id in enabled_ids)


async def build_ops_digest(
    session: AsyncSession, day: date, *, now: datetime | None = None
) -> tuple[str, str]:
    """The operator report for one day as (subject, body).

    Takes no settings - the sweeper's tick and an operator's test-send share this one
    implementation, and the product name comes from the active settings rather than a
    parameter nobody else needs. The subject carries the BOTTOM LINE, because that is
    what gets read first: either "all clear" or how many things need a human.
    """
    app_name = _active_app_name()
    start, end = _day_bounds(day)
    moment = _as_utc(now) if now is not None else end

    # Unscoped and JUSTIFIED: the ops digest is a platform-wide report across every
    # workspace at once.
    per_org = (
        await session.execute(
            sa.select(
                OrgMessagingDaily.org_id,
                sa.func.coalesce(sa.func.sum(OrgMessagingDaily.sent), 0).label("sent"),
                sa.func.coalesce(sa.func.sum(OrgMessagingDaily.delivered), 0).label("delivered"),
                sa.func.coalesce(sa.func.sum(OrgMessagingDaily.failed), 0).label("failed"),
                sa.func.coalesce(sa.func.sum(OrgMessagingDaily.failed_spam_blocked), 0).label(
                    "spam_blocked"
                ),
                sa.func.coalesce(
                    sa.func.sum(OrgMessagingDaily.failed_carrier_rejected), 0
                ).label("carrier_rejected"),
                sa.func.coalesce(
                    sa.func.sum(OrgMessagingDaily.failed_invalid_destination), 0
                ).label("invalid_destination"),
                sa.func.coalesce(sa.func.sum(OrgMessagingDaily.failed_opted_out), 0).label(
                    "opted_out"
                ),
            )
            .where(OrgMessagingDaily.period_date == day)
            .group_by(OrgMessagingDaily.org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()

    org_ids = [row[0] for row in per_org]
    names: dict = {}
    warned: set = set()
    if org_ids:
        # Unscoped and JUSTIFIED: names for the platform-wide table below.
        names = dict(
            (
                await session.execute(
                    sa.select(Org.id, Org.name)
                    .where(Org.id.in_(org_ids))
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            ).all()
        )
        # "Customer warned" = a messaging-health bell reached this workspace in the 7 days
        # ending `day`. Unscoped and JUSTIFIED: it answers that question for EVERY
        # workspace in the table, not for one caller's org.
        warned = set(
            (
                await session.execute(
                    sa.select(sa.distinct(Notification.org_id))
                    .where(
                        Notification.org_id.in_(org_ids),
                        Notification.kind == "messaging_health",
                        Notification.created_at >= start - timedelta(days=6),
                        Notification.created_at < end,
                    )
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            )
            .scalars()
            .all()
        )

    rows: list[dict] = []
    for (
        org_id,
        sent,
        delivered,
        failed,
        spam_blocked,
        carrier_rejected,
        invalid_destination,
        opted_out,
    ) in per_org:
        sent, delivered, failed = int(sent), int(delivered), int(failed)
        volume = delivered + failed
        classes = {
            "failed_spam_blocked": int(spam_blocked),
            "failed_carrier_rejected": int(carrier_rejected),
            "failed_invalid_destination": int(invalid_destination),
            "failed_opted_out": int(opted_out),
        }
        top_key = max(classes, key=lambda key: classes[key]) if any(classes.values()) else None
        rows.append(
            {
                "org_id": org_id,
                "name": names.get(org_id) or "",
                "sent": sent,
                "delivered": delivered,
                "failed": failed,
                "rate": delivered / volume if volume else None,
                "top_failure_label": FAILURE_LABELS[top_key] if top_key else "Other",
                "warned": org_id in warned,
            }
        )
    rows.sort(key=lambda item: item["sent"], reverse=True)

    # Which workspaces above the volume floor are failing, worst first.
    flagged: list[dict] = []
    for row in rows:
        if row["sent"] < OPS_MIN_VOLUME or row["rate"] is None:
            continue
        if row["rate"] < OPS_CRITICAL_RATE:
            flagged.append(row)
        elif row["rate"] < OPS_LOW_RATE:
            flagged.append(row)
    flagged.sort(key=lambda item: item["rate"])

    # Which carriers are sending without receipts coming back. A carrier with no traffic
    # yesterday is quiet because nobody used it, which is not an outage.
    carrier_traffic = dict(
        (
            await session.execute(
                sa.select(Message.carrier, sa.func.count(Message.id))
                .where(
                    Message.direction == "outbound",
                    Message.created_at >= start,
                    Message.created_at < end,
                )
                .group_by(Message.carrier)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    )
    receipts = await messaging_health.receipts_check(session)
    silent: list[str] = []
    for receipt in receipts:
        if int(carrier_traffic.get(receipt["carrier"], 0)) <= 0:
            continue
        last = _as_aware(receipt["last_receipt_at"])
        if last is None or (moment - last) > timedelta(hours=24):
            silent.append(receipt["carrier"])
    silent.sort()

    total_sent = sum(row["sent"] for row in rows)
    total_delivered = sum(row["delivered"] for row in rows)
    total_failed = sum(row["failed"] for row in rows)
    total_volume = total_delivered + total_failed
    rate_text = f"{total_delivered / total_volume:.0%}" if total_volume else "n/a"
    active_orgs = sum(1 for row in rows if row["sent"] > 0)

    if total_sent == 0:
        subject = f"{app_name}: ops report {day:%b %d} \u2014 all clear"
        bottom = ["BOTTOM LINE: No texts sent yesterday."]
    elif not flagged and not silent:
        subject = f"{app_name}: ops report {day:%b %d} \u2014 all clear"
        bottom = [
            f"BOTTOM LINE: All clear. {total_sent} texts, {rate_text} delivered across "
            f"{active_orgs} workspaces."
        ]
    else:
        subject = (
            f"{app_name}: ops report {day:%b %d} \u2014 ACTION NEEDED "
            f"({len(flagged) + len(silent)})"
        )
        bottom = ["BOTTOM LINE: Action needed."]
        for row in flagged:
            bottom.append(
                f"- {row['name'] or row['org_id']}: {row['rate']:.0%} delivered "
                f"({row['failed']} of {row['sent']} failed), mostly "
                f"{row['top_failure_label']}. Customer warned: "
                f"{'yes' if row['warned'] else 'no'}."
            )
        for carrier in silent:
            bottom.append(
                f"- {carrier}: no delivery receipts in 24 h while sending \u2014 check the "
                "webhook."
            )

    lines: list[str] = list(bottom)
    lines.append("")
    lines.append(
        f"{total_sent} texts sent across all workspaces: {total_delivered} delivered, "
        f"{total_failed} failed, {rate_text} delivery rate."
    )

    lines.append("")
    lines.append("Busiest workspaces (top 10 by volume)")
    if rows:
        for row in rows[:10]:
            row_rate = f"{row['rate']:.0%}" if row["rate"] is not None else "n/a"
            lines.append(
                f"{row['name'] or row['org_id']}: {row['sent']} sent, "
                f"{row['delivered']} delivered, {row_rate}"
            )
    else:
        lines.append("None")

    lines.append("")
    lines.append("Workspaces below the delivery floor")
    if flagged:
        for row in flagged:
            lines.append(
                f"{row['name'] or row['org_id']}: {row['rate']:.0%} of {row['sent']} texts"
            )
    else:
        lines.append("None")

    lines.append("")
    lines.append("Carrier receipts")
    for receipt in receipts:
        last = _as_aware(receipt["last_receipt_at"])
        if last is None or (moment - last) > timedelta(hours=24):
            status = "NO RECEIPTS 24h"
        else:
            status = f"{receipt['receipts_24h']} in the last 24h"
        lines.append(f"{receipt['carrier']}: {status}")

    lines.append("")
    lines.append("Customer daily emails ON")
    enabled_names = await _digest_enabled_org_names(session)
    if enabled_names:
        lines.extend(enabled_names)
    else:
        lines.append("none")

    return subject, "\n".join(lines)


# ==================================================================================
# Ticks
# ==================================================================================
async def digest_tick(
    session: AsyncSession, settings, now: datetime | None = None
) -> dict[str, int]:  # noqa: ANN001
    """Sweeper entry point: one digest email per OPTED-IN workspace per local day.

    The send marker is committed BEFORE the send (also for zero-volume days) so an hourly
    pass cannot re-mail the same day and a failed send cannot loop forever.
    """
    moment = _as_utc(now)
    today = moment.date()
    counts = {"sent": 0, "empty": 0, "skipped": 0, "no_recipients": 0, "errors": 0}

    org_ids = await messaging_health._org_ids_in_window(
        session, today - timedelta(days=2), today
    )
    for org_id in org_ids:
        try:
            row = await get_schedule(session, org_id)
            view = schedule_view(row)
            if not view["enabled"]:
                counts["skipped"] += 1
                continue

            tz = ZoneInfo(view["tz"])
            local = moment.astimezone(tz)
            if local.hour < view["hour"]:
                counts["skipped"] += 1
                continue

            if row is not None and row.last_sent_at is not None:
                last = _as_aware(row.last_sent_at)
                if last.astimezone(tz).date() == local.date():
                    counts["skipped"] += 1
                    continue

            day = local.date() - timedelta(days=1)
            if row is None:
                row = ReportSchedule(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    report="delivery",
                    cadence="daily",
                    params={"enabled": True, "hour": DEFAULT_HOUR, "tz": DEFAULT_TZ},
                    recipients=[],
                )
                session.add(row)
                await session.flush()

            recipients = list(row.recipients or [])
            row.last_sent_at = moment
            await session.commit()

            digest = await build(session, org_id, day)
            if digest is None:
                counts["empty"] += 1
                continue

            if not recipients:
                recipients = await default_recipients(session, org_id)
            if not recipients:
                counts["no_recipients"] += 1
                continue

            subject, body = render(
                digest,
                app_name=settings.app_name,
                day=day,
                base_url=(getattr(settings, "public_web_url", "") or ""),
            )
            await mailer.send(settings, recipients, subject, body)
            counts["sent"] += 1
        except Exception:  # noqa: BLE001 - one org's failure must not abort the whole pass
            log.exception("delivery_digest.org_failed", org_id=str(org_id))
            await session.rollback()
            counts["errors"] += 1
            continue
    return counts


async def ops_digest_tick(
    session: AsyncSession, settings, now: datetime | None = None
) -> bool:  # noqa: ANN001
    """Once per Chicago-local day after DEFAULT_HOUR: the platform-wide digest to ops."""
    moment = _as_utc(now)
    tz = ZoneInfo(DEFAULT_TZ)
    local = moment.astimezone(tz)
    if local.hour < DEFAULT_HOUR:
        return False
    today_local = local.date()

    # Unscoped and JUSTIFIED: PlatformSetting is platform-wide (not tenant data) and this
    # tick is an all-workspace pass, not a request made on behalf of one org.
    setting = (
        await session.execute(
            sa.select(PlatformSetting)
            .where(PlatformSetting.key == OPS_SETTING_KEY)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    if value.get("date") == today_local.isoformat():
        return False

    marker = {"date": today_local.isoformat()}
    if setting is None:
        session.add(PlatformSetting(key=OPS_SETTING_KEY, value=marker))
    else:
        setting.value = marker
    # Commit the marker BEFORE building or sending, so a second pass the same day is a
    # no-op and a failing mail cannot re-try every hour.
    await session.commit()

    day = today_local - timedelta(days=1)
    subject, body = await build_ops_digest(session, day, now=moment)

    recipients = await email_delivery._operator_emails()
    if not recipients:
        return False
    await mailer.send(settings, recipients, subject, body)
    return True
