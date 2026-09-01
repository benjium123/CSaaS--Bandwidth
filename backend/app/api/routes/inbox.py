from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import MessageThread, OrgMembership, Tag, ThreadLabel
from app.services import inbox as inbox_svc
from app.services import inbox_access as inbox_access_svc

router = APIRouter(prefix="/api/v1", tags=["inbox"])


class ThreadPatchIn(BaseModel):
    status: str | None = None
    assigned_user_id: uuid.UUID | None = None
    # Distinguishes "unassign" from "not supplied".
    clear_assignee: bool = False


class LabelsIn(BaseModel):
    tag_ids: list[uuid.UUID] = []


class ThreadAiIn(BaseModel):
    #: P10 DR-5: the only two states an operator may set explicitly. `off` is never set
    #: here - it is the thread's birth state, entered only by never having had an
    #: sms_enabled profile see it.
    state: str = Field(pattern="^(active|handed_off)$")


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
    result = await inbox_svc.list_inbox(
        ctx.session,
        ctx.org.id,
        ctx.actor_user_id,
        inbox_svc.InboxFilters(status=status, assigned=assigned, q=q, label_id=label_id),
        cursor=cursor,
        limit=limit,
    )
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin:
        # P15: post-filter the aggregate's page by inbox access. A page can come back
        # shorter than `limit` when some of its threads are on numbers this caller cannot
        # see - `next_cursor` (from the UNFILTERED page) still walks correctly, it just
        # means a client may need an extra round trip to fill a visually full page.
        result = dict(result)
        result["items"] = [
            item for item in result["items"] if access.can_view(item["thread"]["our_e164"])
        ]
    return result


async def _get_thread(
    ctx: OrgContext, thread_id: uuid.UUID, *, require_use: bool = False
) -> MessageThread:
    # Scoped by the session guard: another org's thread id is a 404 here.
    thread = await ctx.session.get(MessageThread, thread_id)
    if thread is None:
        raise NotFoundError("Thread not found")

    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin:
        # An inaccessible thread's detail is a 404, never a 403 - don't leak existence.
        if not access.can_view(thread.our_e164):
            raise NotFoundError("Thread not found")
        if require_use and not access.can_use(thread.our_e164):
            raise PermissionDeniedError("You do not have manage access to this inbox")
    return thread


@router.patch("/threads/{thread_id}")
async def patch_thread(
    thread_id: uuid.UUID,
    payload: ThreadPatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:manage"))],
) -> dict:
    thread = await _get_thread(ctx, thread_id, require_use=True)

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
        "ai_state": thread.ai_state,
    }


@router.get("/threads/{thread_id}/ai")
async def get_thread_ai_state(
    thread_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> dict:
    thread = await _get_thread(ctx, thread_id)
    return {"id": thread.id, "ai_state": thread.ai_state}


@router.post("/threads/{thread_id}/ai")
async def set_thread_ai_state(
    thread_id: uuid.UUID,
    payload: ThreadAiIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:manage"))],
) -> dict:
    """The re-arm / take-over pair (plan DR-5). Re-arming ("active") is the only way a
    `handed_off` thread ever answers again - the bot itself never does this. Setting
    "handed_off" is an explicit manual take-over, the same effect a human's own reply in
    an `active` thread already has implicitly (see messaging.send_message)."""
    thread = await _get_thread(ctx, thread_id, require_use=True)
    thread.ai_state = payload.state
    if payload.state == "active":
        # DR-7: the turn ceiling counts replies SINCE this (re)arm - reset the clock every
        # time an operator explicitly arms the thread, exactly like the bot's own
        # off->active auto-arm does (sms_agent._maybe_reply_inner).
        thread.ai_armed_at = datetime.now(timezone.utc)
    await ctx.session.commit()
    return {"id": thread.id, "ai_state": thread.ai_state}


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
    thread = await _get_thread(ctx, thread_id, require_use=True)

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
