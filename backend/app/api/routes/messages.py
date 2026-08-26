from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError
from app.models import Message, MessageThread
from app.providers.base import get_carrier
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
        created_at=m.created_at,
    )


@router.post("/messages", response_model=MessageOut, status_code=201)
async def send(
    payload: SendIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
    carrier: Annotated[object, Depends(get_carrier)],
) -> MessageOut:
    """201 whenever a row was created — INCLUDING status="rejected".

    Carrier rejection is data, not an HTTP error (phase-1-plan DR-7): the channel is async
    end to end, so a client that must already handle DLR-driven failure reads one uniform
    resource rather than branching on HTTP status.
    """
    to_norm = to_e164(payload.to)
    from_norm = await select_sender(
        ctx.session,
        ctx.org.id,
        to_norm,
        requested=to_e164(payload.from_) if payload.from_ else None,
        allow_reassign=payload.allow_reassign,
    )
    message = await svc.send_message(
        ctx.session, ctx.org.id, carrier, to_e164=to_norm, from_e164=from_norm, body=payload.body
    )
    return _out(message)


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ThreadOut]:
    stmt = (
        sa.select(MessageThread)
        .order_by(MessageThread.last_message_at.desc().nullslast())
        .limit(limit)
        .offset(offset)
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
    return _out(message)
