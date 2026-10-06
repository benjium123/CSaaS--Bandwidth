"""Calendar connections (2026-10-07): connect, keep tokens fresh, read busy time, write
bookings. Provider HTTP lives in ``calendar_google``; this module owns the rows.

Which calendar an assistant uses is explicit: ``AgentProfile.extra["booking"]
["calendar_connection_id"]``. No id (or a disconnected one) = Ringlite's calendar only.

Failure rules:
- Reading busy time fails CLOSED (``CalendarUnavailableError``): offering a slot we could
  not check is how double bookings happen.
- Writing a booking's copy fails OPEN: the appointment is already real in Ringlite, so a
  Google outage only costs the copy, logged and visible on the connection.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import set_org_context
from app.errors import FeatureUnavailableError, ValidationFailedError
from app.models import Appointment, CalendarConnection
from app.services import calendar_google as google
from app.services import credentials, oidc

log = structlog.get_logger("calendars")

STATE_NAMESPACE = "cal"


class CalendarUnavailableError(FeatureUnavailableError):
    code = "calendar_unavailable"
    message = "The connected calendar could not be checked"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def list_connections(session: AsyncSession, org_id: uuid.UUID) -> list[CalendarConnection]:
    stmt = (
        sa.select(CalendarConnection)
        .where(CalendarConnection.org_id == org_id)
        .order_by(CalendarConnection.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_connection(
    session: AsyncSession, org_id: uuid.UUID, connection_id: uuid.UUID
) -> CalendarConnection | None:
    stmt = sa.select(CalendarConnection).where(
        CalendarConnection.org_id == org_id, CalendarConnection.id == connection_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def start_google(settings: Settings, *, org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """The Google consent URL for this member. The single-use state binds the callback
    to this org and user - the callback carries no session of its own."""
    if not google.configured(settings):
        raise FeatureUnavailableError("Google Calendar is not set up on this server")
    state = await oidc.issue_state(
        settings, org_id=org_id, nonce=str(user_id), namespace=STATE_NAMESPACE
    )
    return google.authorize_url(settings, state)


async def complete_google(
    session: AsyncSession, settings: Settings, *, state: str, code: str, now: datetime
) -> CalendarConnection:
    """Finish the connect flow. Raises ValidationFailedError with a user-facing reason."""
    if not google.configured(settings):
        raise FeatureUnavailableError("Google Calendar is not set up on this server")
    data = await oidc.consume_state(settings, state, namespace=STATE_NAMESPACE)
    if not data:
        raise ValidationFailedError("This connect link expired. Please try again.")
    try:
        org_id = uuid.UUID(str(data["org_id"]))
        user_id = uuid.UUID(str(data["nonce"]))
    except (KeyError, ValueError) as exc:
        raise ValidationFailedError("This connect link is not valid.") from exc

    try:
        tokens = await google.exchange_code(settings, code, now=now)
    except google.GoogleCalendarError as exc:
        log.warning("google_calendar_connect_failed", org_id=str(org_id), error=str(exc))
        raise ValidationFailedError(
            "Google did not give calendar access. Please try again and allow calendar access."
        ) from exc
    if not tokens.email or not tokens.refresh_token:
        raise ValidationFailedError("Google did not return the account details we need.")

    secret = credentials.encrypt(
        settings,
        {
            "refresh_token": tokens.refresh_token,
            "access_token": tokens.access_token,
            "expires_at": tokens.expires_at.isoformat(),
        },
    )

    set_org_context(session, org_id)
    stmt = sa.select(CalendarConnection).where(
        CalendarConnection.org_id == org_id,
        CalendarConnection.provider == "google",
        CalendarConnection.account_email == tokens.email,
    )
    conn = (await session.execute(stmt)).scalar_one_or_none()
    if conn is None:
        conn = CalendarConnection(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=user_id,
            provider="google",
            account_email=tokens.email,
            calendar_id="primary",
            credentials=secret,
        )
        session.add(conn)
    else:
        # Reconnecting the same account repairs it in place, keeping the id assistants use.
        conn.credentials = secret
        conn.user_id = user_id
    conn.status = "active"
    conn.last_error = None
    await session.flush()
    return conn


async def _access_token(
    session: AsyncSession, settings: Settings, conn: CalendarConnection, now: datetime
) -> str:
    try:
        secret = credentials.decrypt(settings, conn.credentials)
    except Exception as exc:
        raise CalendarUnavailableError() from exc

    expires_raw = secret.get("expires_at")
    try:
        expires_at = _aware(datetime.fromisoformat(expires_raw)) if expires_raw else None
    except ValueError:
        expires_at = None
    if secret.get("access_token") and expires_at is not None and expires_at > now:
        return secret["access_token"]

    try:
        tokens = await google.refresh(settings, secret["refresh_token"], now=now)
    except google.GoogleCalendarError as exc:
        await _mark_error(session, conn, exc)
        raise CalendarUnavailableError() from exc

    conn.credentials = credentials.encrypt(
        settings,
        {
            "refresh_token": tokens.refresh_token,
            "access_token": tokens.access_token,
            "expires_at": tokens.expires_at.isoformat(),
        },
    )
    await session.flush()
    return tokens.access_token


async def _mark_error(
    session: AsyncSession, conn: CalendarConnection, exc: google.GoogleCalendarError
) -> None:
    if exc.revoked:
        conn.status = "error"
    conn.last_error = str(exc)[:255]
    await session.flush()


async def connection_for_profile(
    session: AsyncSession, org_id: uuid.UUID, profile
) -> CalendarConnection | None:
    raw = ((getattr(profile, "extra", None) or {}).get("booking") or {}).get(
        "calendar_connection_id"
    )
    if not raw:
        return None
    try:
        connection_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    conn = await get_connection(session, org_id, connection_id)
    if conn is None or conn.status != "active":
        return None
    return conn


async def external_busy(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    profile,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
) -> list[tuple[datetime, datetime]]:
    """Busy intervals from the profile's connected calendar; [] when it has none.
    Raises CalendarUnavailableError when the calendar exists but cannot be read."""
    conn = await connection_for_profile(session, org_id, profile)
    if conn is None:
        return []
    token = await _access_token(session, settings, conn, now)
    try:
        return await google.busy_intervals(token, conn.calendar_id, start, end)
    except google.GoogleCalendarError as exc:
        await _mark_error(session, conn, exc)
        log.warning("calendar_busy_failed", connection_id=str(conn.id), error=str(exc))
        raise CalendarUnavailableError() from exc


async def push_booking(
    session: AsyncSession,
    settings: Settings,
    appt: Appointment,
    profile,
    *,
    slot_minutes: int,
    now: datetime,
) -> None:
    """Write a copy of a confirmed booking into the profile's connected calendar.
    Never raises: the booking is already real in Ringlite."""
    conn: CalendarConnection | None = None
    try:
        conn = await connection_for_profile(session, appt.org_id, profile)
        if conn is None or appt.scheduled_for is None:
            return
        token = await _access_token(session, settings, conn, now)
        start = _aware(appt.scheduled_for)
        event_id = await google.create_event(
            token,
            conn.calendar_id,
            start=start,
            end=start + timedelta(minutes=slot_minutes),
            summary=f"Appointment with {appt.contact_e164}",
            description=(
                f"Booked by Ringlite AI for {appt.contact_e164}.\n\n{appt.notes or ''}"
            ).strip(),
        )
    except google.GoogleCalendarError as exc:
        if conn is not None:
            await _mark_error(session, conn, exc)
        log.warning("calendar_push_failed", appointment_id=str(appt.id), error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - see docstring: the copy is best effort
        log.warning("calendar_push_failed", appointment_id=str(appt.id), error=str(exc))
        return
    appt.calendar_connection_id = conn.id
    appt.external_event_id = event_id
    await session.flush()


async def disconnect(session: AsyncSession, settings: Settings, conn: CalendarConnection) -> None:
    """Revoke at Google (best effort) and delete the row. Bookings keep their copies'
    ids; ``calendar_connection_id`` nulls itself via ON DELETE SET NULL."""
    try:
        secret = credentials.decrypt(settings, conn.credentials)
        token = secret.get("refresh_token")
    except Exception:  # noqa: BLE001 - an unreadable row is still deletable
        token = None
    if token:
        await google.revoke(token)
    await session.delete(conn)
    await session.flush()


async def discard_state(settings: Settings, state: str) -> None:
    """Burn a state the person cancelled on, so it cannot be replayed later."""
    await oidc.consume_state(settings, state, namespace=STATE_NAMESPACE)
