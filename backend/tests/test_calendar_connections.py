"""Google Calendar connections: connect, read busy time, write bookings (2026-10-07).

Covers ``calendar_google`` against an ``httpx.MockTransport``, the ``calendars`` service
and the /api/v1/calendar routes, plus the two worker seams that read the profile's
connected calendar (GET /agent/availability, POST /agent/appointments/book). Fixtures and
helpers mirror tests/test_booking.py.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.config import Settings
from app.db.base import set_org_context
from app.errors import ConflictError
from app.models import AgentProfile, Appointment, CalendarConnection
from app.services import booking as booking_svc
from app.services import calendar_google, credentials, oidc
from app.services import calendars as calendars_svc
from tests.conftest import auth_headers, make_settings
from tests.test_agent_seams import _place_call, worker_headers, worker_token
from tests.test_agent_tools import app_with_agent  # noqa: F401 - reused fixture

THEIRS = "+19725550199"

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
_WINDOW_START = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_ALWAYS_OPEN = {key: [["00:00", "23:59"]] for key in _WEEKDAYS}

CAL_OVERRIDES = {
    "public_base_url": "https://ringlite.test",
    "google_calendar_client_id": "cid.apps.googleusercontent.com",
    "google_calendar_client_secret": SecretStr("csecret"),
}


# ==================================================================================
# Helpers + fixtures
# ==================================================================================
def _cal_settings(**overrides) -> Settings:
    base = {
        "credentials_master_key": SecretStr(Fernet.generate_key().decode()),
        **CAL_OVERRIDES,
    }
    base.update(overrides)
    return make_settings(**base)


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


def _config(**overrides):
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Test Agent",
        extra={"booking": _booking_config(**overrides)},
    )
    return booking_svc.parse_booking(profile)


async def _make_profile(session, org_id, booking, *, calendar_connection_id=None):
    block = dict(booking)
    if calendar_connection_id is not None:
        block["calendar_connection_id"] = str(calendar_connection_id)
    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Booking Assistant",
        is_default=True,
        extra={"booking": block},
    )
    session.add(profile)
    await session.commit()
    return profile


async def _place(client, email: str, name: str):
    token, org, call_id = await _place_call(client, email, name)
    return token, uuid.UUID(org["id"]), call_id


def _outcome(response: httpx.Response) -> str:
    return parse_qs(urlparse(response.headers["location"]).query)["calendar"][0]


def _id_token(email: str) -> str:
    claims = {"email": email, "email_verified": True}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class FakeGoogle:
    """A URL-routing stand-in for Google, installed by swapping ``TRANSPORT``."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.time_zone = "America/Chicago"
        self.events: list[dict] = []
        self.event_pages: list[dict] | None = None
        self.scope = "openid email https://www.googleapis.com/auth/calendar.events"
        self.token_status = 200
        self.token_body: dict | None = None
        self.list_status = 200
        self.insert_status = 200
        self._list_calls = 0

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(
            calendar_google, "TRANSPORT", httpx.MockTransport(self.handler)
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url == calendar_google.TOKEN_URL:
            return self._token(request)
        if url == calendar_google.REVOKE_URL:
            return httpx.Response(200, json={})
        if "/calendar/v3/calendars/" in url:
            if request.method == "GET":
                return self._list(request)
            if request.method == "POST":
                return self._insert(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_body is not None or self.token_status != 200:
            body = self.token_body if self.token_body is not None else {"error": "boom"}
            return httpx.Response(self.token_status, json=body)
        form = parse_qs(request.content.decode())
        grant = (form.get("grant_type") or [""])[0]
        if grant == "authorization_code":
            payload = {
                "access_token": "at-1",
                "expires_in": 3600,
                "refresh_token": "rt-1",
                "scope": self.scope,
                "id_token": _id_token("Owner@Example.com"),
            }
            return httpx.Response(200, json=payload)
        if grant == "refresh_token":
            payload = {"access_token": "at-2", "expires_in": 3600, "scope": self.scope}
            return httpx.Response(200, json=payload)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _list(self, _request: httpx.Request) -> httpx.Response:
        if self.list_status != 200:
            return httpx.Response(self.list_status, json={"error": "boom"})
        if self.event_pages is not None:
            index = min(self._list_calls, len(self.event_pages) - 1)
            self._list_calls += 1
            return httpx.Response(200, json=self.event_pages[index])
        return httpx.Response(
            200, json={"timeZone": self.time_zone, "items": self.events}
        )

    def _insert(self, _request: httpx.Request) -> httpx.Response:
        if self.insert_status != 200:
            return httpx.Response(self.insert_status, json={"error": "boom"})
        return httpx.Response(200, json={"id": "evt-1"})


@pytest.fixture
def fake_google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
async def cal_app(app_with_agent):
    """app_with_agent with Google Calendar configured and a fresh Fernet master key."""
    client, application = app_with_agent
    application.state.settings = application.state.settings.model_copy(
        update={
            "credentials_master_key": SecretStr(Fernet.generate_key().decode()),
            **CAL_OVERRIDES,
        }
    )
    yield client, application


async def _connect(client, headers) -> uuid.UUID:
    """Run the full OAuth connect flow and return the single connection's id."""
    start = await client.post("/api/v1/calendar/oauth/google/start", headers=headers)
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]
    callback = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"code": "c1", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    assert _outcome(callback) == "connected"
    listing = await client.get("/api/v1/calendar/connections", headers=headers)
    assert listing.status_code == 200, listing.text
    connections = listing.json()["connections"]
    assert len(connections) == 1
    return uuid.UUID(connections[0]["id"])


# ==================================================================================
# calendar_google unit
# ==================================================================================
def test_authorize_url_points_at_google_with_expected_params():
    url = calendar_google.authorize_url(_cal_settings(), "state-123")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    params = parse_qs(urlparse(url).query)
    assert params["client_id"] == ["cid.apps.googleusercontent.com"]
    assert params["redirect_uri"] == [
        "https://ringlite.test/api/v1/calendar/oauth/google/callback"
    ]
    assert params["response_type"] == ["code"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["state"] == ["state-123"]
    assert "https://www.googleapis.com/auth/calendar.events" in params["scope"][0]


async def test_exchange_code_returns_tokens_and_lowercased_email(fake_google):
    tokens = await calendar_google.exchange_code(_cal_settings(), "c1", now=NOW)
    assert tokens.access_token == "at-1"
    assert tokens.refresh_token == "rt-1"
    assert tokens.email == "owner@example.com"


async def test_exchange_code_without_calendar_scope_fails(fake_google):
    fake_google.scope = "openid email"
    with pytest.raises(calendar_google.GoogleCalendarError):
        await calendar_google.exchange_code(_cal_settings(), "c1", now=NOW)


async def test_refresh_invalid_grant_marks_revoked(fake_google):
    fake_google.token_status = 400
    fake_google.token_body = {"error": "invalid_grant"}
    with pytest.raises(calendar_google.GoogleCalendarError) as exc:
        await calendar_google.refresh(_cal_settings(), "rt-1", now=NOW)
    assert exc.value.revoked is True


async def test_busy_intervals_filters_non_blocking_events(fake_google):
    fake_google.events = [
        {
            "status": "confirmed",
            "start": {"dateTime": "2026-10-06T14:00:00Z"},
            "end": {"dateTime": "2026-10-06T15:00:00Z"},
        },
        {
            "status": "confirmed",
            "transparency": "transparent",
            "start": {"dateTime": "2026-10-06T16:00:00Z"},
            "end": {"dateTime": "2026-10-06T17:00:00Z"},
        },
        {
            "status": "cancelled",
            "start": {"dateTime": "2026-10-06T18:00:00Z"},
            "end": {"dateTime": "2026-10-06T19:00:00Z"},
        },
        {
            "status": "confirmed",
            "attendees": [{"self": True, "responseStatus": "declined"}],
            "start": {"dateTime": "2026-10-06T20:00:00Z"},
            "end": {"dateTime": "2026-10-06T21:00:00Z"},
        },
        {
            "status": "confirmed",
            "start": {"date": "2026-10-06"},
            "end": {"date": "2026-10-07"},
        },
    ]

    intervals = await calendar_google.busy_intervals(
        "at-1", "primary", _WINDOW_START, _WINDOW_END
    )

    assert intervals == [
        (
            datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
        ),
        # 2026-10-06 00:00 CDT -> 2026-10-07 00:00 CDT expressed in UTC.
        (
            datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc),
        ),
    ]


async def test_busy_intervals_follows_page_tokens(fake_google):
    fake_google.event_pages = [
        {
            "timeZone": "America/Chicago",
            "nextPageToken": "p2",
            "items": [
                {
                    "start": {"dateTime": "2026-10-06T14:00:00Z"},
                    "end": {"dateTime": "2026-10-06T15:00:00Z"},
                }
            ],
        },
        {
            "timeZone": "America/Chicago",
            "items": [
                {
                    "start": {"dateTime": "2026-10-06T16:00:00Z"},
                    "end": {"dateTime": "2026-10-06T17:00:00Z"},
                }
            ],
        },
    ]

    intervals = await calendar_google.busy_intervals(
        "at-1", "primary", _WINDOW_START, _WINDOW_END
    )

    assert intervals == [
        (
            datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc),
        ),
    ]
    list_calls = [call for call in fake_google.calls if call.method == "GET"]
    assert len(list_calls) == 2
    assert list_calls[1].url.params.get("pageToken") == "p2"


