"""Public website: the chat assistant, its handoff to a person, and "Talk to sales".

  POST /api/v1/public/site-chat/ask                 answer a question the site's FAQ could not
  POST /api/v1/public/site-chat/handoff             hand the chat to the team (returns a bearer token)
  GET  /api/v1/public/site-chat/{id}/messages       the visitor polls for replies (token required)
  POST /api/v1/public/site-chat/{id}/messages       the visitor writes after the handoff
  POST /api/v1/public/sales-leads                   the "Talk to sales" form
  /api/v1/ops/site/...                              operators read leads and answer chats

Everything public is unauthenticated, so: every route is rate-limited per IP, every field is
length-capped, a chat is reachable only with its token (stored as SHA-256), and the assistant
answers only from the Ringlite facts the site sends plus the fixed facts below. The worst a
visitor can do with crafted "facts" is mislead themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OperatorContext, get_settings, require_operator
from app.config import Settings
from app.db.session import get_session
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.security import SecurityAlert
from app.models.site import SiteChat, SiteChatMessage, SiteLead
from app.rate_limit import enforce_rate_limit
from app.services import identity as identity_svc

log = structlog.get_logger()

public_router = APIRouter(prefix="/api/v1/public", tags=["site"])
ops_router = APIRouter(prefix="/api/v1/ops/site", tags=["ops-site"])
Reviewer = Annotated[OperatorContext, Depends(require_operator("reviewer"))]
Session = Annotated[AsyncSession, Depends(get_session)]

#: Hours a person is expected to answer a handed-off chat.
STAFFED_TZ = ZoneInfo("America/Chicago")
STAFFED_DAYS = range(0, 5)  # Monday-Friday
STAFFED_HOURS = range(9, 18)  # 9:00-17:59

HANDOFF_TOKEN = "HANDOFF"
MAX_TRANSCRIPT = 40
#: Paid assistant calls allowed per day across ALL visitors; past it the chat offers a person.
ASK_GLOBAL_DAILY_MAX = 3000

FIXED_FACTS = """- Ringlite is a business phone service: numbers, browser calling, business texting, one shared inbox.
- Calls and texts reach the 48 contiguous US states only. No Canada, Alaska, Hawaii or international.
- Ringlite never sells "unlimited" calling. Starter is pay as you go; Team includes 200 and Business 1,000 call minutes a month, shared by the workspace; then a published per-minute rate.
- Plans: Starter $15/month (1 user + 1 number, up to 5 users), Team $45/month (3 users + 3 numbers, up to 15 users), Business $130/month (10 users + 10 numbers, no user limit), Custom via sales. Extra users $15/month ($12 on Business), extra numbers $5/month.
- Paying yearly gets 2 months free: Starter $150, Team $450, Business $1,300 a year.
- Every account is identity-verified before it can call. Texting needs 10DLC carrier approval.
- Ringlite is not HIPAA compliant and does not hold SOC 2."""

SYSTEM_PROMPT = f"""You are the assistant on ringlite.io, the website of Ringlite, a business phone service.
Answer the visitor's question in 1-3 short, friendly sentences, using ONLY the facts below.
Never invent prices, limits, features, dates or promises that are not in the facts.
Never ask for passwords, card numbers or identity documents. Never discuss other topics.
If the facts do not answer the question, or the visitor needs account-specific help
(billing, refunds, a declined check, a blocked account), reply with exactly: {HANDOFF_TOKEN}

