from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.errors import NotFoundError
from app.models.links import ShortLink
from app.services import links as links_svc

log = structlog.get_logger("links_redirect")

router = APIRouter(tags=["links"])


@router.get("/l/{code}")
async def follow(
    code: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    """Redirect a tracked short link to its target URL.

    This route is public and has no org path. The code is a random secret, so
    the cross-org lookup reveals nothing about tenants: the only response is a
    302 to the stored URL.
    """
    # Cross-org lookup is intentional here because the short code is globally
    # unique and carries no org prefix in the URL.
    row = (
        await session.execute(
            sa.select(ShortLink)
            .where(ShortLink.code == code)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()

    if row is None:
        raise NotFoundError("That link is no longer available.")

    # Re-validate even though the row was already checked when shortened: an
    # older version of the code could have written an unsafe target.
    if not links_svc.is_safe_target(row.target_url):
        raise NotFoundError("That link is no longer available.")

    set_org_context(session, row.org_id)

    # Read the target BEFORE recording the click. record_click commits, which expires
    # every attribute on `row`; touching row.target_url afterwards would fire a lazy
    # refresh from inside the response path and raise MissingGreenlet.
    target_url = row.target_url

    settings = request.app.state.settings
    client_ip = request.client.host if request.client else None

    try:
        await links_svc.record_click(
            session,
            row,
            ip=client_ip,
            user_agent=request.headers.get("user-agent"),
            secret=settings.jwt_secret.get_secret_value(),
        )
    except Exception:
        # Click analytics must never prevent a person from reaching their
        # destination, so log and continue with the redirect.
        log.exception("short_link_click_record_failed", code=code)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - a failed rollback must not break the redirect
            pass

    response = RedirectResponse(target_url, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