async def test_busy_intervals_too_many_pages_fails(fake_google):
    fake_google.event_pages = [
        {"timeZone": "America/Chicago", "nextPageToken": f"p{i}", "items": []}
        for i in range(calendar_google.MAX_PAGES + 1)
    ]
    with pytest.raises(calendar_google.GoogleCalendarError):
        await calendar_google.busy_intervals("at-1", "primary", _WINDOW_START, _WINDOW_END)


async def test_busy_intervals_401_marks_revoked(fake_google):
    fake_google.list_status = 401
    with pytest.raises(calendar_google.GoogleCalendarError) as exc:
        await calendar_google.busy_intervals("at-1", "primary", _WINDOW_START, _WINDOW_END)
    assert exc.value.revoked is True


async def test_create_event_returns_id_and_encodes_calendar_id(fake_google):
    event_id = await calendar_google.create_event(
        "at-1",
        "a@b.com",
        start=NOW,
        end=NOW + timedelta(minutes=30),
        summary="Appointment",
        description="booked by ringlite",
    )
    assert event_id == "evt-1"
    posts = [
        call
        for call in fake_google.calls
        if call.method == "POST" and "calendar/v3" in str(call.url)
    ]
    assert len(posts) == 1
    assert "/calendars/a%40b.com/events" in str(posts[0].url)


