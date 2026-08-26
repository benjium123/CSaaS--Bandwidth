from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, ValidationFailedError
from app.models import MessageThread, OrgMembership, Tag, ThreadLabel
from app.services import inbox as inbox_svc

router = APIRouter(prefix="/api/v1", tags=["inbox"])


class ThreadPatchIn(BaseModel):
    status: str | None = None
    assigned_user_id: uuid.UUID | None = None
    # Distinguishes "unassign" from "not supplied".
    clear_assignee: bool = False


class LabelsIn(BaseModel):
    tag_ids: list[uuid.UUID] = []


@router.get("/inbox/threads")
async def inbox_threads(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    status: str | None = None,
    assigned: str | None = None,
    q: str | None = None,
    label_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(inbox_svc.DEFAULT_LIMIT, ge=1, le=inbox_svc.MAX_LIMIT),
) -> dict[str, Any]:
    if status and status not in ("open", "closed"):
        raise ValidationFailedError("status must be 'open' or 'closed'")
    return await inbox_svc.list_inbox(
        ctx.session,
        ctx.org.id,
        ctx.membership.user_id,
        inbox_svc.InboxFilters(status=status, assigned=assigned, q=q, label_id=label_id),
        cursor=cursor,
        limit=limit,
    )


async def _get_thread(ctx: OrgContext, thread_id: uuid.UUID) -> MessageThread:
    # Scoped by the session guard: another org's thread id is a 404 here.
    thread = await ctx.session.get(MessageThread, thread_id)
    if thread is None:
        raise NotFoundError("Thread not found")
    return thread


@router.patch("/threads/{thread_id}")
async def patch_thread(
    thread_id: uuid.UUID,
    payload: ThreadPatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:manage"))],
) -> dict:
    thread = await _get_thread(ctx, thread_id)

    if payload.status is not None:
        if payload.status not in ("open", "closed"):
            raise ValidationFailedError("status must be 'open' or 'closed'")
        thread.status = payload.status

    if payload.clear_assignee:
        thread.assigned_user_id = None
    elif payload.assigned_user_id is not None:
        member = (
            await ctx.session.execute(
                sa.select(OrgMembership).where(
                    OrgMembership.user_id == payload.assigned_user_id
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise ValidationFailedError("Assignee is not a member of this organization")
        thread.assigned_user_id = payload.assigned_user_id

    await ctx.session.commit()
    return {
        "id": thread.id,
        "status": thread.status,
        "assigned_user_id": thread.assigned_user_id,
    }


@router.post("/threads/{thread_id}/read", status_code=204)
async def mark_read(
    thread_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> Response:
    thread = await _get_thread(ctx, thread_id)
    thread.last_read_at = datetime.now(timezone.utc)
    await ctx.session.commit()
    return Response(status_code=204)


@router.put("/threads/{thread_id}/labels")
async def set_labels(
    thread_id: uuid.UUID,
    payload: LabelsIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:manage"))],
) -> dict:
    thread = await _get_thread(ctx, thread_id)

    wanted = set(payload.tag_ids)
    if wanted:
        found = {
            t.id
            for t in (
                await ctx.session.execute(sa.select(Tag).where(Tag.id.in_(wanted)))
            ).scalars().all()
        }
        missing = wanted - found
        if missing:
            raise ValidationFailedError(f"Unknown tag ids: {sorted(str(m) for m in missing)}")

    existing = list(
        (
            await ctx.session.execute(
                sa.select(ThreadLabel).where(ThreadLabel.thread_id == thread.id)
            )
        ).scalars().all()
    )
    for row in existing:
        if row.tag_id not in wanted:
            await ctx.session.delete(row)
    have = {row.tag_id for row in existing}
    for tag_id in wanted - have:
        ctx.session.add(
            ThreadLabel(
                id=uuid.uuid4(), org_id=ctx.org.id, thread_id=thread.id, tag_id=tag_id
            )
        )
    await ctx.session.commit()
    return {"thread_id": thread.id, "tag_ids": sorted(str(t) for t in wanted)}
