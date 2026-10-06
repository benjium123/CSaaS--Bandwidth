"""Google Calendar over plain HTTPS (2026-10-07). No Google SDK: five endpoints, httpx.

Scopes are ``calendar.events`` + ``openid`` + ``email`` - exactly what the consent screen
declares. ``freeBusy`` is NOT used: it needs a calendar.readonly/freebusy scope we do not
hold, so busy time is derived from ``events.list`` the way Google itself derives it
(cancelled, "Free"/transparent and declined events do not block time).

Every network call goes through ``_client()`` so tests can swap in an
``httpx.MockTransport`` via ``TRANSPORT``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
SCOPES = ("openid", "email", "https://www.googleapis.com/auth/calendar.events")

TIMEOUT_SECONDS = 8.0
#: events.list pages read per busy query (250 events each) before we stop and fail closed.
MAX_PAGES = 5

#: Swapped for an httpx.MockTransport in tests.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class GoogleCalendarError(Exception):
    """Any failure talking to Google. ``revoked`` = the refresh token is dead (the user
    removed access, or it expired) and only reconnecting fixes it."""

    def __init__(self, message: str, *, revoked: bool = False) -> None:
        super().__init__(message)
        self.revoked = revoked


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    expires_at: datetime
    refresh_token: str | None
    email: str | None


def configured(settings: Settings) -> bool:
    return bool(
        settings.google_calendar_client_id.strip()
        and settings.google_calendar_client_secret.get_secret_value().strip()
        and (settings.public_base_url or "").strip()
    )


def redirect_uri(settings: Settings) -> str:
    # Must match the URI registered on the Google OAuth client byte for byte.
    base = (settings.public_base_url or "").strip().rstrip("/")
    return f"{base}/api/v1/calendar/oauth/google/callback"


def authorize_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.google_calendar_client_id.strip(),
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent = Google returns a refresh token every time, including when
        # the same account reconnects after a disconnect.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=TRANSPORT)


def _expires_at(payload: dict, now: datetime) -> datetime:
    try:
        seconds = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        seconds = 3600
    # Refresh a minute early so a token never expires mid-request.
    return now + timedelta(seconds=max(seconds - 60, 0))


def _email_from_id_token(id_token: str | None) -> str | None:
    """The id_token came straight from Google's token endpoint over TLS, so its payload
    is read without re-verifying the signature (Google's documented server-flow rule)."""
    if not id_token or id_token.count(".") != 2:
        return None
    body = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    if claims.get("email_verified") is False:
        return None
    return email.strip().lower()


async def _token_request(settings: Settings, form: dict) -> dict:
    form = {
        **form,
        "client_id": settings.google_calendar_client_id.strip(),
        "client_secret": settings.google_calendar_client_secret.get_secret_value().strip(),
    }
    try:
        async with _client() as client:
            response = await client.post(TOKEN_URL, data=form)
    except httpx.HTTPError as exc:
        raise GoogleCalendarError(f"Google token request failed: {type(exc).__name__}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or not payload.get("access_token"):
        error = payload.get("error") if isinstance(payload, dict) else None
        raise GoogleCalendarError(
            f"Google token request refused: {error or response.status_code}",
            revoked=error == "invalid_grant",
        )
    return payload


async def exchange_code(settings: Settings, code: str, *, now: datetime) -> TokenSet:
    payload = await _token_request(
        settings,
        {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(settings),
        },
    )
    granted = set(str(payload.get("scope", "")).split())
    if "https://www.googleapis.com/auth/calendar.events" not in granted:
        # Google lets the user untick the calendar box on the consent screen.
        raise GoogleCalendarError("Calendar access was not granted")
    return TokenSet(
        access_token=payload["access_token"],
        expires_at=_expires_at(payload, now),
        refresh_token=payload.get("refresh_token"),
        email=_email_from_id_token(payload.get("id_token")),
    )


async def refresh(settings: Settings, refresh_token: str, *, now: datetime) -> TokenSet:
    payload = await _token_request(
        settings, {"refresh_token": refresh_token, "grant_type": "refresh_token"}
    )
    return TokenSet(
        access_token=payload["access_token"],
        expires_at=_expires_at(payload, now),
        refresh_token=payload.get("refresh_token") or refresh_token,
        email=None,
    )


async def revoke(token: str) -> None:
    """Best effort: a failed revoke must never block a disconnect on our side."""
    try:
        async with _client() as client:
            await client.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        return


def _events_url(calendar_id: str) -> str:
    # A calendar id can be an email address; "@" and "#" must not reach the path raw.
    return EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))


