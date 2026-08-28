"""AI voice agent: two machine seams for the LiveKit worker (JWT-signed with the
LiveKit secret, no OrgContext), plus human-facing agent-profile CRUD (OrgContext,
settings:read/settings:write - there is no dedicated agent:* permission yet).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.db.base import set_org_context
from app.db.session import get_session
from app.errors import ConflictError, NotFoundError, UnauthenticatedError
from app.models import AgentProfile
from app.models.agent import DEFAULT_SMS_HANDOFF_KEYWORDS
from app.services import agent as agent_svc
from app.services import kb as kb_svc

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


def _require_worker(request: Request) -> None:
    settings = request.app.state.settings
    if not agent_svc.verify_worker_token(request.headers, settings):
        raise UnauthenticatedError("Invalid or missing worker credentials")


# ==================================================================================
# Machine seams - the AI worker only. No OrgContext: org comes from the Call row.
# ==================================================================================
class ContextOut(BaseModel):
    org_name: str
    contact_e164: str
    direction: str
    system_prompt: str
    greeting: str
    voice_id: str
    llm_provider: str
    llm_model: str
    #: Spoken after the voicemail beep on outbound drops. The worker reads this key -
    #: dropping it from the seam silently disables voicemail drop for every org.
    voicemail_message: str
    extra_rules: list


@router.get("/context/{call_id}", response_model=ContextOut)
async def get_agent_context(
    call_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ContextOut:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    ctx = await agent_svc.resolve_context(session, call)
    return ContextOut(**ctx)


class TranscriptSegmentIn(BaseModel):
    role: str = Field(max_length=8)
    text: str = Field(max_length=8000)
    #: Postgres INTEGER ceiling - anything past it escapes the IntegrityError savepoint
    #: in upsert_transcript_segments as a DataError instead of being skipped cleanly.
    at_ms: int = Field(ge=0, le=2_147_483_647)


class TranscriptIn(BaseModel):
    call_id: uuid.UUID
    segments: list[TranscriptSegmentIn] = Field(max_length=agent_svc.MAX_TRANSCRIPT_BATCH)


@router.post("/transcript")
async def post_agent_transcript(
    payload: TranscriptIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, payload.call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    accepted = await agent_svc.upsert_transcript_segments(session, call, payload.segments)
    await session.commit()
    return {"accepted": accepted}


class ContactMessageOut(BaseModel):
    direction: str
    body: str
    at: str


class ContactOut(BaseModel):
    name: str
    tags: list[str]
    last_messages: list[ContactMessageOut]


@router.get("/contact/{e164}", response_model=ContactOut)
async def get_agent_contact(
    e164: str,
    call_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ContactOut:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    ctx = await agent_svc.get_contact_context(session, to_e164(e164))
    return ContactOut(**ctx)


class AppointmentBookIn(BaseModel):
    call_id: uuid.UUID
    contact_e164: str
    raw_when: str = Field(min_length=1, max_length=255)
    notes: str = Field(default="", max_length=2000)


class AppointmentBookOut(BaseModel):
    id: uuid.UUID
    raw_when: str
    scheduled_for: datetime | None
    status: str


@router.post("/appointments", response_model=AppointmentBookOut, status_code=201)
async def post_agent_appointment(
    payload: AppointmentBookIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AppointmentBookOut:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, payload.call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    appt = await agent_svc.book_appointment(
        session,
        call,
        contact_e164=payload.contact_e164,
        raw_when=payload.raw_when,
        notes=payload.notes,
    )
    await session.commit()

    bus = request.app.state.event_bus
    bus.publish(
        call.org_id,
        {
            "type": "appointment.booked",
            "appointment_id": str(appt.id),
            "contact_e164": appt.contact_e164,
            "raw_when": appt.raw_when,
        },
    )
    return AppointmentBookOut(
        id=appt.id, raw_when=appt.raw_when, scheduled_for=appt.scheduled_for, status=appt.status
    )


class KbSearchChunkOut(BaseModel):
    title: str
    text: str
    score: int


class KbSearchOut(BaseModel):
    chunks: list[KbSearchChunkOut]


@router.get("/kb/search", response_model=KbSearchOut)
async def get_agent_kb_search(
    call_id: uuid.UUID,
    q: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KbSearchOut:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    chunks = await kb_svc.search(session, call.org_id, q)
    return KbSearchOut(chunks=[KbSearchChunkOut(**c) for c in chunks])


class HandoffIn(BaseModel):
    call_id: uuid.UUID
    reason: str = Field(max_length=500)
    summary: str = Field(default="", max_length=2000)


class HandoffOut(BaseModel):
    published: bool


@router.post("/handoff", response_model=HandoffOut)
async def post_agent_handoff(
    payload: HandoffIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HandoffOut:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, payload.call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    bus = request.app.state.event_bus
    agent_svc.publish_handoff(bus, call, reason=payload.reason, summary=payload.summary)
    return HandoffOut(published=True)


class AmdIn(BaseModel):
    call_id: uuid.UUID
    result: str = Field(pattern="^(machine|human)$")


@router.post("/amd")
async def post_agent_amd(
    payload: AmdIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, payload.call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)
    updated = await agent_svc.set_amd_result(session, call, payload.result)
    await session.commit()
    return {"updated": updated}


# ==================================================================================
# Human-facing agent-profile CRUD (OrgContext)
# ==================================================================================
class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    system_prompt: str = ""
    greeting: str = Field(default="", max_length=500)
    voice_id: str = Field(default="", max_length=64)
    llm_provider: str = Field(default="", max_length=16)
    llm_model: str = Field(default="", max_length=64)
    #: P9: spoken after the voicemail beep on outbound drops. Empty = no drop.
    voicemail_message: str = Field(default="", max_length=500)
    extra: dict = Field(default_factory=dict)
    #: P10: the SMS surface. Off by default forever - enabling is always an explicit act.
    sms_enabled: bool = False
    sms_turn_ceiling: int = Field(default=10, ge=1)
    sms_handoff_keywords: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SMS_HANDOFF_KEYWORDS)
    )
    sms_max_reply_chars: int = Field(default=480, ge=1, le=1600)


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=127)
    system_prompt: str | None = None
    greeting: str | None = Field(default=None, max_length=500)
    voice_id: str | None = Field(default=None, max_length=64)
    llm_provider: str | None = Field(default=None, max_length=16)
    llm_model: str | None = Field(default=None, max_length=64)
    voicemail_message: str | None = Field(default=None, max_length=500)
    extra: dict | None = None
    sms_enabled: bool | None = None
    sms_turn_ceiling: int | None = Field(default=None, ge=1)
    sms_handoff_keywords: list[str] | None = None
    sms_max_reply_chars: int | None = Field(default=None, ge=1, le=1600)


class ProfileOut(BaseModel):
    id: uuid.UUID
    name: str
    system_prompt: str
    greeting: str
    voice_id: str
    llm_provider: str
    llm_model: str
    voicemail_message: str
    is_default: bool
    extra: dict
    sms_enabled: bool
    sms_turn_ceiling: int
    sms_handoff_keywords: list[str]
    sms_max_reply_chars: int


def _profile_out(p: AgentProfile) -> ProfileOut:
    return ProfileOut(
        id=p.id,
        name=p.name,
        system_prompt=p.system_prompt,
        greeting=p.greeting,
        voice_id=p.voice_id,
        llm_provider=p.llm_provider,
        llm_model=p.llm_model,
        voicemail_message=p.voicemail_message,
        is_default=p.is_default,
        extra=p.extra or {},
        sms_enabled=p.sms_enabled,
        sms_turn_ceiling=p.sms_turn_ceiling,
        sms_handoff_keywords=list(p.sms_handoff_keywords or []),
        sms_max_reply_chars=p.sms_max_reply_chars,
    )


@router.get("/profiles", response_model=list[ProfileOut])
async def list_agent_profiles(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[ProfileOut]:
    rows = await agent_svc.list_profiles(ctx.session, ctx.org.id)
    return [_profile_out(p) for p in rows]


@router.post("/profiles", response_model=ProfileOut, status_code=201)
async def create_agent_profile(
    payload: ProfileIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> ProfileOut:
    try:
        profile = await agent_svc.create_profile(ctx.session, ctx.org.id, **payload.model_dump())
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"An agent profile named {payload.name!r} already exists") from exc
    return _profile_out(profile)


@router.patch("/profiles/{profile_id}", response_model=ProfileOut)
async def update_agent_profile(
    profile_id: uuid.UUID,
    payload: ProfilePatch,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> ProfileOut:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        profile = await agent_svc.update_profile(ctx.session, profile_id, **updates)
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError("An agent profile with that name already exists") from exc
    return _profile_out(profile)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_agent_profile(
    profile_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> None:
    await agent_svc.delete_profile(ctx.session, profile_id)
    await ctx.session.commit()


@router.post("/profiles/{profile_id}/default", response_model=ProfileOut)
async def set_default_agent_profile(
    profile_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> ProfileOut:
    profile = await agent_svc.set_default_profile(ctx.session, profile_id)
    await ctx.session.commit()
    return _profile_out(profile)
