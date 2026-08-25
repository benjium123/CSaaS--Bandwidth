from __future__ import annotations

import asyncio

import sqlalchemy as sa
from fastapi import APIRouter, Request, Response

from app.db.session import get_sessionmaker

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict:
    """Liveness + DB reachability. No auth: monitors must be able to call it."""
    settings = request.app.state.settings
    db_state = "ok"
    try:
        async with get_sessionmaker()() as session:
            await asyncio.wait_for(session.execute(sa.text("SELECT 1")), timeout=2.0)
    except (asyncio.TimeoutError, Exception):  # noqa: B014 - intentional catch-all
        db_state = "unreachable"

    if db_state != "ok":
        response.status_code = 503

    return {
        "status": "ok" if db_state == "ok" else "degraded",
        "env": settings.app_env,
        "version": request.app.version,
        "db": db_state,
    }