# ==================================================================================
# booking service (busy-aware)
# ==================================================================================
async def test_free_slots_excludes_busy_and_keeps_touching_slots(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "cal09@example.com", "Org CAL09")
    set_org_context(session, org_id)
    cfg = _config(lead_minutes=0, weekly=_ALWAYS_OPEN)
    assert cfg is not None

    baseline = await booking_svc.free_slots(session, org_id, cfg, now=NOW, limit=5)
    assert len(baseline) == 5
    first, second = baseline[0], baseline[1]
    span = timedelta(minutes=cfg.slot_minutes)

    slots = await booking_svc.free_slots(
        session, org_id, cfg, now=NOW, limit=5, busy=[(first, first + span)]
    )
    assert first not in slots
    assert slots[0] == second

    # A busy interval ending exactly at a slot start does not exclude that slot.
    touching = await booking_svc.free_slots(
        session, org_id, cfg, now=NOW, limit=5, busy=[(first, second)]
    )
    assert second in touching


async def test_book_slot_conflicts_when_busy_overlaps_start(app_with_agent, session):
    client, _app = app_with_agent
    _token, org_id, _call = await _place(client, "cal10@example.com", "Org CAL10")
    set_org_context(session, org_id)
    cfg = _config(lead_minutes=0, weekly=_ALWAYS_OPEN)
    assert cfg is not None

    start = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
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
            busy=[(start, start + timedelta(minutes=10))],
        )


# ==================================================================================
# Routes: connect / list / callback / disconnect
# ==================================================================================
async def test_start_connect_and_unconfigured_503(cal_app):
    client, application = cal_app
    token, org_id, _call = await _place(client, "cal11@example.com", "Org CAL11")
    headers = auth_headers(token, org_id)

    response = await client.post("/api/v1/calendar/oauth/google/start", headers=headers)
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert parse_qs(urlparse(url).query)["state"][0]

    application.state.settings = application.state.settings.model_copy(
        update={"google_calendar_client_id": ""}
    )
    unconfigured = await client.post(
        "/api/v1/calendar/oauth/google/start", headers=headers
    )
    assert unconfigured.status_code == 503, unconfigured.text


