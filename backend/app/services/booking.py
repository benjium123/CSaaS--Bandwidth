"""Real appointment booking against the business hours stored on an agent profile.

Booking hours are MACHINE-READABLE and live on the profile under
``AgentProfile.extra["booking"]`` (the builder's interview keeps the human-facing
``booking.enabled``/``booking.rules`` next to it)::

    {"enabled": true, "timezone": "America/Chicago", "slot_minutes": 30,
     "lead_minutes": 60, "horizon_days": 14,
     "weekly": {"mon": [["09:00", "17:00"]], ...}}

A CONFIRMED booking is an ``Appointment`` row with a real ``scheduled_for`` (aware UTC),
``status="booked"`` and a human ``raw_when`` like "Tue Oct 7, 10:00 AM CDT". No schema
change is involved: a legacy request row simply keeps ``scheduled_for=None``.

Every entry point defends against the worker racing a person in the calendar, so this
module NEVER trusts the caller's chosen time: it re-derives the slot from the config and
re-checks the database after writing.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError
from app.models import Appointment

#: Calendar day keys, indexed by ``date.weekday()`` (Monday == 0), matching the builder's
#: ``weekly`` object exactly.
_WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Strict HH:MM. Anything else (a stray "9am", a 24-hour typo) is bad data, not a guess.
_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

MIN_SLOT_MINUTES = 10
MAX_SLOT_MINUTES = 240
DEFAULT_SLOT_MINUTES = 30
DEFAULT_LEAD_MINUTES = 0
DEFAULT_HORIZON_DAYS = 14

#: The one sentence every rejected booking answers with, so the worker can recognize a
#: lost race as a lost race.
CONFLICT_MESSAGE = "That time is no longer available"


@dataclass(frozen=True)
class BookingConfig:
    """A validated ``extra["booking"]`` block. Only ``parse_booking`` ever builds one."""

    timezone: str
    tz: ZoneInfo
    slot_minutes: int
    lead_minutes: int
    horizon_days: int
    #: weekday key -> [(start, end), ...] window pairs in local wall-clock time.
    weekly: dict[str, list[tuple[time, time]]]


def parse_booking(profile) -> BookingConfig | None:
    """Return the profile's booking config, or None when it is missing, disabled or in
    any way invalid. This NEVER raises: a malformed block is a workspace that has not set
    booking up, not a 500 for the worker mid-call."""
    if profile is None:
        return None

    raw = (getattr(profile, "extra", None) or {}).get("booking")
    if not isinstance(raw, dict):
        return None
    if raw.get("enabled") is not True:
        return None

    tz_name = raw.get("timezone")
    if not isinstance(tz_name, str) or not tz_name.strip():
        return None
    tz_name = tz_name.strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return None

    slot_minutes = raw.get("slot_minutes", DEFAULT_SLOT_MINUTES)
    if isinstance(slot_minutes, bool) or not isinstance(slot_minutes, int):
        return None
    if not (MIN_SLOT_MINUTES <= slot_minutes <= MAX_SLOT_MINUTES):
        return None

    lead_minutes = raw.get("lead_minutes", DEFAULT_LEAD_MINUTES)
    if isinstance(lead_minutes, bool) or not isinstance(lead_minutes, int) or lead_minutes < 0:
        return None

    horizon_days = raw.get("horizon_days", DEFAULT_HORIZON_DAYS)
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 0:
        return None

    weekly_raw = raw.get("weekly")
    if not isinstance(weekly_raw, dict):
        return None

    weekly: dict[str, list[tuple[time, time]]] = {}
    for key in _WEEKDAY_KEYS:
        windows = weekly_raw.get(key) or []
        if not isinstance(windows, list):
            return None
        parsed_windows: list[tuple[time, time]] = []
        for window in windows:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                return None
            start = _parse_clock(window[0])
            end = _parse_clock(window[1])
            if start is None or end is None or start >= end:
                return None
            parsed_windows.append((start, end))
        weekly[key] = parsed_windows

    return BookingConfig(
        timezone=tz_name,
        tz=tz,
        slot_minutes=slot_minutes,
        lead_minutes=lead_minutes,
        horizon_days=horizon_days,
        weekly=weekly,
    )


def _parse_clock(value) -> time | None:
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.match(value.strip())
    if match is None:
        return None
    return time(int(match.group(1)), int(match.group(2)))


async def free_slots(
    session: AsyncSession,
    org_id: uuid.UUID,
    cfg: BookingConfig,
    *,
    now: datetime,
    limit: int = 5,
    after: datetime | None = None,
    busy: list[tuple[datetime, datetime]] | None = None,
) -> list[datetime]:
    """The next ``limit`` open slot starts for this org, earliest first, as aware UTC.

    Days are walked (in ``cfg.timezone``) from ``max(now + lead, after)`` up to
    ``now + horizon_days``; a slot counts only when it STARTS on the window grid and
    ENDS by the window's end, and only when no existing booked appointment of this org
    falls inside ``[slot, slot + slot_minutes)`` and no ``busy`` interval (a connected
    calendar's busy time, aware UTC ``[start, end)``) overlaps it.
    """
    now_utc = _as_utc(now)
    span = timedelta(minutes=cfg.slot_minutes)

    start_utc = now_utc + timedelta(minutes=cfg.lead_minutes)
    if after is not None:
        after_utc = _as_utc(after)
        if after_utc > start_utc:
            start_utc = after_utc
    horizon_utc = now_utc + timedelta(days=cfg.horizon_days)

    booked = await _booked_starts(session, org_id, start_utc, horizon_utc + span)

    slots: list[datetime] = []
    day = start_utc.astimezone(cfg.tz).date()
    last_day = horizon_utc.astimezone(cfg.tz).date()
    while day <= last_day and len(slots) < limit:
        for window_start, window_end in cfg.weekly.get(_WEEKDAY_KEYS[day.weekday()], []):
            slot_local = datetime.combine(day, window_start, tzinfo=cfg.tz)
            window_end_local = datetime.combine(day, window_end, tzinfo=cfg.tz)
            while slot_local + span <= window_end_local:
                candidate = slot_local.astimezone(timezone.utc)
                slot_local = slot_local + span
                if candidate < start_utc:
                    continue
                if candidate > horizon_utc:
                    break
                if _is_free(booked, candidate, span) and not _overlaps_busy(
                    busy, candidate, span
                ):
                    slots.append(candidate)
                    if len(slots) >= limit:
                        break
            if len(slots) >= limit:
                break
        day = day + timedelta(days=1)
    return slots


async def book_slot(
    session: AsyncSession,
    org_id: uuid.UUID,
    cfg: BookingConfig,
    *,
    start: datetime,
    contact_e164: str,
    call_id: uuid.UUID | None,
    notes: str,
    now: datetime,
    busy: list[tuple[datetime, datetime]] | None = None,
) -> Appointment:
    """Book ``start`` (aware UTC) for this org, or raise ConflictError.

    ``start`` must be one of the slot starts the config itself allows - aligned to the
    window grid, inside a window, inside the lead/horizon window - AND free. After the
    insert we re-check the database: without a unique index, two workers that both read
    "free" can both write, and the second one to notice deletes its own row and reports
    the loss.
    """
    start_utc = _as_utc(start)
    now_utc = _as_utc(now)

    if not _allowed_start(cfg, start_utc, now_utc):
        raise ConflictError(CONFLICT_MESSAGE)
    if _overlaps_busy(busy, start_utc, timedelta(minutes=cfg.slot_minutes)):
        raise ConflictError(CONFLICT_MESSAGE)
    if await _count_in_slot(session, org_id, cfg, start_utc) > 0:
        raise ConflictError(CONFLICT_MESSAGE)

    appt = Appointment(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call_id,
        contact_e164=contact_e164,
        raw_when=human(start_utc, cfg.tz),
        scheduled_for=start_utc,
        notes=notes,
        status="booked",
        created_by="ai",
    )
    session.add(appt)
    await session.flush()

    if await _count_in_slot(session, org_id, cfg, start_utc) > 1:
        await session.delete(appt)
        await session.flush()
        raise ConflictError(CONFLICT_MESSAGE)

    return appt


def human(dt: datetime, tz) -> str:
    """A spoken-friendly label, e.g. "Tue Oct 7, 10:00 AM CDT". ``tz`` may be a ZoneInfo
    or an IANA name."""
    if isinstance(tz, str):
        try:
            tz = ZoneInfo(tz)
        except Exception:
            tz = timezone.utc
    local = _as_utc(dt).astimezone(tz)
    hour = local.strftime("%I").lstrip("0") or "12"
    label = (
        f"{local.strftime('%a')} {local.strftime('%b')} {local.day}, "
        f"{hour}:{local.strftime('%M')} {local.strftime('%p').upper()}"
    )
    zone = local.strftime("%Z")
    if zone:
        label = f"{label} {zone}"
    return label


def _allowed_start(cfg: BookingConfig, start_utc: datetime, now_utc: datetime) -> bool:
    if start_utc < now_utc + timedelta(minutes=cfg.lead_minutes):
        return False
    if start_utc > now_utc + timedelta(days=cfg.horizon_days):
        return False

    local = start_utc.astimezone(cfg.tz)
    if local.second or local.microsecond:
        return False

    minutes = local.hour * 60 + local.minute
    for window_start, window_end in cfg.weekly.get(_WEEKDAY_KEYS[local.weekday()], []):
        w_start = window_start.hour * 60 + window_start.minute
        w_end = window_end.hour * 60 + window_end.minute
        if minutes < w_start or minutes >= w_end:
            continue
        if (minutes - w_start) % cfg.slot_minutes != 0:
            continue
        if minutes + cfg.slot_minutes > w_end:
            continue
        return True
    return False


async def _count_in_slot(
    session: AsyncSession, org_id: uuid.UUID, cfg: BookingConfig, start_utc: datetime
) -> int:
    span = timedelta(minutes=cfg.slot_minutes)
    stmt = (
        sa.select(sa.func.count())
        .select_from(Appointment)
        .where(
            Appointment.org_id == org_id,
            Appointment.status == "booked",
            Appointment.scheduled_for.isnot(None),
            Appointment.scheduled_for >= start_utc,
            Appointment.scheduled_for < start_utc + span,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def _booked_starts(
    session: AsyncSession, org_id: uuid.UUID, start_utc: datetime, end_utc: datetime
) -> list[datetime]:
    stmt = sa.select(Appointment.scheduled_for).where(
        Appointment.org_id == org_id,
        Appointment.status == "booked",
        Appointment.scheduled_for.isnot(None),
        Appointment.scheduled_for >= start_utc,
        Appointment.scheduled_for < end_utc,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_as_utc(row) for row in rows if row is not None]


def _is_free(booked: list[datetime], candidate: datetime, span: timedelta) -> bool:
    end = candidate + span
    for start in booked:
        if candidate <= start < end:
            return False
    return True


def _overlaps_busy(
    busy: list[tuple[datetime, datetime]] | None, candidate: datetime, span: timedelta
) -> bool:
    end = candidate + span
    for busy_start, busy_end in busy or ():
        if _as_utc(busy_start) < end and candidate < _as_utc(busy_end):
            return True
    return False


def _as_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo on DateTime(timezone=True) columns and hands values back naive;
    # everything this module writes is UTC, so a naive value is UTC, not a guess.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
