"""Real appointment booking on the workspace's own calendar.

Covers the pure booking service (parse_booking / free_slots / book_slot / human) and the
two worker seams (GET /agent/availability, POST /agent/appointments/book), reusing the
same worker-auth fixtures as tests/test_agent_tools.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db.base import set_org_context
from app.errors import ConflictError
from app.models import AgentProfile, Appointment
from app.services import booking as booking_svc
from tests.test_agent_seams import _place_call, worker_headers, worker_token
from tests.test_agent_tools import app_with_agent  # noqa: F401 - reused fixture

OUR = "+12145550100"
THEIRS = "+19725550199"

#: A Monday, 07:00 America/Chicago (CDT). Fixed so slot arithmetic is deterministic.
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_ALWAYS_OPEN = {key: [["00:00", "23:59"]] for key in _WEEKDAYS}


def _weekly(**days) -> dict:
    base = {key: [] for key in _WEEKDAYS}
    base.update(days)
    return base


def _booking_config(**overrides) -> dict:
    data = {
        "enabled": True,
        "timezone": "America/Chicago",
        "slot_minutes": 30,
        "lead_minutes": 60,
        "horizon_days": 14,
        "weekly": _weekly(mon=[["09:00", "17:00"]], tue=[["09:00", "17:00"]]),
    }
    data.update(overrides)
    return data


def _profile(booking: dict | None = None) -> AgentProfile:
    extra = {"booking": booking} if booking is not None else {}
    return AgentProfile(id=uuid.uuid4(), org_id=uuid.uuid4(), name="Test Agent", extra=extra)


def _config(**overrides):
    return booking_svc.parse_booking(_profile(_booking_config(**overrides)))


async def _place(client, email: str, name: str):
    token, org, call_id = await _place_call(client, email, name)
    return token, uuid.UUID(org["id"]), call_id


async def _make_profile(session, org_id: uuid.UUID, booking: dict) -> AgentProfile:
    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Booking Assistant",
        is_default=True,
        extra={"booking": booking},
    )
    session.add(profile)
    await session.commit()
    return profile


# ==================================================================================
# parse_booking
# ==================================================================================
def test_parse_booking_valid():
    cfg = booking_svc.parse_booking(_profile(_booking_config()))
    assert cfg is not None
    assert cfg.timezone == "America/Chicago"
    assert cfg.slot_minutes == 30
    assert cfg.lead_minutes == 60
    assert cfg.horizon_days == 14
    assert cfg.weekly["mon"] == [(time(9, 0), time(17, 0))]
    assert cfg.weekly["wed"] == []


def test_parse_booking_missing_profile_or_block_is_none():
    assert booking_svc.parse_booking(None) is None
    assert booking_svc.parse_booking(_profile()) is None


def test_parse_booking_disabled_is_none():
    assert booking_svc.parse_booking(_profile(_booking_config(enabled=False))) is None


def test_parse_booking_bad_timezone_is_none():
    profile = _profile(_booking_config(timezone="Mars/Olympus"))
    assert booking_svc.parse_booking(profile) is None


def test_parse_booking_bad_clock_is_none():
    profile = _profile(_booking_config(weekly=_weekly(mon=[["9am", "17:00"]])))
    assert booking_svc.parse_booking(profile) is None


def test_parse_booking_bad_slot_minutes_is_none():
    assert booking_svc.parse_booking(_profile(_booking_config(slot_minutes=5))) is None
    assert booking_svc.parse_booking(_profile(_booking_config(slot_minutes=600))) is None


# ==================================================================================
# human
# ==================================================================================
def test_human_labels_in_profile_timezone():
    dt = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    assert booking_svc.human(dt, ZoneInfo("America/Chicago")) == "Mon Oct 5, 9:00 AM CDT"
    # An IANA name works too.
    assert booking_svc.human(dt, "America/Chicago") == "Mon Oct 5, 9:00 AM CDT"


# ==================================================================================
# free_slots
# ==================================================================================
async def test_free_slots_respects_lead_time_and_windows(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk10@example.com", "Org BK10")
    set_org_context(session, org_id)
    cfg = _config(weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    # NOW is 07:00 CDT; lead is 60 min so the first candidate is 08:00 CDT. The Monday
    # window opens at 09:00 CDT == 14:00 UTC.
    slots = await booking_svc.free_slots(session, org_id, cfg, now=NOW, limit=5)
    assert slots == [
        datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc),
    ]


async def test_free_slots_lead_time_pushes_first_slot(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk11@example.com", "Org BK11")
    set_org_context(session, org_id)
    cfg = _config(lead_minutes=300, weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    # 07:00 CDT + 5h = 12:00 CDT == 17:00 UTC; everything before it is too soon.
    slots = await booking_svc.free_slots(session, org_id, cfg, now=NOW, limit=1)
    assert slots == [datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)]


async def test_free_slots_slot_must_end_inside_window(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk12@example.com", "Org BK12")
    set_org_context(session, org_id)
    # 09:00-09:45: only the 09:00 slot fits (ends 09:30); 09:30 would end 10:00.
    cfg = _config(weekly=_weekly(mon=[["09:00", "09:45"]]))
    assert cfg is not None

    slots = await booking_svc.free_slots(session, org_id, cfg, now=NOW, limit=1)
    assert slots == [datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)]


async def test_free_slots_skips_booked_appointments(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk13@example.com", "Org BK13")
    set_org_context(session, org_id)
    session.add(
        Appointment(
            id=uuid.uuid4(),
            org_id=org_id,
            contact_e164=THEIRS,
            raw_when="Mon Oct 5, 9:30 AM CDT",
            scheduled_for=datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc),
            notes="",
            status="booked",
            created_by="ai",
        )
    )
    await session.commit()

    cfg = _config(weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None
    slots = await booking_svc.free_slots(session, org_id, cfg, now=NOW, limit=3)
    assert datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc) not in slots
    assert slots[0] == datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    assert slots[1] == datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


async def test_free_slots_respects_horizon(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk14@example.com", "Org BK14")
    set_org_context(session, org_id)
    cfg = _config(horizon_days=1, weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    slots = await booking_svc.free_slots(session, org_id, cfg, now=NOW, limit=50)
    assert slots
    limit_utc = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    for slot in slots:
        assert slot < limit_utc
        assert slot.date() == datetime(2026, 10, 5, tzinfo=timezone.utc).date()


# ==================================================================================
# book_slot
# ==================================================================================
async def test_book_slot_books_a_free_slot(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk20@example.com", "Org BK20")
    set_org_context(session, org_id)
    cfg = _config(weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    start = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)  # 09:00 CDT
    appt = await booking_svc.book_slot(
        session,
        org_id,
        cfg,
        start=start,
        contact_e164=THEIRS,
        call_id=None,
        notes="intro call",
        now=NOW,
    )
    assert appt.status == "booked"
    assert appt.created_by == "ai"
    assert appt.scheduled_for == start
    assert appt.contact_e164 == THEIRS
    assert appt.raw_when == "Mon Oct 5, 9:00 AM CDT"


async def test_book_slot_misaligned_start_conflicts(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk21@example.com", "Org BK21")
    set_org_context(session, org_id)
    cfg = _config(weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    start = datetime(2026, 10, 5, 14, 15, tzinfo=timezone.utc)  # 09:15 CDT - off grid
    with pytest.raises(ConflictError):
        await booking_svc.book_slot(
            session,
            org_id,
            cfg,
            start=start,
            contact_e164=THEIRS,
            call_id=None,
            notes="",
            now=NOW,
        )


async def test_book_slot_outside_window_conflicts(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk22@example.com", "Org BK22")
    set_org_context(session, org_id)
    cfg = _config(weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    start = datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)  # 18:00 CDT - after close
    with pytest.raises(ConflictError):
        await booking_svc.book_slot(
            session,
            org_id,
            cfg,
            start=start,
            contact_e164=THEIRS,
            call_id=None,
            notes="",
            now=NOW,
        )


async def test_book_slot_second_booking_same_slot_conflicts(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "bk23@example.com", "Org BK23")
    set_org_context(session, org_id)
    cfg = _config(weekly=_weekly(mon=[["09:00", "17:00"]]))
    assert cfg is not None

    start = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    await booking_svc.book_slot(
        session,
        org_id,
        cfg,
        start=start,
        contact_e164=THEIRS,
        call_id=None,
        notes="",
        now=NOW,
    )
    await session.commit()

    with pytest.raises(ConflictError):
        await booking_svc.book_slot(
            session,
            org_id,
            cfg,
            start=start,
            contact_e164=THEIRS,
            call_id=None,
            notes="",
            now=NOW,
        )


# ==================================================================================
# Routes
# ==================================================================================
async def test_availability_requires_worker_auth(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "bkr1@example.com", "Org BKR1")
    r = await client.get(f"/api/v1/agent/availability?call_id={call_id}")
    assert r.status_code == 401


async def test_availability_disabled_without_booking_config(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "bkr2@example.com", "Org BKR2")
    r = await client.get(
        f"/api/v1/agent/availability?call_id={call_id}",
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "timezone": "", "slots": []}


async def test_availability_enabled_lists_slots(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "bkr3@example.com", "Org BKR3")
    org_id = uuid.UUID(org["id"])
    await _make_profile(session, org_id, _booking_config(lead_minutes=0, weekly=_ALWAYS_OPEN))

    r = await client.get(
        f"/api/v1/agent/availability?call_id={call_id}",
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["timezone"] == "America/Chicago"
    assert body["slots"]
    for slot in body["slots"]:
        assert slot["label"]
        assert datetime.fromisoformat(slot["start"]).tzinfo is not None


async def test_book_appointment_slot_success_and_taken(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "bkr4@example.com", "Org BKR4")
    org_id = uuid.UUID(org["id"])
    await _make_profile(session, org_id, _booking_config(lead_minutes=0, weekly=_ALWAYS_OPEN))

    start = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    r = await client.post(
        "/api/v1/agent/appointments/book",
        json={"call_id": call_id, "start": start.isoformat(), "notes": "intro"},
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["booked"] is True
    assert uuid.UUID(body["appointment_id"])
    assert body["label"] == booking_svc.human(start, ZoneInfo("America/Chicago"))

    again = await client.post(
        "/api/v1/agent/appointments/book",
        json={"call_id": call_id, "start": start.isoformat(), "notes": ""},
        headers=worker_headers(worker_token()),
    )
    assert again.status_code == 409, again.text
    assert "no longer available" in again.text


async def test_book_appointment_slot_when_disabled_is_422(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "bkr5@example.com", "Org BKR5")
    r = await client.post(
        "/api/v1/agent/appointments/book",
        json={"call_id": call_id, "start": "2026-10-05T14:00:00Z", "notes": ""},
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 422
