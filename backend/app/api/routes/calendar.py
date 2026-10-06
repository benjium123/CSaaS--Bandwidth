"""Calendar connections (2026-10-07): connect Google Calendar, list, disconnect.

The OAuth callback is PUBLIC (Google redirects the browser there with no Ringlite session);
the single-use, namespaced state issued by /start is what binds it to the member and
workspace, exactly like SSO. The callback ends in a redirect back to the app, never JSON.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, require_permission
from app.config import Settings
from app.db.session import get_session
from app.errors import CsaasError, NotFoundError, PermissionDeniedError
from app.rate_limit import enforce_rate_limit
from app.services import calendar_google
from app.services import calendars as calendars_svc
from app.services import identity as identity_svc

log = structlog.get_logger("calendar_routes")

router = APIRouter(prefix="/api/v1/calendar", tags=["calendar"])

#: Where the browser lands after Google: the AI settings' Appointments tab.
RETURN_PATH = "/settings/ai"


class CalendarConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    account_email: str
    status: str
    last_error: str | None = None
    connected_by_me: bool
    created_at: datetime


class CalendarConnectionsOut(BaseModel):
    google_available: bool
    connections: list[CalendarConnectionOut]


class ConnectStartOut(BaseModel):
    url: str


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/connections", response_model=CalendarConnectionsOut)
async def list_calendar_connections(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> CalendarConnectionsOut:
    rows = await calendars_svc.list_connections(ctx.session, ctx.org.id)
    me = ctx.actor_user_id
    return CalendarConnectionsOut(
        google_available=calendar_google.configured(_settings(request)),
        connections=[
            CalendarConnectionOut(
                id=row.id,
                provider=row.provider,
                account_email=row.account_email,
                status=row.status,
                last_error=row.last_error,
                connected_by_me=me is not None and row.user_id == me,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


@router.post("/oauth/google/start", response_model=ConnectStartOut)
async def start_google_connect(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> ConnectStartOut:
    user_id = ctx.actor_user_id
    if user_id is None:
        raise PermissionDeniedError("Connect a calendar while signed in as a person")
    await enforce_rate_limit(request, f"calendar:start:{user_id}")
    url = await calendars_svc.start_google(_settings(request), org_id=ctx.org.id, user_id=user_id)
    return ConnectStartOut(url=url)


def _back_to_app(settings: Settings, outcome: str, message: str | None = None) -> RedirectResponse:
    base = (settings.public_base_url or "").strip().rstrip("/")
    params = {"tab": "appointments", "calendar": outcome}
    if message:
        params["calendar_error"] = message[:200]
    return RedirectResponse(f"{base}{RETURN_PATH}?{urlencode(params)}", status_code=302)


@router.get("/oauth/google/callback")
async def google_connect_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = _settings(request)
    await enforce_rate_limit(
        request, f"calendar:callback:{identity_svc.client_ip(request) or 'unknown'}"
    )
    if error is not None or not code or not state:
        # "access_denied" = the person pressed Cancel on Google's screen.
        if error is not None and state:
            await calendars_svc.discard_state(settings, state)
        return _back_to_app(settings, "cancelled")
    try:
        await calendars_svc.complete_google(
            session, settings, state=state, code=code, now=datetime.now(timezone.utc)
        )
        await session.commit()
    except CsaasError as exc:
        await session.rollback()
        return _back_to_app(settings, "error", exc.message)
    return _back_to_app(settings, "connected")


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_calendar_connection(
    connection_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> None:
    conn = await calendars_svc.get_connection(ctx.session, ctx.org.id, connection_id)
    if conn is None:
        raise NotFoundError("Calendar connection not found")
    mine = ctx.actor_user_id is not None and conn.user_id == ctx.actor_user_id
    if not mine and not ctx.role.grants("settings:write"):
        raise PermissionDeniedError("Only the person who connected it or an admin can remove it")
    await calendars_svc.disconnect(ctx.session, _settings(request), conn)
    await ctx.session.commit()