async def test_full_connect_stores_connection_without_leaking_secrets(
    cal_app, session, fake_google
):
    client, _application = cal_app
    token, org_id, _call = await _place(client, "cal12@example.com", "Org CAL12")
    headers = auth_headers(token, org_id)

    start = await client.post("/api/v1/calendar/oauth/google/start", headers=headers)
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]

    callback = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"code": "c1", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    assert location.startswith("https://ringlite.test/settings/ai?")
    outcome = parse_qs(urlparse(location).query)
    assert outcome["tab"] == ["appointments"]
    assert outcome["calendar"] == ["connected"]

    listing = await client.get("/api/v1/calendar/connections", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["google_available"] is True
    assert len(body["connections"]) == 1
    connection = body["connections"][0]
    assert connection["provider"] == "google"
    assert connection["account_email"] == "owner@example.com"
    assert connection["status"] == "active"
    assert connection["connected_by_me"] is True
    assert "rt-1" not in listing.text
    assert "credentials" not in listing.text

    set_org_context(session, org_id)
    session.expire_all()
    row = (
        await session.execute(
            sa.select(CalendarConnection).where(CalendarConnection.org_id == org_id)
        )
    ).scalar_one()
    assert "rt-1" not in row.credentials


async def test_replayed_and_cancelled_states(cal_app, fake_google):
    client, _application = cal_app
    token, org_id, _call = await _place(client, "cal13@example.com", "Org CAL13")
    headers = auth_headers(token, org_id)

    start = await client.post("/api/v1/calendar/oauth/google/start", headers=headers)
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]

    first = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"code": "c1", "state": state},
        follow_redirects=False,
    )
    assert first.status_code == 302, first.text
    assert _outcome(first) == "connected"

    replay = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"code": "c1", "state": state},
        follow_redirects=False,
    )
    assert replay.status_code == 302, replay.text
    assert _outcome(replay) == "error"

    fresh = await client.post("/api/v1/calendar/oauth/google/start", headers=headers)
    cancel_state = parse_qs(urlparse(fresh.json()["url"]).query)["state"][0]
    cancel = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"error": "access_denied", "state": cancel_state},
        follow_redirects=False,
    )
    assert cancel.status_code == 302, cancel.text
    assert _outcome(cancel) == "cancelled"

    reuse = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"code": "c1", "state": cancel_state},
        follow_redirects=False,
    )
    assert reuse.status_code == 302, reuse.text
    assert _outcome(reuse) == "error"


