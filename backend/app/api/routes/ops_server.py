"""Ops console "Server" page: whole-host resources (CPU, memory, disk, load, uptime) plus
per-app usage read from the host stats file. Reviewer operators only; no DB access."""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends

from app.auth.deps import OperatorContext, require_operator_permission
from app.services import server_stats

router = APIRouter(prefix="/api/v1/ops/console", tags=["ops-console"])
log = structlog.get_logger("ops_server")

Reader = Annotated[OperatorContext, Depends(require_operator_permission("ops:read"))]


@router.get("/server")
async def console_server(op: Reader) -> dict:
    return await server_stats.snapshot()
