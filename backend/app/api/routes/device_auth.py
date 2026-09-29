"""Ringlite apps P1: sign-in for the Android and desktop apps.

Contract: ringlite_apps/docs/api/device-auth-contract.md. Every credential check is the web
one (routes/auth._login, twofa verify helpers, login_flow.complete_login); these routes only
ask for a device session instead of a cookie, and add refresh, logout and QR device linking.
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import auth as auth_routes
from app.api.routes import twofa as twofa_routes
from app.auth.deps import get_current_user
from app.config import Settings
from app.db.session import get_session
from app.errors import PermissionDeniedError, UnauthenticatedError
from app.models import Session as IdentitySession
from app.models import User
from app.rate_limit import enforce_rate_limit
from app.services import device_sessions, identity as identity_svc, login_flow

router = APIRouter(prefix="/api/v1/auth/device", tags=["device-auth"])
app_router = APIRouter(prefix="/api/v1/app", tags=["device-auth"])


class DeviceIn(BaseModel):
    kind: Literal["android", "desktop", "ios"]
    name: str | None = Field(default=None, max_length=120)
    os: str | None = Field(default=None, max_length=60)
    app_version: str | None = Field(default=None, max_length=40)

    def info(self) -> device_sessions.DeviceInfo:
        return device_sessions.DeviceInfo(
            kind=self.kind, name=self.name, os=self.os, app_version=self.app_version
        )


class DeviceLoginIn(BaseModel):
    email: EmailStr
    password: str
    device: DeviceIn


class DeviceVerifyIn(BaseModel):
    pending_token: str
    code: str = Field(min_length=6, max_length=8)
    device: DeviceIn


class DeviceEmailVerifyIn(BaseModel):
    pending_token: str
    code: str = Field(min_length=6, max_length=6)
    device: DeviceIn


class PendingIn(BaseModel):
    pending_token: str


class RefreshIn(BaseModel):
    refresh_token: str = Field(max_length=200)


class LinkIn(BaseModel):
    code: str = Field(max_length=100)
    device: DeviceIn


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/login")
async def device_login(
    payload: DeviceLoginIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    result = await auth_routes._login(
        auth_routes.LoginIn(email=payload.email, password=payload.password),
        request,
        response,
        session,
        device=payload.device.info(),
    )
    if isinstance(result, dict):
        return result
    # Second factor pending: same fields the web login returns.
    return result.model_dump(
        include={"requires_2fa", "pending_token", "methods", "recovery_codes_available"}
    )


@router.post("/2fa/verify")
async def device_2fa_verify(
    payload: DeviceVerifyIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return await twofa_routes.verify_totp_login(
        twofa_routes.VerifyIn(pending_token=payload.pending_token, code=payload.code),
        request,
        response,
        session,
        device=payload.device.info(),
    )


@router.post("/2fa/email/send")
async def device_2fa_email_send(
    payload: PendingIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return await twofa_routes.email_login_send(
        twofa_routes.PendingIn(pending_token=payload.pending_token), request, session
    )


@router.post("/2fa/email/verify")
async def device_2fa_email_verify(
    payload: DeviceEmailVerifyIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return await twofa_routes.verify_email_login(
        twofa_routes.PendingCodeIn(pending_token=payload.pending_token, code=payload.code),
        request,
        response,
        session,
        device=payload.device.info(),
    )


@router.post("/refresh")
async def device_refresh(
    payload: RefreshIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await enforce_rate_limit(request, f"device-refresh:{payload.refresh_token[:48]}")
    return await device_sessions.refresh(session, _settings(request), payload.refresh_token)


@router.post("/logout")
async def device_logout(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    sid = getattr(request.state, "session_id", None)
    row = await session.get(IdentitySession, sid) if sid is not None else None
    if row is None or not row.is_device or row.user_id != user.id:
        raise UnauthenticatedError("Sign in to continue")
    await device_sessions.revoke(session, _settings(request), row, revoked_by=user.id)
    return {"ok": True}


@router.post("/link-codes")
async def create_link_code(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """A signed-in WEB user creates a one-time code to show as a QR on screen."""
    sid = getattr(request.state, "session_id", None)
    row = await identity_svc.get_live_session(session, sid) if sid is not None else None
    # Browser (cookie) sessions only: a device cannot mint links for more devices.
    if row is None or row.is_device or row.token_hash is None:
        raise PermissionDeniedError(
            "Link a device from the Ringlite web app.", code="web_session_required"
        )
    # The link carries this session's sign-in strength: an account with a second factor
    # can only link from a session that passed it.
    if login_flow.second_factor_methods(user) and row.second_factor_at is None:
        raise PermissionDeniedError(
            "Sign in again with two-step verification to link a device.",
            code="two_factor_required",
        )
    await enforce_rate_limit(request, f"device-link-create:{user.id}")
    settings = _settings(request)
    code, expires = await device_sessions.create_link_code(session, settings, row)
    # Never the request's Host header: the app trusts this host, so it comes from config.
    host = urlparse(settings.public_web_url).netloc
    return {
        "code": code,
        "qr_payload": f"ringlite://link?c={code}&h={host}",
        "expires_at": expires.isoformat(),
    }


@router.post("/link")
async def redeem_link_code(
    payload: LinkIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await enforce_rate_limit(request, f"device-link:{identity_svc.client_ip(request)}")
    return await device_sessions.redeem_link_code(
        session, _settings(request), request, payload.code, payload.device.info()
    )


@app_router.get("/version")
async def app_version(request: Request) -> dict:
    settings = _settings(request)
    return {
        "android": {
            "min_supported_code": settings.app_min_android_code,
            "latest_code": settings.app_latest_android_code,
            "latest_name": settings.app_latest_android_name,
            "manifest_url": settings.app_android_manifest_url,
        },
        "desktop": {"min_supported_code": settings.app_min_desktop_code},
    }


def min_app_header(settings: Settings, app_header: str | None) -> str | None:
    """``X-Ringlite-App: android/<versionCode>`` → the minimum code for that platform."""
    if not app_header or "/" not in app_header:
        return None
    platform = app_header.split("/", 1)[0].strip().lower()
    minimum = {
        "android": settings.app_min_android_code,
        "desktop": settings.app_min_desktop_code,
    }.get(platform)
    return str(minimum) if minimum is not None else None
