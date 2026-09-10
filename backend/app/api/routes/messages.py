from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app import routing
from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import Message, MessageThread
from app.providers.base import get_carrier
from app.services import inbox_access as inbox_access_svc
from app.services import links as links_svc
from app.services import media as media_svc
from app.services import messaging as svc
from app.services.sender import select_sender

router = APIRouter(prefix="/api/v1", tags=["messaging"])


def _normalise_schedule(moment: datetime | None) -> datetime | None:
    """Validate a send-later instant, or return None for "send it now".

    A naive value is read as UTC rather than refused: the composer always sends an offset,
    but an API client that does not should get a predictable interpretation instead of a
    422 it cannot debug. Both bounds are refusals a person can act on - a moment already
    past is almost always a timezone mistake, and a moment years away is almost always a
    typo'd year.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if moment <= now:
        raise ValidationFailedError("Pick a time in the future to send this later")
    if moment > now + timedelta(days=svc.MAX_SCHEDULE_DAYS):
        raise ValidationFailedError(
            f"Messages can be scheduled up to {svc.MAX_SCHEDULE_DAYS} days ahead"
        )
    return moment


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
    #: P28 send-later. An ISO instant. The composer builds it from the time the sender
    #: picked in the CONTACT's timezone; by the time it arrives here it is an absolute
    #: moment, so the server never has to guess whose clock a wall time belonged to. A
    #: value with no timezone is read as UTC.
    scheduled_for: datetime | None = None
    #: P28 link tracking, per message. There is no org-level default column to read
    #: (compliance_settings has no JSON/extra column and P28 adds no migration), so the
    #: composer sends this explicitly on every send.
    track_links: bool = False

    model_config = {"populate_by_name": True}


class LinkOut(BaseModel):
    """One tracked link inside an outbound message."""

    code: str
    target_url: str
    clicks: int


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
    #: P21: one plain sentence saying WHY this message went out the way it did
    #: ("Sent via Telnyx - cheapest healthy route", "Failed over to Telnyx - Bandwidth
    #: unavailable"). None for inbound messages and for anything sent before P21.
    route_reason: str | None = None
    #: P28: the same failure in words a person can act on ("This number can't receive
    #: text messages."). None whenever the message has not failed.
    failure_reason_public: str | None = None
    #: P28 send-later: the moment this message is due. None once it has been released.
    scheduled_for: datetime | None = None
    #: P28 link tracking: total clicks across every tracked link in this message.
    clicks: int = 0
    links: list[LinkOut] = []


class ThreadOut(BaseModel):
    id: uuid.UUID
    our_e164: str
    contact_e164: str
    last_message_at: datetime | None


def _out(m: Message, links: list | None = None) -> MessageOut:
    link_rows = links or []
    return MessageOut(
        failure_reason_public=m.failure_reason_public,
        scheduled_for=m.scheduled_for,
        clicks=sum(link.clicks for link in link_rows),
        links=[
            LinkOut(code=link.code, target_url=link.target_url, clicks=link.clicks)
            for link in link_rows
        ],
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
        route_reason=m.route_reason,
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
        scheduled_for=_normalise_schedule(payload.scheduled_for),
        track_links=payload.track_links,
        public_web_url=settings.public_web_url or "",
    )
    links = await links_svc.links_for_messages(ctx.session, [message.id])
    return _out(message, links.get(message.id, []))


@router.delete("/messages/{message_id}/schedule", status_code=204)
async def cancel_schedule(
    message_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
) -> Response:
    """Cancel a send-later message before it goes out.

    Gated on inbox:send, not inbox:read - cancelling somebody's message is a SEND-side
    act. The number check mirrors the read routes: a message on a number this caller
    cannot use is a 404, so cancelling never doubles as an existence oracle.
    """
    message = await ctx.session.get(Message, message_id)
    if message is None:
        raise NotFoundError("Message not found")
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin and not access.can_use(message.from_e164):
        raise NotFoundError("Message not found")
    await svc.cancel_scheduled_message(ctx.session, ctx.org.id, message_id)
    return Response(status_code=204)


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
    #: P28: "scheduled" lists only send-later messages that have not gone out yet - the
    #: composer's "Scheduled" view. Any other value is ignored rather than refused.
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[MessageOut]:
    # P15: resolve access UNCONDITIONALLY - without this, a request with no thread_id
    # filter handed back every Message row in the org regardless of inbox grants.
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    stmt = (
        sa.select(Message)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(limit)
        .offset(offset)
    )
    if thread_id is not None:
        # P15: a thread-id-addressed read is gated exactly like the thread's own detail
        # route (app/api/routes/inbox.py::_get_thread) - an inaccessible thread is a 404,
        # not a 403, so existence is not leaked either way.
        thread = await ctx.session.get(MessageThread, thread_id)
        if thread is None:
            raise NotFoundError("Thread not found")
        if not access.is_admin and not access.can_view(thread.our_e164):
            raise NotFoundError("Thread not found")
        stmt = stmt.where(Message.thread_id == thread_id)
    elif not access.is_admin:
        # P15: no thread_id - scope to threads on numbers this caller may see.
        visible = access.member_e164s | access.viewer_e164s
        stmt = stmt.where(
            Message.thread_id.in_(
                sa.select(MessageThread.id).where(MessageThread.our_e164.in_(visible))
            )
        )
    if after is not None:
        # Keyset for polling: each poll transfers only what is new.
        stmt = stmt.where(Message.created_at > after)
    if status == svc.SCHEDULED_STATUS:
        stmt = stmt.where(Message.status == svc.SCHEDULED_STATUS)
    rows = list((await ctx.session.execute(stmt)).scalars().all())
    # ONE extra query for the whole page, never one per bubble.
    links = await links_svc.links_for_messages(ctx.session, [m.id for m in rows])
    return [_out(m, links.get(m.id, [])) for m in rows]


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
    links = await links_svc.links_for_messages(ctx.session, [message.id])
    return _out(message, links.get(message.id, []))
