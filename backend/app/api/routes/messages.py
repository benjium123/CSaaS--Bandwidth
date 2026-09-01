from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app import routing
from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, PermissionDeniedError
from app.models import Message, MessageThread
from app.providers.base import get_carrier
from app.services import inbox_access as inbox_access_svc
from app.services import media as media_svc
from app.services import messaging as svc
from app.services.sender import select_sender

router = APIRouter(prefix="/api/v1", tags=["messaging"])


class SendIn(BaseModel):
    to: str = Field(min_length=3, max_length=32)
    body: str = Field(min_length=1, max_length=4000)
    from_: str | None = Field(default=None, alias="from", max_length=32)
    # Opt in to moving a conversation to a different number when its sticky number has
    # been retired. Without this the send fails loudly rather than silently jumping.
    allow_reassign: bool = False
    media_ids: list[uuid.UUID] = []
    #: "At will" carrier override. Honoured or refused - never silently substituted, so a
    #: named carrier that is failing returns an error rather than quietly using another.
    carrier: str | None = Field(default=None, max_length=16)

    model_config = {"populate_by_name": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    direction: str
    status: str
    from_e164: str
    to_e164: str
    body: str | None
    segment_count_est: int | None
    segment_count_carrier: int | None
    error_code: str | None
    hold_until: datetime | None
    created_at: datetime


class ThreadOut(BaseModel):
    id: uuid.UUID
    our_e164: str
    contact_e164: str
    last_message_at: datetime | None


def _out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id,
        thread_id=m.thread_id,
        direction=m.direction,
        status=m.status,
        from_e164=m.from_e164,
        to_e164=m.to_e164,
        body=m.body,
        segment_count_est=m.segment_count_est,
        segment_count_carrier=m.segment_count_carrier,
        error_code=m.error_code,
        hold_until=m.hold_until,
        created_at=m.created_at,
    )


@router.post("/messages", response_model=MessageOut, status_code=201)
async def send(
    payload: SendIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
    carrier: Annotated[object, Depends(get_carrier)],
) -> MessageOut:
    """201 whenever a row was created — INCLUDING status="rejected".

    Carrier rejection is data, not an HTTP error (phase-1-plan DR-7): the channel is async
    end to end, so a client that must already handle DLR-driven failure reads one uniform
    resource rather than branching on HTTP status.
    """
    to_norm = to_e164(payload.to)
    registry = getattr(request.app.state, "carriers", None)

    # One query, two uses: whether this contact has been spoken to decides both if a
    # sticky sender exists and whether a carrier switch is permitted.
    has_prior = await routing.has_prior_conversation(ctx.session, ctx.org.id, to_norm)

    if payload.carrier and not payload.from_:
        # An explicit carrier picks its own number: asking sticky-sender first would pick a
        # number on the wrong carrier and then fail the two against each other.
        plan = await routing.plan_route(
            ctx.session,
            ctx.org.id,
            registry,
            contact_e164=to_norm,
            requested_carrier=payload.carrier,
            is_reply_in_thread=has_prior,
            require_registration=request.app.state.settings.require_number_registration,
        )
    else:
        from_norm = await select_sender(
            ctx.session,
            ctx.org.id,
            to_norm,
            requested=to_e164(payload.from_) if payload.from_ else None,
            allow_reassign=payload.allow_reassign,
        )
        plan = await routing.plan_route(
            ctx.session,
            ctx.org.id,
            registry,
            contact_e164=to_norm,
            requested_from=to_e164(payload.from_) if payload.from_ else None,
            requested_carrier=payload.carrier,
            # Only a REAL prior conversation is sticky. For a new one select_sender has
            # merely spread across the whole pool, and treating that spread as sticky would
            # let it silently outrank the org's carrier preference.
            thread_our_number=from_norm if has_prior else None,
            is_reply_in_thread=has_prior,
            require_registration=request.app.state.settings.require_number_registration,
        )
    from_norm = plan.primary.from_e164
    # P15: the number the routing plan actually landed on must be one this caller may
    # send from - checked here (not earlier) because sticky/deterministic pool picks are
    # only known once the plan is resolved.
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.can_use(from_norm):
        raise PermissionDeniedError(f"You do not have send access to {from_norm}")
    settings = request.app.state.settings
    # The carrier fetches MMS media from a URL, so attachments become long-lived signed
    # links rather than being uploaded twice.
    media_urls = [
        media_svc.signed_url(
            settings.public_base_url or "",
            asset_id,
            settings.jwt_secret.get_secret_value(),
            media_svc.CARRIER_URL_TTL,
        )
        for asset_id in payload.media_ids
    ]
    message = await svc.send_message(
        ctx.session,
        ctx.org.id,
        carrier,
        to_e164=to_norm,
        from_e164=from_norm,
        body=payload.body,
        media_ids=payload.media_ids,
        media_urls=media_urls,
        registry=registry,
        plan=plan,
    )
    return _out(message)


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ThreadOut]:
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    stmt = (
        sa.select(MessageThread)
        .order_by(MessageThread.last_message_at.desc().nullslast())
        .limit(limit)
        .offset(offset)
    )
    if not access.is_admin:
        stmt = stmt.where(
            MessageThread.our_e164.in_(access.member_e164s | access.viewer_e164s)
        )
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [
        ThreadOut(
            id=t.id,
            our_e164=t.our_e164,
            contact_e164=t.contact_e164,
            last_message_at=t.last_message_at,
        )
        for t in rows
    ]


@router.get("/messages", response_model=list[MessageOut])
async def list_messages(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    thread_id: uuid.UUID | None = None,
    after: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[MessageOut]:
    stmt = sa.select(Message).order_by(Message.created_at.asc()).limit(limit).offset(offset)
    if thread_id is not None:
        # P15: a thread-id-addressed read is gated exactly like the thread's own detail
        # route (app/api/routes/inbox.py::_get_thread) - an inaccessible thread is a 404,
        # not a 403, so existence is not leaked either way.
        access = await inbox_access_svc.resolve_access(
            ctx.session, ctx.actor_user_id, ctx.role.permissions or []
        )
        thread = await ctx.session.get(MessageThread, thread_id)
        if thread is None:
            raise NotFoundError("Thread not found")
        if not access.is_admin and not access.can_view(thread.our_e164):
            raise NotFoundError("Thread not found")
        stmt = stmt.where(Message.thread_id == thread_id)
    if after is not None:
        # Keyset for polling: each poll transfers only what is new.
        stmt = stmt.where(Message.created_at > after)
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [_out(m) for m in rows]


@router.get("/messages/{message_id}", response_model=MessageOut)
async def get_message(
    message_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
) -> MessageOut:
    # Scoped by the session guard: another org's id is simply not found here.
    message = await ctx.session.get(Message, message_id)
    if message is None:
        raise NotFoundError("Message not found")
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin:
        thread = await ctx.session.get(MessageThread, message.thread_id)
        # An inaccessible message's thread is a 404, never a 403 - don't leak existence.
        if thread is None or not access.can_view(thread.our_e164):
            raise NotFoundError("Message not found")
    return _out(message)
