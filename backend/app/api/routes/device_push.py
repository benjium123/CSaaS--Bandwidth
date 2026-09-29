"""Ringlite apps P2: native push token registration for the current device session."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.db.session import get_session
from app.errors import PermissionDeniedError
from app.models import DevicePushToken, User
from app.models import Session as IdentitySession

router = APIRouter(prefix="/api/v1/me/device", tags=["device-push"])


class PushTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    platform: Literal["android", "desktop", "ios"]
    app_version: str | None = Field(default=None, max_length=40)


async def _device_row(request: Request, session: AsyncSession, user: User) -> IdentitySession:
    """The bearer must be a live DEVICE session owned by the caller, else 403."""
    sid = getattr(request.state, "session_id", None)
    row = await session.get(IdentitySession, sid) if sid is not None else None
    if row is None or not row.is_device or row.user_id != user.id:
        raise PermissionDeniedError(
            "Notifications can only be enabled from the Ringlite app.",
            code="device_session_required",
        )
    return row


@router.put("/push-token")
async def register_push_token(
    payload: PushTokenIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    row = await _device_row(request, session, user)
    # The same phone re-registering under a new login must not keep ringing for the old
    # session: drop any other row that already holds this token string (any user).
    await session.execute(
        delete(DevicePushToken).where(
            DevicePushToken.token == payload.token,
            DevicePushToken.session_id != row.id,
        )
    )
    existing = (
        await session.execute(
            select(DevicePushToken).where(DevicePushToken.session_id == row.id)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DevicePushToken(
                user_id=user.id,
                session_id=row.id,
                platform=payload.platform,
                token=payload.token,
                app_version=payload.app_version,
            )
        )
    else:
        existing.user_id = user.id
        existing.platform = payload.platform
        existing.token = payload.token
        existing.app_version = payload.app_version
    await session.commit()
    return {"ok": True}


@router.delete("/push-token")
async def unregister_push_token(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    sid = getattr(request.state, "session_id", None)
    if sid is not None:
        await session.execute(
            delete(DevicePushToken).where(
                DevicePushToken.session_id == sid,
                DevicePushToken.user_id == user.id,
            )
        )
        await session.commit()
    return {"ok": True}
