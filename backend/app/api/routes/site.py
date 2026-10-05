"""Public website: the chat assistant, its handoff to a person, and "Talk to sales".

  POST /api/v1/public/site-chat/ask                 answer a question the site's FAQ could not
  POST /api/v1/public/site-chat/handoff             hand the chat to the team
                                                    (returns a bearer token)
  GET  /api/v1/public/site-chat/{id}/messages       the visitor polls for replies (token required)
  POST /api/v1/public/site-chat/{id}/messages       the visitor writes after the handoff
  POST /api/v1/public/sales-leads                   the "Talk to sales" form
  POST /api/v1/support/ask                          the assistant, for a signed-in customer
                                                    (sees their account)
  POST /api/v1/support/chat                         a signed-in customer starts a stored chat
  /api/v1/ops/site/...                              operators read leads and answer chats

Everything public is unauthenticated, so: every route is rate-limited per IP, every field is
length-capped, and a chat is reachable only with its token (stored as SHA-256). The assistant
(services/support_agent.py) answers from the help docs and the conversation; in a stored chat it
answers each visitor message until it hands off or an operator takes over (``ai_state``).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    OperatorContext,
    OrgContext,
    get_current_org,
    get_current_user,
    get_settings,
    require_operator_permission,
)
from app.config import Settings
from app.db.session import get_session
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import User
from app.models.security import SecurityAlert
from app.models.site import SiteChat, SiteChatMessage, SiteLead
from app.rate_limit import enforce_rate_limit
from app.services import identity as identity_svc
from app.services import support_agent

log = structlog.get_logger()

public_router = APIRouter(prefix="/api/v1/public", tags=["site"])
ops_router = APIRouter(prefix="/api/v1/ops/site", tags=["ops-site"])
#: Signed-in customers (0095): the console's "Chat with us" handoff.
customer_router = APIRouter(prefix="/api/v1/support", tags=["support-chat"])
Reader = Annotated[OperatorContext, Depends(require_operator_permission("ops:read"))]
Site = Annotated[OperatorContext, Depends(require_operator_permission("ops:site"))]
Session = Annotated[AsyncSession, Depends(get_session)]

#: Hours a person is expected to answer a handed-off chat.
STAFFED_TZ = ZoneInfo("America/Chicago")
STAFFED_DAYS = range(0, 5)  # Monday-Friday
STAFFED_HOURS = range(9, 18)  # 9:00-17:59

MAX_TRANSCRIPT = 40
#: Paid assistant calls allowed per day across ALL visitors; past it the chat offers a person.
ASK_GLOBAL_DAILY_MAX = 3000
#: Assistant replies in one stored chat before it hands the chat to the team regardless.
AI_REPLIES_PER_CHAT = 40
#: ``site_chats.ai_state`` values in which the assistant answers visitor messages:
#: "active" = the assistant alone (the team is not alerted); "assist" = a person was asked
#: for out of staffed hours, so the team is alerted as usual AND the assistant keeps helping.
AI_ANSWERS = ("active", "assist")
NOT_SURE = "I'm not sure about that one. A person from our team can help."
TEAM_TAKES_OVER = (
    "I'll pass this to our team. People answer Monday-Friday 9am-6pm Central; "
    "otherwise we reply by email, usually within one business day."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def staffed_now(now: datetime | None = None) -> bool:
    local = (now or _now()).astimezone(STAFFED_TZ)
    return local.weekday() in STAFFED_DAYS and local.hour in STAFFED_HOURS


def _ip(request: Request) -> str:
    return identity_svc.client_ip(request) or "unknown"


# --- the assistant --------------------------------------------------------------------

class Turn(BaseModel):
    role: Literal["visitor", "assistant"]
    text: str = Field(max_length=1000)


class Fact(BaseModel):
    q: str = Field(max_length=300)
    a: str = Field(max_length=900)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=30)
    context: list[Fact] = Field(default_factory=list, max_length=10)


async def _global_ask_budget_spent(settings: Settings) -> bool:
    from app import rate_limit

    key = "POST:site-chat-ask:global"
    window = 86400
    shared = await rate_limit._redis_allow(settings, key, ASK_GLOBAL_DAILY_MAX, window)
    retry = shared if shared is not None else rate_limit._limiter.allow(
        key, ASK_GLOBAL_DAILY_MAX, window
    )
    return bool(retry)


async def _agent_answer(
    settings: Settings, history: list[tuple[str, str]], account: dict | None
) -> support_agent.Reply:
    if await _global_ask_budget_spent(settings):
        return support_agent.Reply("", True)
    return await support_agent.reply(settings, history, account)


def _ask_out(answer: support_agent.Reply) -> dict:
    if answer.handoff or not answer.text:
        return {"answer": (answer.text or NOT_SURE)[:1000], "handoff": True}
    # Capped at the Turn limit: the widget sends past answers back as history.
    return {"answer": answer.text[:1000], "handoff": False}


@public_router.post("/site-chat/ask")
async def site_chat_ask(
    payload: AskIn, request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    await enforce_rate_limit(request, f"site-chat-ask:{_ip(request)}")
    history = [(t.role, t.text) for t in payload.history] + [("visitor", payload.question)]
    return _ask_out(await _agent_answer(settings, history, None))


@customer_router.post("/ask")
async def support_ask(
    payload: AskIn,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """The assistant for a signed-in customer: same as /site-chat/ask plus a read-only summary
    of THEIR workspace, taken from the session, never from the client."""
    await enforce_rate_limit(request, f"support-ask:{user.id}")
    history = [(t.role, t.text) for t in payload.history] + [("visitor", payload.question)]
    account = await support_agent.account_summary(ctx.org.id)
    return _ask_out(await _agent_answer(settings, history, account))


async def _ai_turn(settings: Settings, chat_id: uuid.UUID, trigger_id: uuid.UUID) -> None:
    """Background: the assistant answers the visitor message ``trigger_id`` in a stored chat,
    with the whole chat as history. Skips when a newer message arrived (a newer visitor
    message's own turn answers with the fuller history) or an operator took over meanwhile.
    Never raises."""
    from app.db.session import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            chat = await session.get(SiteChat, chat_id)
            if chat is None or chat.ai_state not in AI_ANSWERS or chat.status == "closed":
                return
            rows = await _chat_messages(session, chat_id)
            if not rows or rows[-1].id != trigger_id:
                return
            history = [(m.role, m.text) for m in rows]
            spent = sum(1 for m in rows if m.role == "ai") >= AI_REPLIES_PER_CHAT
            org_id = chat.org_id
        account = await support_agent.account_summary(org_id) if org_id else None
        if spent:
            answer = support_agent.Reply("", True)
        else:
            answer = await _agent_answer(settings, history, account)

        async with get_sessionmaker()() as session:
            chat = await session.get(SiteChat, chat_id)
            if chat is None or chat.ai_state not in AI_ANSWERS:
                return
            was = chat.ai_state
            rows = await _chat_messages(session, chat_id)
            if not rows or rows[-1].id != trigger_id:
                return
            now = _now()
            text = answer.text or TEAM_TAKES_OVER
            session.add(
                SiteChatMessage(
                    id=uuid.uuid4(), chat_id=chat.id, role="ai", text=text, created_at=now
                )
            )
            chat.last_message_at = now
            # In "assist" the team was already alerted when the person was asked for.
            alert = answer.handoff and was == "active"
            if answer.handoff:
                chat.ai_state = "handoff"
            if alert:
                session.add(_handoff_alert(chat, "The assistant handed a chat to the team."))
            await session.commit()
            log.info("site_chat.ai_reply", chat_id=str(chat_id), handoff=answer.handoff)
            if alert:
                _alert(
                    settings, chat, title=f"Chat needs a person: {chat.name}", body=_last_text(rows)
                )
    except Exception:
        log.warning("site_chat.ai_turn_failed", chat_id=str(chat_id), exc_info=True)


async def _chat_messages(session: AsyncSession, chat_id: uuid.UUID) -> list[SiteChatMessage]:
    return list(
        (
            await session.execute(
                sa.select(SiteChatMessage)
                .where(SiteChatMessage.chat_id == chat_id)
                .order_by(SiteChatMessage.created_at)
            )
        )
        .scalars()
        .all()
    )


def _last_text(rows: list[SiteChatMessage]) -> str:
    for m in reversed(rows):
        if m.role == "visitor" and m.text.strip():
            return m.text.strip()[:200]
    return "Needs a reply"


# --- handoff to a person --------------------------------------------------------------

class HandoffIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str | None = Field(default=None, max_length=40)
    sms_consent: bool = False
    page: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=60)
    transcript: list[Turn] = Field(default_factory=list, max_length=200)


async def _create_chat(
    session: AsyncSession,
    request: Request,
    *,
    name: str,
    email: str,
    phone: str | None,
    sms_consent: bool,
    reason: str | None,
    page: str | None,
    transcript: list[Turn],
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    want_person: bool = True,
    background: BackgroundTasks | None = None,
) -> dict:
    """Creates a stored chat (visitor or signed-in customer).

    ``want_person``: the visitor asked for a person, so the team is alerted. The assistant
    answers alone (``ai_state="active"``) when nobody asked for a person, and alongside the team
    (``"assist"``) out of staffed hours so the visitor is not left alone until the email reply.
    When nobody asked for a person it answers the transcript's last visitor message right away."""
    token = secrets.token_urlsafe(32)
    now = _now()
    chat = SiteChat(
        id=uuid.uuid4(),
        token_hash=_hash(token),
        status="waiting",
        name=name.strip(),
        email=email.strip().lower(),
        phone=(phone or "").strip() or None,
        sms_consent=sms_consent and bool((phone or "").strip()),
        reason=reason,
        page=page,
        ip=_ip(request),
        last_message_at=now,
        last_visitor_at=now,
        org_id=org_id,
        user_id=user_id,
        ai_state="active" if not want_person else (None if staffed_now(now) else "assist"),
    )
    session.add(chat)
    last_visitor: SiteChatMessage | None = None
    # Microsecond steps keep the transcript in order (it all arrives in one request).
    for i, turn in enumerate(transcript[-MAX_TRANSCRIPT:]):
        m = SiteChatMessage(
            id=uuid.uuid4(), chat_id=chat.id, role=turn.role, text=turn.text,
            created_at=now + timedelta(microseconds=i),
        )
        session.add(m)
        last_visitor = m if turn.role == "visitor" else None
    who = "A customer" if org_id else "A website visitor"
    if want_person:
        session.add(_handoff_alert(chat, f"{who} asked for a person. Answer in Ops -> Website."))
    await session.commit()
    log.info(
        "site_chat.created", chat_id=str(chat.id), reason=chat.reason, customer=org_id is not None,
        want_person=want_person, ai=chat.ai_state,
    )
    if want_person:
        _alert_operators(
            request, chat, title=f"New chat: {chat.name}", body=_last_visitor_text(transcript)
        )
    elif last_visitor is not None and background is not None:
        background.add_task(_ai_turn, request.app.state.settings, chat.id, last_visitor.id)
    return {
        "chat_id": str(chat.id),
        "token": token,
        "staffed": staffed_now(now),
        "ai": chat.ai_state in AI_ANSWERS,
    }


def _handoff_alert(chat: SiteChat, action: str) -> SecurityAlert:
    return SecurityAlert(
        id=uuid.uuid4(),
        kind="site_chat_handoff",
        detail={
            "chat_id": str(chat.id), "reason": chat.reason, "page": chat.page, "action": action
        },
    )


def _last_visitor_text(transcript: list[Turn]) -> str:
    for turn in reversed(transcript):
        if turn.role == "visitor" and turn.text.strip():
            return turn.text.strip()[:200]
    return "Asked to talk to a person"


def _alert_operators(request: Request, chat: SiteChat, *, title: str, body: str) -> None:
    """Best-effort phone push to whoever should answer: the assignee, else every active
    operator allowed to answer chats. Never fails the request that triggered it."""
    _alert(request.app.state.settings, chat, title=title, body=body)


def _alert(settings: Settings, chat: SiteChat, *, title: str, body: str) -> None:
    from app.services import device_push, fcm

    try:
        if not fcm.enabled(settings):
            return
        device_push.schedule(
            settings, _push_operators(settings, chat.id, chat.assigned_user_id, title, body)
        )
    except Exception:
        log.warning("site_chat.alert_failed", exc_info=True)


async def _push_operators(
    settings: Settings, chat_id: uuid.UUID, assignee: uuid.UUID | None, title: str, body: str
) -> None:
    from app.db.session import get_sessionmaker
    from app.models.security import PlatformOperator
    from app.services import device_push
    from app.services import operators as operators_svc

    if assignee is not None:
        user_ids = [assignee]
    else:
        now = _now()
        async with get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    sa.select(PlatformOperator.user_id, PlatformOperator.role).where(
                        PlatformOperator.is_active.is_(True),
                        sa.or_(
                            PlatformOperator.expires_at.is_(None), PlatformOperator.expires_at > now
                        ),
                    )
                )
            ).all()
        user_ids = [uid for uid, role in rows if operators_svc.has_permission(role, "ops:site")]
    if not user_ids:
        return
    await device_push.push_to_users(
        settings,
        user_ids,
        "support_chat",
        {"chat_id": str(chat_id)},
        title=title[:120],
        body=body[:200],
        collapse_key=f"support-chat-{chat_id}",
    )