def _api_error(response: httpx.Response) -> GoogleCalendarError:
    return GoogleCalendarError(
        f"Google Calendar returned {response.status_code}",
        revoked=response.status_code == 401,
    )


def _parse_instant(value: dict, tz: ZoneInfo) -> datetime | None:
    """A Google event time: ``dateTime`` (RFC3339) or ``date`` (all-day, calendar tz)."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("dateTime"), str):
        raw = value["dateTime"].replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(timezone.utc)
    if isinstance(value.get("date"), str):
        try:
            day = date.fromisoformat(value["date"])
        except ValueError:
            return None
        # All-day end dates are exclusive already (end = the next day at 00:00).
        return datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    return None


def _blocks_time(event: dict) -> bool:
    if event.get("status") == "cancelled":
        return False
    if event.get("transparency") == "transparent":
        return False
    for attendee in event.get("attendees") or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return False
    return True


async def busy_intervals(
    access_token: str, calendar_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Busy ``[start, end)`` intervals (aware UTC) overlapping the window. Raises
    GoogleCalendarError on any failure - callers fail CLOSED rather than offer a time
    that may be taken."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
        "fields": "timeZone,nextPageToken,items(status,transparency,start,end,attendees(self,"
        "responseStatus))",
    }
    intervals: list[tuple[datetime, datetime]] = []
    page_token: str | None = None
    try:
        async with _client() as client:
            for _ in range(MAX_PAGES):
                query = dict(params)
                if page_token:
                    query["pageToken"] = page_token
                response = await client.get(
                    _events_url(calendar_id), params=query, headers=headers
                )
                if response.status_code != 200:
                    raise _api_error(response)
                payload = response.json()
                try:
                    tz = ZoneInfo(payload.get("timeZone") or "UTC")
                except Exception:
                    tz = ZoneInfo("UTC")
                for event in payload.get("items") or []:
                    if not _blocks_time(event):
                        continue
                    busy_start = _parse_instant(event.get("start"), tz)
                    busy_end = _parse_instant(event.get("end"), tz)
                    if busy_start is None or busy_end is None or busy_end <= busy_start:
                        continue
                    intervals.append((busy_start, busy_end))
                page_token = payload.get("nextPageToken")
                if not page_token:
                    return intervals
    except httpx.HTTPError as exc:
        raise GoogleCalendarError(f"Google Calendar request failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise GoogleCalendarError("Google Calendar returned an unreadable response") from exc
    # More pages than we read: an incomplete picture must not count as "free".
    raise GoogleCalendarError("Too many events in the booking window")


async def create_event(
    access_token: str,
    calendar_id: str,
    *,
    start: datetime,
    end: datetime,
    summary: str,
    description: str,
) -> str:
    """Insert one event and return its Google id."""
    body = {
        "summary": summary[:250],
        "description": description[:4000],
        "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
        "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
    }
    try:
        async with _client() as client:
            response = await client.post(
                _events_url(calendar_id),
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise GoogleCalendarError(f"Google Calendar request failed: {type(exc).__name__}") from exc
    if response.status_code not in (200, 201):
        raise _api_error(response)
    try:
        event_id = response.json().get("id")
    except ValueError:
        event_id = None
    if not isinstance(event_id, str) or not event_id:
        raise GoogleCalendarError("Google Calendar did not return an event id")
    return event_id
