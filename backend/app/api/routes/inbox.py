from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import Call, Message, MessageThread, OrgMembership, OrgNumber, Tag, ThreadLabel
from app.services import inbox as inbox_svc
from app.services import inbox_access as inbox_access_svc
from app.services import messaging as messaging_svc

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


class ReadPairIn(BaseModel):
    our_e164: str = Field(min_length=3, max_length=32)
    contact_e164: str = Field(min_length=3, max_length=32)


class ImportantPairIn(BaseModel):
    our_e164: str = Field(min_length=3, max_length=32)
    contact_e164: str = Field(min_length=3, max_length=32)
    important: bool


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
        if payload.assigned_user_id == ctx.actor_user_id:
            # 5.14: self-claiming an unassigned thread races two operators clicking
            # "claim" on the same thread at once - a plain attribute set here would let
            # the second click silently steal it with no signal. An atomic conditional
            # UPDATE makes "someone already claimed it" observable as a 409 instead. An
            # explicit reassignment TO SOMEONE ELSE (the branch below) is a deliberate
            # management action, not a claim race, so it stays unconditional.
            # 5(b): also succeed when the thread is ALREADY assigned to the caller
            # themselves - a repeated/retried claim by the same operator is idempotent
            # (200), not a race to report as a conflict. Only someone else's assignment
            # blocks the claim (409).
            result = await ctx.session.execute(
                sa.update(MessageThread)
                .where(
                    MessageThread.id == thread.id,
                    sa.or_(
                        MessageThread.assigned_user_id.is_(None),
                        MessageThread.assigned_user_id == payload.assigned_user_id,
                    ),
                )
                .values(assigned_user_id=payload.assigned_user_id)
            )
            if result.rowcount == 0:
                raise ConflictError("This thread has already been claimed")
            thread.assigned_user_id = payload.assigned_user_id
        else:
            thread.assigned_user_id = payload.assigned_user_id

    await ctx.session.commit()
    return {
        "id": thread.id,
        "status": thread.status,
        "assigned_user_id": thread.assigned_user_id,
        "ai_state": thread.ai_state,
        "important": thread.is_important,
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
    # 5.4: marking read requires MANAGE access to this inbox, not merely VIEW - a viewer
    # who reads a thread they cannot use should not be able to zero out its unread state
    # for every member who can.
    thread = await _get_thread(ctx, thread_id, require_use=True)
    thread.last_read_at = datetime.now(timezone.utc)
    await ctx.session.commit()
    return Response(status_code=204)


@router.post("/inbox/read-pair", status_code=204)
async def mark_read_pair(
    payload: ReadPairIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> Response:
    """5.11: a call-only conversation (no inbound/outbound SMS yet, so no MessageThread
    row exists) could never be marked read - there was no thread to PATCH. This upserts
    the (our_e164, contact_e164) thread first (same helper the send/inbound paths use),
    then marks it read - after which conversations.py::_call_unread respects the new
    last_read_at exactly like it already does for a message-backed pair."""
    our_e164 = to_e164(payload.our_e164)
    contact_e164 = to_e164(payload.contact_e164)

    # 5(a): our_e164 must be a number this org actually owns - otherwise this endpoint
    # would happily fabricate a thread under a number nobody in the org can see.
    number = (
        await ctx.session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == our_e164))
    ).scalar_one_or_none()
    if number is None:
        raise NotFoundError("Number not found")

    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin:
        if not access.can_view(our_e164):
            raise NotFoundError("Conversation not found")
        if not access.can_use(our_e164):
            raise PermissionDeniedError("You do not have manage access to this inbox")

    # 5(a): only mark-read an ALREADY-EXISTING conversation (a Call or a Message on a
    # matching thread) - otherwise any caller could invent arbitrary (our, contact) pairs
    # and upsert threads for conversations that never happened.
    has_call = (
        await ctx.session.execute(
            sa.select(Call.id)
            .where(Call.our_e164 == our_e164, Call.contact_e164 == contact_e164)
            .limit(1)
        )
    ).first()
    if has_call is None:
        has_message = (
            await ctx.session.execute(
                sa.select(Message.id)
                .join(MessageThread, Message.thread_id == MessageThread.id)
                .where(
                    MessageThread.our_e164 == our_e164,
                    MessageThread.contact_e164 == contact_e164,
                )
                .limit(1)
            )
        ).first()
        if has_message is None:
            raise NotFoundError("Conversation not found")

    thread = await messaging_svc.upsert_thread(ctx.session, ctx.org.id, our_e164, contact_e164)
    thread.last_read_at = datetime.now(timezone.utc)
    await ctx.session.commit()
    return Response(status_code=204)


@router.post("/inbox/important-pair", status_code=204)
async def mark_important_pair(
    payload: ImportantPairIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> Response:
    """Toggle the "important" star on a conversation pair. Mirrors mark_read_pair
    exactly (same normalize / OrgNumber-exists / access-resolve / existing-Call-or-
    Message precondition / upsert_thread sequence) so a call-only pair can be starred
    the same way it can be marked read."""
    our_e164 = to_e164(payload.our_e164)
    contact_e164 = to_e164(payload.contact_e164)

    number = (
        await ctx.session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == our_e164))
    ).scalar_one_or_none()
    if number is None:
        raise NotFoundError("Number not found")

    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin:
        if not access.can_view(our_e164):
            raise NotFoundError("Conversation not found")
        if not access.can_use(our_e164):
            raise PermissionDeniedError("You do not have manage access to this inbox")

    has_call = (
        await ctx.session.execute(
            sa.select(Call.id)
            .where(Call.our_e164 == our_e164, Call.contact_e164 == contact_e164)
            .limit(1)
        )
    ).first()
    if has_call is None:
        has_message = (
            await ctx.session.execute(
                sa.select(Message.id)
                .join(MessageThread, Message.thread_id == MessageThread.id)
                .where(
                    MessageThread.our_e164 == our_e164,
                    MessageThread.contact_e164 == contact_e164,
                )
                .limit(1)
            )
        ).first()
        if has_message is None:
            raise NotFoundError("Conversation not found")

    thread = await messaging_svc.upsert_thread(ctx.session, ctx.org.id, our_e164, contact_e164)
    thread.is_important = payload.important
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