@public_router.post("/site-chat/handoff", status_code=201)
async def site_chat_handoff(payload: HandoffIn, request: Request, session: Session) -> dict:
    ip = _ip(request)
    await enforce_rate_limit(request, f"site-chat-handoff:{ip}")
    return await _create_chat(
        session,
        request,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        sms_consent=payload.sms_consent,
        reason=payload.reason,
        page=payload.page,
        transcript=payload.transcript,
    )


class CustomerHandoffIn(BaseModel):
    page: str | None = Field(default=None, max_length=200)
    transcript: list[Turn] = Field(default_factory=list, max_length=200)
    #: True from "Talk to a person"; False (the app's chat screen) = the assistant answers first.
    want_person: bool = False


@customer_router.post("/chat", status_code=201)
async def support_chat_handoff(
    payload: CustomerHandoffIn,
    request: Request,
    background: BackgroundTasks,
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """A signed-in customer starts a chat (the assistant first, or a person when asked).
    Identity and workspace come from the session, never from the client, so operators can trust who
    they are talking to."""
    await enforce_rate_limit(request, f"support-chat-handoff:{user.id}")
    return await _create_chat(
        ctx.session,
        request,
        name=(user.full_name or user.email.split("@")[0])[:120],
        email=user.email,
        phone=None,
        sms_consent=False,
        reason="customer",
        page=payload.page,
        transcript=payload.transcript,
        org_id=ctx.org.id,
        user_id=user.id,
        want_person=payload.want_person,
        background=background,
    )


async def _chat_for_visitor(session: AsyncSession, chat_id: uuid.UUID, token: str) -> SiteChat:
    chat = await session.get(SiteChat, chat_id)
    if chat is None or not hmac.compare_digest(chat.token_hash, _hash(token)):
        raise NotFoundError("Chat not found")
    return chat


def _message_out(m: SiteChatMessage) -> dict:
    return {"id": str(m.id), "role": m.role, "text": m.text, "at": m.created_at.isoformat()}


def _visitor_message_out(m: SiteChatMessage) -> dict:
    """What the visitor's client sees: the assistant's replies come as ``agent`` messages
    (every client already renders those) flagged ``ai``."""
    out = _message_out(m)
    if m.role == "ai":
        out["role"] = "agent"
        out["ai"] = True
    return out


@public_router.get("/site-chat/{chat_id}/messages")
async def site_chat_messages(
    chat_id: uuid.UUID,
    request: Request,
    session: Session,
    token: Annotated[str, Query(max_length=100)],
    after: Annotated[str | None, Query(max_length=40)] = None,
    history: bool = False,
) -> dict:
    """Replies since ``after``. ``history=true`` also returns the visitor's own messages, so an
    app reopening a chat can redraw the whole conversation (the site widget keeps its own copy)."""
    await enforce_rate_limit(request, f"site-chat-poll:{_ip(request)}")
    chat = await _chat_for_visitor(session, chat_id, token)
    roles = ("visitor", "agent", "ai", "system") if history else ("agent", "ai", "system")
    stmt = sa.select(SiteChatMessage).where(
        SiteChatMessage.chat_id == chat.id, SiteChatMessage.role.in_(roles)
    )
    if after:
        try:
            since = datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailedError("after must be an ISO timestamp") from exc
        stmt = stmt.where(SiteChatMessage.created_at > since)
    rows = (
        await session.execute(stmt.order_by(SiteChatMessage.created_at).limit(100))
    ).scalars().all()
    return {
        "status": chat.status,
        "agent_name": chat.agent_name,
        "ai": chat.ai_state in AI_ANSWERS,
        "messages": [_visitor_message_out(m) for m in rows],
    }


class VisitorMessageIn(BaseModel):
    token: str = Field(max_length=100)
    text: str = Field(min_length=1, max_length=1000)


@public_router.post("/site-chat/{chat_id}/messages", status_code=201)
async def site_chat_visitor_message(
    chat_id: uuid.UUID,
    payload: VisitorMessageIn,
    request: Request,
    session: Session,
    background: BackgroundTasks,
) -> dict:
    await enforce_rate_limit(request, f"site-chat-send:{_ip(request)}")
    chat = await _chat_for_visitor(session, chat_id, payload.token)
    if chat.status == "closed":
        raise ConflictError("This chat has ended")
    now = _now()
    msg = SiteChatMessage(
        id=uuid.uuid4(), chat_id=chat.id, role="visitor", text=payload.text, created_at=now
    )
    session.add(msg)
    chat.last_message_at = now
    chat.last_visitor_at = now
    await session.commit()
    if chat.ai_state in AI_ANSWERS:
        background.add_task(_ai_turn, request.app.state.settings, chat.id, msg.id)
    if chat.ai_state != "active":
        _alert_operators(request, chat, title=f"{chat.name} wrote", body=payload.text[:200])
    return {"id": str(msg.id), "at": now.isoformat()}


# --- talk to sales --------------------------------------------------------------------

class LeadIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=160)
    team_size: str = Field(min_length=1, max_length=20)
    numbers_needed: str = Field(min_length=1, max_length=20)
    switching_from: str | None = Field(default=None, max_length=60)
    message: str | None = Field(default=None, max_length=4000)
    sms_consent: bool = False
    plan: str | None = Field(default=None, max_length=32)
    page: str | None = Field(default=None, max_length=200)


