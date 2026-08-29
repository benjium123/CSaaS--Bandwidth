"""Per-number reputation monitoring (P14 DR-7).

v1 is DERIVED monitoring, no third-party reputation API: everything here is computed from
rows we already have (``messages`` for outcomes, ``org_numbers`` for which numbers exist).
Alerting is one audit-log row, written at most once per (org, number, UTC day) - anything
richer than that is explicitly out of scope for this phase.

Two axes, on purpose kept separate:

  - **delivery rate** answers "does this number's traffic actually land" - it is scoped to
    rows the carrier gave a real DLR outcome for (``delivered``/``failed``), because mixing
    in immediate ``rejected`` sends (which never reached the carrier - bad request, missing
    registration, etc.) would understate a perfectly healthy number's real performance.
  - **spam-class error count** answers "did a carrier ever say this looked like spam",
    scanned across BOTH ``failed`` (DLR) and ``rejected`` (immediate) rows, because a spam
    block can arrive either way depending on the carrier.

The spam-class code lists below are extracted as constants rather than reused from
``providers/bandwidth/errors.py`` / ``providers/telnyx/errors.py``: those modules classify
codes for BREAKER purposes (does this mean the carrier is down, is it worth retrying) - a
different axis from "did the carrier think this looked like spam". A code can be
``invalid_request`` for retry purposes and still be spam-class for reputation purposes.
Sourced from Bandwidth's published messaging error taxonomy (4750 "carrier rejected,
possibly spam"; 4770 "carrier rejected as SPAM" and their documented sibling codes in the
same 4750-4759 / 4770-4779 families - see docs/research/bandwidth.md for the numbering
convention). Treat this list as v1 best-effort: if it under- or over-fires against real
production traffic, refine the constants - that is a data change here, not a design change.

Telnyx is deliberately EMPTY (``TELNYX_SPAM_CLASS_CODES = frozenset()``), not a best guess:
the codes that looked plausible from Telnyx's public docs (40002/40003) turned out to
collide with ``providers/telnyx/errors.py``'s own ``_INVALID_REQUEST`` set (malformed
request, not spam), and B1 (a live Telnyx account) does not exist yet to observe real spam
DLRs against. Populate this from real Telnyx traffic once B1 is live - tracked as
OPEN_ISSUES D29.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import AuditLogEntry, Message, Org, OrgNumber
from app.services import audit

log = structlog.get_logger("reputation")

#: Trailing window per DR-7.
WINDOW_DAYS = 7
#: Alert thresholds per DR-7: delivery < 85% over >= 50 sends, OR any spam-class error.
MIN_VOLUME_FOR_DELIVERY_ALERT = 50
DELIVERY_RATE_ALERT_THRESHOLD = 0.85
ALERT_ACTION = "number.reputation_alert"
ALERT_TARGET_TYPE = "org_number"

# Bandwidth: 4750-class ("carrier rejected, possibly spam") and 4770-class ("carrier
# rejected as SPAM", SHAFT/content violations).
BANDWIDTH_SPAM_CLASS_CODES: frozenset[str] = frozenset(
    {
        "4750", "4751", "4752", "4753", "4754",
        "4770", "4771", "4772", "4773", "4774", "4775",
    }
)

# Telnyx: EMPTY on purpose - see module docstring (OPEN_ISSUES D29). Not populated until
# there is real Telnyx DLR traffic to validate a spam-class list against (B1).
TELNYX_SPAM_CLASS_CODES: frozenset[str] = frozenset()

SPAM_CLASS_CODES_BY_CARRIER: dict[str, frozenset[str]] = {
    "bandwidth": BANDWIDTH_SPAM_CLASS_CODES,
    "telnyx": TELNYX_SPAM_CLASS_CODES,
}

_ALL_CONSIDERED_STATUSES = ("delivered", "failed", "rejected")


@dataclass(frozen=True)
class NumberReputationStats:
    e164: str
    carrier: str
    window_start: datetime
    window_end: datetime
    volume: int
    delivered: int
    failed: int
    rejected: int
    #: None when this number had no carrier-terminal (delivered/failed) outcome in the
    #: window - "no data" must never render as "0% delivery".
    delivery_rate: float | None
    #: Share of `volume` that carried ANY carrier error code (failed or rejected).
    carrier_error_rate: float | None
    spam_class_error_count: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_sqlite(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def _bind(session: AsyncSession, moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if _is_sqlite(session) else moment


async def compute_number_stats(
    session: AsyncSession, org_id: uuid.UUID, now: datetime | None = None
) -> list[NumberReputationStats]:
    """Trailing WINDOW_DAYS per-number stats for every number this org holds - including
    numbers with zero traffic in the window (reported with volume=0 / rates=None), so the
    reputation endpoint never looks like a number silently disappeared.

    Opus review B4: this is SQL aggregates (COUNT ... GROUP BY) only - a trailing-7-day,
    all-numbers scan must never pull individual message rows into Python, that is exactly
    the shape of query that gets expensive as `messages` grows.
    """
    moment = now or _now()
    window_start = moment - timedelta(days=WINDOW_DAYS)

    numbers = list(
        (await session.execute(sa.select(OrgNumber).where(OrgNumber.org_id == org_id)))
        .scalars()
        .all()
    )
    if not numbers:
        return []

    bind_start = _bind(session, window_start)
    bind_end = _bind(session, moment)
    base_filter = (
        Message.org_id == org_id,
        Message.direction == "outbound",
        Message.created_at >= bind_start,
        Message.created_at < bind_end,
    )

    # Per-number, per-status counts.
    status_rows = (
        await session.execute(
            sa.select(Message.from_e164, Message.status, sa.func.count())
            .where(*base_filter, Message.status.in_(_ALL_CONSIDERED_STATUSES))
            .group_by(Message.from_e164, Message.status)
        )
    ).all()
    status_counts: dict[str, dict[str, int]] = {}
    for from_e164, status, count in status_rows:
        status_counts.setdefault(from_e164, {})[status] = int(count)

    # Per-number count of ANY carrier error code (failed or rejected).
    error_rows = (
        await session.execute(
            sa.select(Message.from_e164, sa.func.count())
            .where(
                *base_filter,
                Message.status.in_(_ALL_CONSIDERED_STATUSES),
                Message.error_code.is_not(None),
            )
            .group_by(Message.from_e164)
        )
    ).all()
    error_counts: dict[str, int] = {from_e164: int(count) for from_e164, count in error_rows}

    # Per-number spam-class error count, one grouped query per carrier that actually HAS a
    # non-empty spam-code list (today: bandwidth only - see TELNYX_SPAM_CLASS_CODES). Keyed
    # by `Message.carrier` (the carrier that actually handled that send, which DR-2 failover
    # may differ from the number's registered carrier) so a code is only ever checked
    # against the taxonomy it actually belongs to.
    spam_counts: dict[str, int] = {}
    carriers_present = {n.carrier for n in numbers}
    for carrier_name in carriers_present:
        spam_codes = SPAM_CLASS_CODES_BY_CARRIER.get(carrier_name, frozenset())
        if not spam_codes:
            continue
        rows = (
            await session.execute(
                sa.select(Message.from_e164, sa.func.count())
                .where(
                    *base_filter,
                    Message.carrier == carrier_name,
                    Message.status.in_(_ALL_CONSIDERED_STATUSES),
                    Message.error_code.in_(spam_codes),
                )
                .group_by(Message.from_e164)
            )
        ).all()
        for from_e164, count in rows:
            spam_counts[from_e164] = spam_counts.get(from_e164, 0) + int(count)

    out: list[NumberReputationStats] = []
    for number in numbers:
        counts = status_counts.get(number.e164, {})
        delivered = counts.get("delivered", 0)
        failed = counts.get("failed", 0)
        rejected = counts.get("rejected", 0)
        volume = delivered + failed + rejected
        dlr_terminal = delivered + failed
        delivery_rate = (delivered / dlr_terminal) if dlr_terminal else None
        error_count = error_counts.get(number.e164, 0)
        carrier_error_rate = (error_count / volume) if volume else None
        spam_class_error_count = spam_counts.get(number.e164, 0)
        out.append(
            NumberReputationStats(
                e164=number.e164,
                carrier=number.carrier,
                window_start=window_start,
                window_end=moment,
                volume=volume,
                delivered=delivered,
                failed=failed,
                rejected=rejected,
                delivery_rate=delivery_rate,
                carrier_error_rate=carrier_error_rate,
                spam_class_error_count=spam_class_error_count,
            )
        )
    return out


async def _already_alerted_today(
    session: AsyncSession, org_id: uuid.UUID, e164: str, moment: datetime
) -> bool:
    day_start = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    existing = (
        await session.execute(
            sa.select(AuditLogEntry.id).where(
                AuditLogEntry.org_id == org_id,
                AuditLogEntry.action == ALERT_ACTION,
                AuditLogEntry.target_id == e164,
                AuditLogEntry.created_at >= _bind(session, day_start),
                AuditLogEntry.created_at < _bind(session, day_end),
            )
        )
    ).first()
    return existing is not None


def _breaches(stats: NumberReputationStats) -> bool:
    # Opus review: the volume gate must use the SAME denominator as the rate itself
    # (delivered + failed - carrier-terminal rows) - NOT total volume, which also counts
    # `rejected` sends that never reached the carrier and would let an org pad its way
    # past the floor with a stack of immediate rejections while its real delivery sample
    # stays tiny.
    carrier_terminal = stats.delivered + stats.failed
    delivery_breach = (
        stats.delivery_rate is not None
        and carrier_terminal >= MIN_VOLUME_FOR_DELIVERY_ALERT
        and stats.delivery_rate < DELIVERY_RATE_ALERT_THRESHOLD
    )
    return delivery_breach or stats.spam_class_error_count > 0


async def check_reputation(
    session: AsyncSession, org_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Per DR-7: write ONE audit row per (org, number, UTC day) when a number's trailing-
    7-day stats cross a threshold. Idempotent per number per day - CHECKED against the
    audit log, never assumed, so a sweeper ticking every ~60s cannot spam it. Commits once.
    Returns the number of new alert rows written.
    """
    moment = now or _now()
    set_org_context(session, org_id)
    stats = await compute_number_stats(session, org_id, moment)

    alerts = 0
    for s in stats:
        if not _breaches(s):
            continue
        if await _already_alerted_today(session, org_id, s.e164, moment):
            continue
        row = audit.record(
            session,
            org_id,
            action=ALERT_ACTION,
            target_type=ALERT_TARGET_TYPE,
            target_id=s.e164,
            detail={
                "carrier": s.carrier,
                "window_days": WINDOW_DAYS,
                "volume": s.volume,
                "delivered": s.delivered,
                "failed": s.failed,
                "rejected": s.rejected,
                "delivery_rate": s.delivery_rate,
                "spam_class_error_count": s.spam_class_error_count,
            },
        )
        # TimestampMixin's default stamps REAL wall-clock time, which only coincides with
        # `moment` when the caller used the real clock (the sweeper's normal case). Setting
        # it explicitly keeps the "one alert per UTC day" check self-consistent with
        # whatever `now` this call actually used - otherwise a caller that ever passes an
        # explicit historical `now` (a backfill, a test) would compare two different clocks
        # against each other in `_already_alerted_today`.
        row.created_at = moment
        log.warning(
            "number_reputation_alert",
            org_id=str(org_id),
            e164=s.e164,
            carrier=s.carrier,
            delivery_rate=s.delivery_rate,
            spam_class_error_count=s.spam_class_error_count,
        )
        alerts += 1
    await session.commit()
    return alerts


async def reputation_tick(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Sweeper entry point. Same per-org-commit discipline as usage.rollup_day /
    routing_exec.routing_tick: one org's failure must never roll back another's already-
    committed alerts in the same pass (check_reputation commits per org internally).
    """
    moment = now or _now()
    org_ids = list(
        (await session.execute(sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})))
        .scalars()
        .all()
    )
    counts = {"orgs": 0, "alerts": 0}
    for org_id in org_ids:
        counts["alerts"] += await check_reputation(session, org_id, moment)
        counts["orgs"] += 1
    return counts