async def test_sso_state_is_rejected_by_the_calendar_callback(cal_app):
    client, application = cal_app
    _token, org_id, _call = await _place(client, "cal14@example.com", "Org CAL14")
    state = await oidc.issue_state(
        application.state.settings, org_id=org_id, nonce=str(uuid.uuid4())
    )

    response = await client.get(
        "/api/v1/calendar/oauth/google/callback",
        params={"code": "c1", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    assert _outcome(response) == "error"


async def test_reconnecting_keeps_one_connection_row(cal_app, fake_google):
    client, _application = cal_app
    token, org_id, _call = await _place(client, "cal15@example.com", "Org CAL15")
    headers = auth_headers(token, org_id)

    first_id = await _connect(client, headers)
    second_id = await _connect(client, headers)
    assert first_id == second_id

    listing = await client.get("/api/v1/calendar/connections", headers=headers)
    connections = listing.json()["connections"]
    assert len(connections) == 1
    assert connections[0]["id"] == str(first_id)


# ==================================================================================
# Worker seams reading the connected calendar
# ==================================================================================
async def test_availability_reads_the_connected_calendar(cal_app, session, fake_google):
    client, _application = cal_app
    token, org_id, call_id = await _place(client, "cal16@example.com", "Org CAL16")
    headers = auth_headers(token, org_id)
    conn_id = await _connect(client, headers)
    await _make_profile(
        session,
        org_id,
        _booking_config(lead_minutes=0, weekly=_ALWAYS_OPEN),
        calendar_connection_id=conn_id,
    )

    fake_google.events = []
    first = await client.get(
        f"/api/v1/agent/availability?call_id={call_id}",
        headers=worker_headers(worker_token()),
    )
    assert first.status_code == 200, first.text
    assert first.json()["enabled"] is True
    slots = first.json()["slots"]
    assert slots
    first_slot = datetime.fromisoformat(slots[0]["start"])

    fake_google.events = [
        {
            "status": "confirmed",
            "start": {"dateTime": first_slot.isoformat()},
            "end": {"dateTime": (first_slot + timedelta(minutes=30)).isoformat()},
        }
    ]
    second = await client.get(
        f"/api/v1/agent/availability?call_id={call_id}",
        headers=worker_headers(worker_token()),
    )
    assert second.status_code == 200, second.text
    offered = [datetime.fromisoformat(slot["start"]) for slot in second.json()["slots"]]
    assert first_slot not in offered

    fake_google.list_status = 500
    third = await client.get(
        f"/api/v1/agent/availability?call_id={call_id}",
        headers=worker_headers(worker_token()),
    )
    assert third.status_code == 503, third.text


async def test_book_appointment_writes_google_event(cal_app, session, fake_google):
    client, _application = cal_app
    token, org_id, call_id = await _place(client, "cal17a@example.com", "Org CAL17a")
    headers = auth_headers(token, org_id)
    conn_id = await _connect(client, headers)
    await _make_profile(
        session,
        org_id,
        _booking_config(lead_minutes=0, weekly=_ALWAYS_OPEN),
        calendar_connection_id=conn_id,
    )

    fake_google.events = []
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    response = await client.post(
        "/api/v1/agent/appointments/book",
        json={"call_id": call_id, "start": start.isoformat(), "notes": "intro"},
        headers=worker_headers(worker_token()),
    )
    assert response.status_code == 200, response.text
    assert response.json()["booked"] is True

    posts = [
        call
        for call in fake_google.calls
        if call.method == "POST" and "calendar/v3" in str(call.url)
    ]
    assert len(posts) == 1
    payload = json.loads(posts[0].content.decode())
    assert datetime.fromisoformat(payload["start"]["dateTime"]) == start

    set_org_context(session, org_id)
    session.expire_all()
    appt = (
        await session.execute(sa.select(Appointment).where(Appointment.org_id == org_id))
    ).scalar_one()
    assert appt.external_event_id == "evt-1"
    assert appt.calendar_connection_id == conn_id


async def test_book_appointment_survives_google_write_failure(
    cal_app, session, fake_google
):
    client, _application = cal_app
    token, org_id, call_id = await _place(client, "cal17b@example.com", "Org CAL17b")
    headers = auth_headers(token, org_id)
    conn_id = await _connect(client, headers)
    await _make_profile(
        session,
        org_id,
        _booking_config(lead_minutes=0, weekly=_ALWAYS_OPEN),
        calendar_connection_id=conn_id,
    )

    fake_google.events = []
    fake_google.insert_status = 500
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    response = await client.post(
        "/api/v1/agent/appointments/book",
        json={"call_id": call_id, "start": start.isoformat(), "notes": ""},
        headers=worker_headers(worker_token()),
    )
    assert response.status_code == 200, response.text
    assert response.json()["booked"] is True

    set_org_context(session, org_id)
    session.expire_all()
    appt = (
        await session.execute(sa.select(Appointment).where(Appointment.org_id == org_id))
    ).scalar_one()
    assert appt.external_event_id is None


async def test_expired_access_token_is_refreshed(cal_app, session, fake_google):
    client, application = cal_app
    settings = application.state.settings
    token, org_id, call_id = await _place(client, "cal18@example.com", "Org CAL18")
    headers = auth_headers(token, org_id)
    conn_id = await _connect(client, headers)
    await _make_profile(
        session,
        org_id,
        _booking_config(lead_minutes=0, weekly=_ALWAYS_OPEN),
        calendar_connection_id=conn_id,
    )

    set_org_context(session, org_id)
    conn = await calendars_svc.get_connection(session, org_id, conn_id)
    assert conn is not None
    conn.credentials = credentials.encrypt(
        settings,
        {
            "refresh_token": "rt-1",
            "access_token": "old",
            "expires_at": "2000-01-01T00:00:00+00:00",
        },
    )
    await session.commit()

    fake_google.calls.clear()
    fake_google.events = []
    response = await client.get(
        f"/api/v1/agent/availability?call_id={call_id}",
        headers=worker_headers(worker_token()),
    )
    assert response.status_code == 200, response.text

    token_calls = [
        call for call in fake_google.calls if str(call.url) == calendar_google.TOKEN_URL
    ]
    assert any("grant_type=refresh_token" in call.content.decode() for call in token_calls)
    event_gets = [
        call
        for call in fake_google.calls
        if call.method == "GET" and "calendar/v3" in str(call.url)
    ]
    assert event_gets
    assert event_gets[-1].headers.get("authorization") == "Bearer at-2"


async def test_disconnect_revokes_and_deletes(cal_app, fake_google):
    client, _application = cal_app
    token, org_id, _call = await _place(client, "cal19@example.com", "Org CAL19")
    headers = auth_headers(token, org_id)
    conn_id = await _connect(client, headers)

    fake_google.calls.clear()
    response = await client.delete(
        f"/api/v1/calendar/connections/{conn_id}", headers=headers
    )
    assert response.status_code == 204, response.text
    revokes = [
        call
        for call in fake_google.calls
        if str(call.url) == calendar_google.REVOKE_URL
    ]
    assert len(revokes) == 1

    remaining = await client.get("/api/v1/calendar/connections", headers=headers)
    assert remaining.json()["connections"] == []

    unknown = await client.delete(
        f"/api/v1/calendar/connections/{uuid.uuid4()}", headers=headers
    )
    assert unknown.status_code == 404, unknown.text
