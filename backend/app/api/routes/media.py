from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, require_permission
from app.db.session import get_session
from app.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import MediaAsset
from app.services import media as media_svc

router = APIRouter(prefix="/api/v1/media", tags=["media"])


class MediaOut(BaseModel):
    id: uuid.UUID
    content_type: str | None
    size_bytes: int | None
    status: str
    url: str | None = None


def _store(request: Request):
    store = getattr(request.app.state, "media_store", None)
    if store is None:  # pragma: no cover - lifespan always sets it
        raise ValidationFailedError("Media storage is not configured")
    return store


@router.post("", response_model=MediaOut, status_code=201)
async def upload(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
    file: Annotated[UploadFile, File()],
) -> MediaOut:
    data = await file.read()
    settings = request.app.state.settings
    asset = await media_svc.store_upload(
        ctx.session,
        ctx.org.id,
        _store(request),
        data=data,
        content_type=(file.content_type or "application/octet-stream").split(";")[0].strip(),
        retention_days=settings.media_retention_days,
    )
    await ctx.session.commit()
    return MediaOut(
        id=asset.id,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        status=asset.status,
        url=media_svc.signed_url(
            settings.public_base_url or "",
            asset.id,
            settings.jwt_secret.get_secret_value(),
            media_svc.BROWSER_URL_TTL,
        ),
    )


@router.get("/{asset_id}/content")
async def content(
    asset_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    exp: int | None = Query(default=None),
    sig: str | None = Query(default=None),
) -> Response:
    """Serve media bytes to a signed URL.

    The carrier fetches outbound MMS from here and the browser renders inbound MMS from
    here; neither can present a JWT. A signature grants access to exactly ONE asset, so it
    never widens into org-level access.
    """
    settings = request.app.state.settings
    if not (
        exp
        and media_svc.verify_signature(
            asset_id, exp, sig or "", settings.jwt_secret.get_secret_value()
        )
    ):
        raise PermissionDeniedError("Invalid or expired media link")

    import sqlalchemy as sa

    from app.db.base import ALLOW_UNSCOPED_KEY

    asset = (
        await session.execute(
            sa.select(MediaAsset)
            .where(MediaAsset.id == asset_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if asset is None or asset.status != "stored" or not asset.storage_key:
        raise NotFoundError("Media not found")

    try:
        data = await _store(request).get(asset.storage_key)
    except KeyError as exc:
        raise NotFoundError("Media not found") from exc

    return Response(
        content=data,
        media_type=asset.content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=900"},
    )