@public_router.post("/sales-leads", status_code=202)
async def sales_lead(payload: LeadIn, request: Request, session: Session) -> dict:
    ip = _ip(request)
    await enforce_rate_limit(request, f"sales-lead:{ip}")
    lead = SiteLead(
        id=uuid.uuid4(),
        name=payload.name.strip(),
        email=payload.email.strip().lower(),
        phone=(payload.phone or "").strip() or None,
        company=(payload.company or "").strip() or None,
        team_size=payload.team_size,
        numbers_needed=payload.numbers_needed,
        switching_from=payload.switching_from,
        message=payload.message,
        sms_consent=payload.sms_consent and bool((payload.phone or "").strip()),
        plan=payload.plan,
        page=payload.page,
        ip=ip,
    )
    session.add(lead)
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="sales_lead",
            detail={
                "lead_id": str(lead.id),
                "team_size": lead.team_size,
                "numbers_needed": lead.numbers_needed,
                "action": "New Talk to sales enquiry. See Ops -> Website.",
            },
        )
    )
    await session.commit()
    log.info("site.sales_lead", lead_id=str(lead.id))
    return {"received": True}


# --- operators ------------------------------------------------------------------------

def _chat_summary(chat: SiteChat, last: str | None) -> dict:
    return {
        "id": str(chat.id),
        "status": chat.status,
        "name": chat.name,
        "email": chat.email,
        "phone": chat.phone,
        "sms_consent": chat.sms_consent,
        "reason": chat.reason,
        "page": chat.page,
        "agent_name": chat.agent_name,
        "created_at": chat.created_at.isoformat(),
        "last_message_at": chat.last_message_at.isoformat() if chat.last_message_at else None,
        "last_message": last,
        "kind": "customer" if chat.org_id else "visitor",
        "org_id": str(chat.org_id) if chat.org_id else None,
        "assigned_user_id": str(chat.assigned_user_id) if chat.assigned_user_id else None,
        "unread": _is_unread(chat),
        "ai_state": chat.ai_state,
    }


