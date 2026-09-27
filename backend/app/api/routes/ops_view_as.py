"""H3 view as workspace (services/view_as.py): start, check and end a read-only view."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.deps import OperatorContext, require_operator_permission
from app.errors import NotFoundError
from app.models import OperatorViewSession, Org
from app.services import operator_audit
from app.services import view_as as view_as_svc

router = APIRouter(prefix="/api/v1/ops/view-as", tags=["ops"])

Reader = Annotated[OperatorContext, Depends(require_operator_permission("ops:read"))]
Support = Annotated[OperatorContext, Depends(require_operator_permission("ops:support"))]
StartView = Annotated[
    OperatorContext, Depends(require_operator_permission("ops:support", major=True))
]


class StartIn(BaseModel):
    org_id: uuid.UUID


def _out(row: OperatorViewSession, org_name: str | None) -> dict:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "org_name": org_name,
        "reason": row.reason,
        "started_at": row.started_at,
        "expires_at": row.expires_at,
        "ended_at": row.ended_at,
        "header": view_as_svc.HEADER,
        #: The console shows the workspace with exactly these (read) permissions.
        "permissions": view_as_svc.VIEW_AS_PERMISSIONS,
    }


@router.post("", status_code=201)
async def start_view(payload: StartIn, request: Request, op: StartView) -> dict:
    """Needs a reason (X-Ops-Reason) and a fresh second factor; the owners are told."""
    org = await op.session.get(Org, payload.org_id)
    if org is None:
        raise NotFoundError("Workspace not found")
    row = await view_as_svc.start(
        op.session,
        request.app.state.settings,
        operator=op.user,
        org=org,
        reason=operator_audit.reason_of(request) or "",
        request=request,
    )
    await op.session.commit()
    return _out(row, org.name)


@router.get("/current")
async def current_view(op: Reader) -> dict:
    row = (
        await op.session.execute(
            sa.select(OperatorViewSession)
            .where(
                OperatorViewSession.operator_user_id == op.user.id,
                OperatorViewSession.ended_at.is_(None),
            )
            .order_by(OperatorViewSession.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not view_as_svc.is_live(row):
        return {"view": None}
    org = await op.session.get(Org, row.org_id)
    return {"view": _out(row, org.name if org else None)}


@router.post("/{view_id}/end")
async def end_view(view_id: uuid.UUID, op: Support) -> dict:
    row = await op.session.get(OperatorViewSession, view_id)
    if row is None or row.operator_user_id != op.user.id:
        raise NotFoundError("View not found")
    if row.ended_at is None:
        row.ended_at = view_as_svc._now()
        await op.session.commit()
    return {"ended": True}