Fixed facts:
{FIXED_FACTS}"""


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
    history: list[Turn] = Field(default_factory=list, max_length=10)
    context: list[Fact] = Field(default_factory=list, max_length=10)


#: Tests replace this to avoid the network.
_client_factory = httpx.AsyncClient


async def _ask_llm(settings: Settings, payload: AskIn) -> str:
    api_key = settings.deepseek_api_key.get_secret_value().strip()
    if not api_key:
        return HANDOFF_TOKEN
    facts = "\n".join(f"Q: {f.q}\nA: {f.a}" for f in payload.context)
    messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nSite facts:\n{facts}"}]
    for turn in payload.history[-10:]:
        messages.append({"role": "user" if turn.role == "visitor" else "assistant", "content": turn.text})
    messages.append({"role": "user", "content": payload.question})
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    async with _client_factory() as client:
        res = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": settings.ai_guard_model, "messages": messages, "max_tokens": 220, "temperature": 0.2},
            timeout=20.0,
        )
        res.raise_for_status()
        return (res.json()["choices"][0]["message"]["content"] or "").strip()


async def _global_ask_budget_spent(settings: Settings) -> bool:
    from app import rate_limit

    key = "POST:site-chat-ask:global"
    window = 86400
    shared = await rate_limit._redis_allow(settings, key, ASK_GLOBAL_DAILY_MAX, window)
    retry = shared if shared is not None else rate_limit._limiter.allow(key, ASK_GLOBAL_DAILY_MAX, window)
    return bool(retry)


@public_router.post("/site-chat/ask")
async def site_chat_ask(
    payload: AskIn, request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    await enforce_rate_limit(request, f"site-chat-ask:{_ip(request)}")
    if await _global_ask_budget_spent(settings):
        return {"answer": "I'm not sure about that one. A person from our team can help.", "handoff": True}
    try:
        answer = await _ask_llm(settings, payload)
    except Exception as exc:  # noqa: BLE001 - any provider failure becomes a handoff offer
        log.warning("site_chat.ask_failed", error=str(exc)[:200])
        answer = HANDOFF_TOKEN
    if not answer or HANDOFF_TOKEN in answer:
        return {
            "answer": "I'm not sure about that one. A person from our team can help.",
            "handoff": True,
        }
    # Capped at the Turn limit: the widget sends past answers back as history.
    return {"answer": answer[:1000], "handoff": False}


# --- handoff to a person --------------------------------------------------------------

class HandoffIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str | None = Field(default=None, max_length=40)
    sms_consent: bool = False
    page: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=60)
    transcript: list[Turn] = Field(default_factory=list, max_length=200)


@public_router.post("/site-chat/handoff", status_code=201)
async def site_chat_handoff(payload: HandoffIn, request: Request, session: Session) -> dict:
    ip = _ip(request)
    await enforce_rate_limit(request, f"site-chat-handoff:{ip}")
    token = secrets.token_urlsafe(32)
    now = _now()
    chat = SiteChat(
        id=uuid.uuid4(),
        token_hash=_hash(token),
        status="waiting",
        name=payload.name.strip(),
        email=payload.email.strip().lower(),
        phone=(payload.phone or "").strip() or None,
        sms_consent=payload.sms_consent and bool((payload.phone or "").strip()),
        reason=payload.reason,
        page=payload.page,
        ip=ip,
        last_message_at=now,
    )
    session.add(chat)
    for turn in payload.transcript[-MAX_TRANSCRIPT:]:
        session.add(SiteChatMessage(id=uuid.uuid4(), chat_id=chat.id, role=turn.role, text=turn.text, created_at=now))
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="site_chat_handoff",
            detail={
                "chat_id": str(chat.id),
                "reason": chat.reason,
                "page": chat.page,
                "action": "A website visitor asked for a person. Answer in Ops -> Website.",
            },
        )
    )
    await session.commit()
    log.info("site_chat.handoff", chat_id=str(chat.id), reason=chat.reason)
    return {"chat_id": str(chat.id), "token": token, "staffed": staffed_now(now)}


async def _chat_for_visitor(session: AsyncSession, chat_id: uuid.UUID, token: str) -> SiteChat:
    chat = await session.get(SiteChat, chat_id)
    if chat is None or not hmac.compare_digest(chat.token_hash, _hash(token)):
        raise NotFoundError("Chat not found")
    return chat


def _message_out(m: SiteChatMessage) -> dict:
    return {"id": str(m.id), "role": m.role, "text": m.text, "at": m.created_at.isoformat()}


@public_router.get("/site-chat/{chat_id}/messages")
async def site_chat_messages(
    chat_id: uuid.UUID,
    request: Request,
    session: Session,
    token: Annotated[str, Query(max_length=100)],
    after: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    await enforce_rate_limit(request, f"site-chat-poll:{_ip(request)}")
    chat = await _chat_for_visitor(session, chat_id, token)
    stmt = sa.select(SiteChatMessage).where(
        SiteChatMessage.chat_id == chat.id, SiteChatMessage.role.in_(("agent", "system"))
    )
    if after:
        try:
            since = datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailedError("after must be an ISO timestamp") from exc
        stmt = stmt.where(SiteChatMessage.created_at > since)
    rows = (await session.execute(stmt.order_by(SiteChatMessage.created_at).limit(100))).scalars().all()
    return {"status": chat.status, "agent_name": chat.agent_name, "messages": [_message_out(m) for m in rows]}


class VisitorMessageIn(BaseModel):
    token: str = Field(max_length=100)
    text: str = Field(min_length=1, max_length=1000)


@public_router.post("/site-chat/{chat_id}/messages", status_code=201)
async def site_chat_visitor_message(
    chat_id: uuid.UUID, payload: VisitorMessageIn, request: Request, session: Session
) -> dict:
    await enforce_rate_limit(request, f"site-chat-send:{_ip(request)}")
    chat = await _chat_for_visitor(session, chat_id, payload.token)
    if chat.status == "closed":
        raise ConflictError("This chat has ended")
    now = _now()
    msg = SiteChatMessage(id=uuid.uuid4(), chat_id=chat.id, role="visitor", text=payload.text, created_at=now)
    session.add(msg)
    chat.last_message_at = now
    await session.commit()
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
    }


@ops_router.get("/chats")
async def ops_list_chats(
    op: Reviewer, status: Annotated[str | None, Query(max_length=16)] = None
) -> dict:
    session = op.session
    stmt = sa.select(SiteChat).order_by(SiteChat.last_message_at.desc().nullslast()).limit(200)
    if status:
        stmt = stmt.where(SiteChat.status == status)
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
    return {"chats": out, "staffed": staffed_now()}


@ops_router.get("/chats/{chat_id}")
async def ops_get_chat(chat_id: uuid.UUID, op: Reviewer) -> dict:
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
    return {**_chat_summary(chat, None), "messages": [_message_out(m) for m in rows]}


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
async def ops_reply(chat_id: uuid.UUID, payload: ReplyIn, op: Reviewer) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    if chat.status == "closed":
        raise ConflictError("This chat has ended")
    now = _now()
    msg = SiteChatMessage(id=uuid.uuid4(), chat_id=chat.id, role="agent", text=payload.text, created_at=now)
    op.session.add(msg)
    chat.status = "active"
    chat.agent_name = chat.agent_name or _operator_name(op)
    chat.last_message_at = now
    await op.session.commit()
    return _message_out(msg)


@ops_router.post("/chats/{chat_id}/close")
async def ops_close(chat_id: uuid.UUID, op: Reviewer) -> dict:
    chat = await op.session.get(SiteChat, chat_id)
    if chat is None:
        raise NotFoundError("Chat not found")
    if chat.status != "closed":
        now = _now()
        op.session.add(
            SiteChatMessage(id=uuid.uuid4(), chat_id=chat.id, role="system", text="The chat was closed.", created_at=now)
        )
        chat.status = "closed"
        chat.last_message_at = now
        await op.session.commit()
    return {"status": "closed"}


@ops_router.get("/leads")
async def ops_list_leads(op: Reviewer) -> dict:
    rows = (
        await op.session.execute(sa.select(SiteLead).order_by(SiteLead.created_at.desc()).limit(200))
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
async def ops_lead_status(lead_id: uuid.UUID, payload: LeadStatusIn, op: Reviewer) -> dict:
    lead = await op.session.get(SiteLead, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found")
    lead.status = payload.status
    await op.session.commit()
    return {"status": lead.status}