def _is_unread(chat: SiteChat) -> bool:
    """The customer wrote after an operator last opened the chat (closed chats never count, nor
    chats the assistant is still answering)."""
    if chat.status == "closed" or chat.last_visitor_at is None or chat.ai_state == "active":
        return False
    return chat.agent_read_at is None or chat.last_visitor_at > chat.agent_read_at


@ops_router.get("/chats")
async def ops_list_chats(
    op: Reader,
    status: Annotated[str | None, Query(max_length=16)] = None,
    kind: Annotated[Literal["customer", "visitor"] | None, Query()] = None,
    mine: bool = False,
) -> dict:
    session = op.session
    stmt = sa.select(SiteChat).order_by(SiteChat.last_message_at.desc().nullslast()).limit(200)
    if status == "open":
        stmt = stmt.where(SiteChat.status != "closed")
    elif status:
        stmt = stmt.where(SiteChat.status == status)
    if kind == "customer":
        stmt = stmt.where(SiteChat.org_id.is_not(None))
    elif kind == "visitor":
        stmt = stmt.where(SiteChat.org_id.is_(None))
    if mine:
        stmt = stmt.where(SiteChat.assigned_user_id == op.user.id)
    chats = (await session.execute(stmt)).scalars().all()
    out = []
    for chat in chats:
        last = (
            await session.execute(
                sa.select(SiteChatMessage.text)
                .where(SiteChatMessage.chat_id == chat.id)
                .order_by(SiteChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        out.append(_chat_summary(chat, last))
    names = await _org_names(session, {c.org_id for c in chats if c.org_id})
    assignees = await _user_names(
        session, {c.assigned_user_id for c in chats if c.assigned_user_id}
    )
    for item in out:
        item["org_name"] = names.get(item["org_id"]) if item["org_id"] else None
        item[
            "assigned_name"
        ] = assignees.get(item["assigned_user_id"]) if item["assigned_user_id"] else None
    return {"chats": out, "staffed": staffed_now()}


async def _org_names(session: AsyncSession, ids: set) -> dict[str, str]:
    from app.models.org import Org

    if not ids:
        return {}
    rows = (
        await session.execute(
            sa.select(
                Org.id, Org.name
            ).where(Org.id.in_(ids)).execution_options(allow_unscoped=True)
        )
    ).all()
    return {str(i): n for i, n in rows}


async def _user_names(session: AsyncSession, ids: set) -> dict[str, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            sa.select(User.id, User.full_name, User.email)
            .where(User.id.in_(ids))
            .execution_options(allow_unscoped=True)
        )
    ).all()
    return {str(i): (n or e) for i, n, e in rows}


@ops_router.get("/chats/unread")
async def ops_unread_chats(op: Reader) -> dict:
    """Open chats with a customer message the operator has not seen, limited to chats that are
    unassigned or assigned to the caller (what the console alerts on)."""
    rows = (
        await op.session.execute(
            sa.select(SiteChat)
            .where(
                SiteChat.status != "closed",
                SiteChat.last_visitor_at.is_not(None),
                sa.or_(
                    SiteChat.agent_read_at.is_(None),
                    SiteChat.last_visitor_at > SiteChat.agent_read_at
                ),
                sa.or_(
                    SiteChat.assigned_user_id.is_(None), SiteChat.assigned_user_id == op.user.id
                ),
                sa.or_(SiteChat.ai_state.is_(None), SiteChat.ai_state != "active"),
            )
            .order_by(SiteChat.last_visitor_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return {
        "count": len(rows),
        "chats": [
            {
                "id": str(c.id),
                "name": c.name,
                "kind": "customer" if c.org_id else "visitor",
                "last_visitor_at": c.last_visitor_at.isoformat() if c.last_visitor_at else None,
            }
            for c in rows
        ],
    }


@ops_router.get("/chats/{chat_id}")
async def ops_get_chat(chat_id: uuid.UUID, op: Reader) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    rows = (
        await op.session.execute(
            sa.select(SiteChatMessage)
            .where(SiteChatMessage.chat_id == chat.id)
            .order_by(SiteChatMessage.created_at)
        )
    ).scalars().all()
    out = {**_chat_summary(chat, None), "messages": [_message_out(m) for m in rows]}
    out["customer"] = await _customer_context(chat) if chat.org_id else None
    if chat.assigned_user_id:
        out["assigned_name"] = (await _user_names(op.session, {chat.assigned_user_id})).get(
            str(chat.assigned_user_id)
        )
    return out


async def _customer_context(chat: SiteChat) -> dict | None:
    """Workspace facts an operator needs while answering. Read in its own session because the
    billing helpers set a tenant context on the session they are given."""
    from app.db.session import get_sessionmaker
    from app.models.org import Org
    from app.services import credits, plans

    try:
        async with get_sessionmaker()() as session:
            org = (
                await session.execute(
                    sa.select(
                        Org
                    ).where(Org.id == chat.org_id).execution_options(allow_unscoped=True)
                )
            ).scalar_one_or_none()
            if org is None:
                return None
            plan = await plans.plan_for(session, org.id)
            balance_micros = await credits.balance(session, org.id)
            return {
                "org_id": str(org.id),
                "org_name": org.name,
                "plan": plan.name if plan else None,
                "balance_usd": round(balance_micros / 1_000_000, 2),
            }
    except Exception:
        log.warning("site_chat.customer_context_failed", exc_info=True)
        return None


@ops_router.post("/chats/{chat_id}/read")
async def ops_mark_read(chat_id: uuid.UUID, op: Site) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    chat.agent_read_at = _now()
    await op.session.commit()
    return {"unread": False}


class AssignIn(BaseModel):
    #: true = assign to the caller, false = unassign.
    to_me: bool


@ops_router.post("/chats/{chat_id}/assign")
async def ops_assign(chat_id: uuid.UUID, payload: AssignIn, op: Site) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    chat.assigned_user_id = op.user.id if payload.to_me else None
    if payload.to_me and chat.ai_state in ("active", "assist", "handoff"):
        chat.ai_state = "off"  # a person took the chat: the assistant stops answering
    await op.session.commit()
    names = await _user_names(op.session, {chat.assigned_user_id}) if chat.assigned_user_id else {}
    return {
        "assigned_user_id": str(chat.assigned_user_id) if chat.assigned_user_id else None,
        "assigned_name": names.get(str(chat.assigned_user_id)) if chat.assigned_user_id else None,
    }


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _operator_name(op: OperatorContext) -> str:
    user = op.user
    for attr in ("first_name", "name", "display_name", "full_name"):
        value = getattr(user, attr, None)
        if value:
            return str(value).split(" ")[0][:120]
    return "Ringlite team"


@ops_router.post("/chats/{chat_id}/reply", status_code=201)
async def ops_reply(chat_id: uuid.UUID, payload: ReplyIn, op: Site) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    if chat.status == "closed":
        raise ConflictError("This chat has ended")
    now = _now()
    msg = SiteChatMessage(
        id=uuid.uuid4(), chat_id=chat.id, role="agent", text=payload.text, created_at=now
    )
    op.session.add(msg)
    chat.status = "active"
    chat.agent_name = chat.agent_name or _operator_name(op)
    chat.last_message_at = now
    chat.agent_read_at = now
    if chat.ai_state in ("active", "assist", "handoff"):
        chat.ai_state = "off"  # a person replied: the assistant stops answering
    await op.session.commit()
    return _message_out(msg)


@ops_router.post("/chats/{chat_id}/close")
async def ops_close(chat_id: uuid.UUID, op: Site) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    if chat.status != "closed":
        now = _now()
        op.session.add(
            SiteChatMessage(
                id=uuid.uuid4(),
                chat_id=chat.id,
                role="system",
                text="The chat was closed.",
                created_at=now
            )
        )
        chat.status = "closed"
        chat.last_message_at = now
        await op.session.commit()
    return {"status": "closed"}


@ops_router.get("/leads")
async def ops_list_leads(op: Reader) -> dict:
    rows = (
        await op.session.execute(
            sa.select(SiteLead).order_by(SiteLead.created_at.desc()).limit(200)
        )
    ).scalars().all()
    return {
        "leads": [
            {
                "id": str(r.id),
                "status": r.status,
                "name": r.name,
                "email": r.email,
                "phone": r.phone,
                "company": r.company,
                "team_size": r.team_size,
                "numbers_needed": r.numbers_needed,
                "switching_from": r.switching_from,
                "message": r.message,
                "sms_consent": r.sms_consent,
                "plan": r.plan,
                "page": r.page,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


class LeadStatusIn(BaseModel):
    status: Literal["new", "contacted", "won", "lost"]


@ops_router.post("/leads/{lead_id}/status")
async def ops_lead_status(lead_id: uuid.UUID, payload: LeadStatusIn, op: Site) -> dict:
    lead = await op.session.get(SiteLead, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found")
    lead.status = payload.status
    await op.session.commit()
    return {"status": lead.status}
