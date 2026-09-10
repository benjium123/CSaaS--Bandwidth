"""AI voice agent: two machine seams for the LiveKit worker (JWT-signed with the
LiveKit secret, no OrgContext), plus human-facing agent-profile CRUD (OrgContext,
settings:read/settings:write - there is no dedicated agent:* permission yet).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# starlette's UploadFile, NOT fastapi's: request.form() yields the starlette
# class, and fastapi.UploadFile is a SUBCLASS of it - isinstance against the
# subclass is False for a real form upload and would 422 every valid file.
from starlette.datastructures import UploadFile

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.db.base import set_org_context
from app.db.session import get_session
from app.errors import (
    ConflictError,
    CsaasError,
    NotFoundError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.models import AgentProfile, Contact, ContactPhone, KbDocument, Org
from app.models.agent import DEFAULT_SMS_HANDOFF_KEYWORDS
from app.services import agent as agent_svc
from app.services import contact_visibility, kb_ingest
from app.services import kb as kb_svc

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


def _require_worker(request: Request) -> None:
    settings = request.app.state.settings
    if not agent_svc.verify_worker_token(request.headers, settings):
        raise UnauthenticatedError("Invalid or missing worker credentials")


class ToolNotAvailableError(CsaasError):
    code = "not_available"
    http_status = 501
    message = "That action is not available yet."


def _merge_tool_secrets(existing: list, incoming: list) -> list:
    """A PATCH that sends the redacted marker - or no secret at all - keeps the stored
    webhook secret. Match by name, else by url, else the only stored webhook entry, so
    editing the address never silently drops the signing key (Opus P23a verify B2)."""
    stored = [
        old for old in (existing or []) if isinstance(old, dict) and old.get("tool") == "webhook"
    ]
    merged = []
    for item in incoming:
        if (
            isinstance(item, dict)
            and item.get("tool") == "webhook"
            and (item.get("secret") in (None, "", agent_svc.REDACTED_SECRET))
        ):
            name = item.get("name")
            url = item.get("url")
            match = None
            if name:
                match = next((o for o in stored if o.get("name") == name), None)
            if match is None and url:
                match = next((o for o in stored if o.get("url") == url), None)
            if match is None and len(stored) == 1:
                match = stored[0]
            if match is not None:
                item = {**item, "secret": match.get("secret", "")}
        merged.append(item)
    return merged


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

    org = await session.get(Org, call.org_id)
    policy = org.contact_visibility if org is not None else "everyone"
    dept_id = await contact_visibility.department_for_inbox_number(session, call.our_e164)
    scope = contact_visibility.machine_scope(policy, dept_id)
    predicate = contact_visibility.visible_contacts_filter(scope)

    contact_ctx = await agent_svc.get_contact_context(session, to_e164(e164))

    visible = True
    if predicate is not None:
        found = (
            await session.execute(
                sa.select(Contact.id)
                .join(ContactPhone, ContactPhone.contact_id == Contact.id)
                .where(ContactPhone.e164 == to_e164(e164))
                .where(predicate)
                .limit(1)
            )
        ).scalar_one_or_none()
        visible = found is not None

    if not visible:
        contact_ctx["name"] = ""
        contact_ctx["tags"] = []

    return ContactOut(**contact_ctx)


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
    #: P23a: kb.search now returns the chunk's document id, and this route splats its dicts
    #: straight in - the field has to be declared here or every /kb/search call 500s.
    document_id: str = ""
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
    # P23 assistant builder columns.
    goals: str = ""
    guardrails: str = ""
    language: str = Field(default="en", min_length=2, max_length=8)
    stt_provider: str = Field(default="", max_length=16)
    tts_provider: str = Field(default="", max_length=16)
    max_call_seconds: int = Field(default=900, ge=30, le=7200)
    silence_timeout_seconds: int = Field(default=12, ge=1, le=120)
    interrupt_sensitivity: str = "medium"
    voicemail_action: str = "leave_message"
    tools: list = Field(default_factory=list)
    post_call_fields: list = Field(default_factory=list)


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
    # P23 assistant builder columns.
    goals: str | None = None
    guardrails: str | None = None
    language: str | None = Field(default=None, min_length=2, max_length=8)
    stt_provider: str | None = Field(default=None, max_length=16)
    tts_provider: str | None = Field(default=None, max_length=16)
    max_call_seconds: int | None = Field(default=None, ge=30, le=7200)
    silence_timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    interrupt_sensitivity: str | None = None
    voicemail_action: str | None = None
    tools: list | None = None
    post_call_fields: list | None = None


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
    goals: str
    guardrails: str
    language: str
    stt_provider: str
    tts_provider: str
    max_call_seconds: int
    silence_timeout_seconds: int
    interrupt_sensitivity: str
    voicemail_action: str
    tools: list
    post_call_fields: list
    effective_prompt: str


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
        goals=p.goals,
        guardrails=p.guardrails,
        language=p.language,
        stt_provider=p.stt_provider,
        tts_provider=p.tts_provider,
        max_call_seconds=p.max_call_seconds,
        silence_timeout_seconds=p.silence_timeout_seconds,
        interrupt_sensitivity=p.interrupt_sensitivity,
        voicemail_action=p.voicemail_action,
        tools=agent_svc.redact_tools(p.tools or []),
        post_call_fields=list(p.post_call_fields or []),
        effective_prompt=agent_svc.effective_prompt(p),
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
    if payload.interrupt_sensitivity not in agent_svc.INTERRUPT_SENSITIVITIES:
        raise ValidationFailedError(
            "Interruption sensitivity must be low, medium, or high."
        )
    if payload.voicemail_action not in agent_svc.VOICEMAIL_ACTIONS:
        raise ValidationFailedError(
            "Voicemail action must be leave_message, hang_up, or retry_later."
        )

    data = payload.model_dump()
    data["tools"] = agent_svc.validate_tools(data["tools"])
    data["post_call_fields"] = agent_svc.validate_post_call_fields(
        data["post_call_fields"]
    )

    try:
        profile = await agent_svc.create_profile(ctx.session, ctx.org.id, **data)
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

    if "interrupt_sensitivity" in updates:
        if updates["interrupt_sensitivity"] not in agent_svc.INTERRUPT_SENSITIVITIES:
            raise ValidationFailedError(
                "Interruption sensitivity must be low, medium, or high."
            )
    if "voicemail_action" in updates:
        if updates["voicemail_action"] not in agent_svc.VOICEMAIL_ACTIONS:
            raise ValidationFailedError(
                "Voicemail action must be leave_message, hang_up, or retry_later."
            )
    if "tools" in updates:
        existing = await agent_svc.get_profile(ctx.session, ctx.org.id, profile_id)
        updates["tools"] = agent_svc.validate_tools(
            _merge_tool_secrets(existing.tools or [], updates["tools"])
        )
    if "post_call_fields" in updates:
        updates["post_call_fields"] = agent_svc.validate_post_call_fields(
            updates["post_call_fields"]
        )

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


# ==================================================================================
# P23a: go-live readiness, in-app simulator, worker config and server-side tools.
# ==================================================================================
@router.post("/profiles/{profile_id}/go-live", response_model=None)
async def go_live_agent_profile(
    profile_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> dict | JSONResponse:
    profile = await agent_svc.get_profile(ctx.session, ctx.org.id, profile_id)
    readiness = await agent_svc.go_live_readiness(
        ctx.session, request.app.state.settings, org=ctx.org, profile=profile
    )

    if readiness["ready"]:
        return {
            "ok": True,
            "ready": True,
            "mode": readiness["mode"],
            "missing_kinds": [],
            "missing_platform_keys": [],
        }

    missing_kinds = readiness["missing_kinds"]
    missing_envs = readiness["missing_platform_keys"]
    kind_words = {
        "llm": "language model",
        "stt": "speech recognition",
        "tts": "voice",
    }
    parts: list[str] = []
    for kind in missing_kinds:
        parts.append(f"add a {kind_words.get(kind, kind)} connection")
    if missing_envs:  # env var names stay in details for operators, never in the sentence
        parts.append("we are still setting up this service on our side")
    for word in readiness["missing"]:
        if not missing_kinds and word not in missing_envs:
            parts.append(f"add a {word} connection")
    if not parts:
        parts.append("give it a name")
    message = "This assistant cannot go live yet: " + ", ".join(parts) + "."

    # The STANDARD envelope main.py's CsaasError handler produces, with the machine lists
    # under `details` - a bespoke top-level shape here would be the one 422 in the API the
    # generic client could not read.
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_failed",
                "message": message,
                "request_id": request.headers.get("X-Request-Id", ""),
                "details": {
                    "ready": False,
                    "mode": readiness["mode"],
                    "missing_kinds": missing_kinds,
                    "missing_platform_keys": missing_envs,
                },
            }
        },
    )


class SimulateMessageIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class SimulateIn(BaseModel):
    messages: list[SimulateMessageIn] = Field(min_length=1, max_length=20)


class SimulateOut(BaseModel):
    reply: str
    tokens_in: int
    tokens_out: int
    kb_hits: list[dict]


@router.post("/profiles/{profile_id}/simulate", response_model=SimulateOut)
async def simulate_agent_profile(
    profile_id: uuid.UUID,
    payload: SimulateIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> SimulateOut:
    profile = await agent_svc.get_profile(ctx.session, ctx.org.id, profile_id)
    result = await agent_svc.simulate_turn(
        ctx.session,
        request.app.state.settings,
        org=ctx.org,
        profile=profile,
        messages=[m.model_dump() for m in payload.messages],
    )
    await ctx.session.commit()
    return SimulateOut(**result)


@router.get("/config/{call_id}", response_model=None)
async def get_agent_config(
    call_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)

    org = await session.get(Org, call.org_id)
    if org is None:
        raise NotFoundError("Organization not found")

    profile = await agent_svc._pick_profile(session, call.org_id)
    if profile is None:
        profile = AgentProfile(id=uuid.uuid4(), org_id=call.org_id, name="")

    include_keys = getattr(request.app.state.settings, "ai_per_org_keys", False)
    return await agent_svc.resolve_worker_config(
        session,
        request.app.state.settings,
        call=call,
        profile=profile,
        include_keys=include_keys,
    )


class ToolCallIn(BaseModel):
    call_id: uuid.UUID
    arguments: dict = Field(default_factory=dict)


@router.post("/tools/{tool}", response_model=None)
async def post_agent_tool(
    tool: str,
    payload: ToolCallIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _require_worker(request)
    call = await agent_svc.get_call_unscoped(session, payload.call_id)
    if call is None:
        raise NotFoundError("Call not found")
    set_org_context(session, call.org_id)

    profile = await agent_svc._pick_profile(session, call.org_id)
    if profile is None:
        profile = AgentProfile(id=uuid.uuid4(), org_id=call.org_id, name="")

    if tool == "lookup_contact":
        e164 = payload.arguments.get("e164")
        if not e164 or not isinstance(e164, str):
            raise ValidationFailedError(
                "We need a phone number to look up that contact."
            )
        return {
            "ok": True,
            "result": await agent_svc.get_contact_context(session, to_e164(e164)),
        }

    if tool == "webhook":
        # The outer "ok" is the CALL's verdict, not "we tried" - a hardcoded True here
        # would tell the worker a failed custom action succeeded.
        result = await agent_svc.call_webhook_tool(profile, arguments=payload.arguments)
        return {"ok": bool(result.get("ok")), "result": result}

    if tool in ("book_appointment", "transfer", "send_followup_sms"):
        raise ToolNotAvailableError()

    raise NotFoundError("There is no action by that name.")


# ==================================================================================
# P23a knowledge documents - human-facing, org-scoped (NOT the worker).
# ==================================================================================
class KbDocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source: str
    status: str
    chunk_count: int
    detail: str = ""


def _kb_document_out(doc: KbDocument) -> KbDocumentOut:
    return KbDocumentOut(
        id=doc.id,
        title=doc.title,
        source=doc.source,
        status=doc.status,
        chunk_count=doc.chunk_count,
        detail=getattr(doc, "_ingest_detail", "") or "",
    )


@router.post("/kb/documents", response_model=KbDocumentOut, status_code=201)
async def create_kb_document(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> KbDocumentOut:
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            # form.get returns a plain str for a non-file part; calling .read() on that
            # would be an AttributeError 500 instead of an honest 422.
            raise ValidationFailedError("Send a file, a web address, or some text.")
        declared = getattr(upload, "size", None)
        if declared is not None and declared > kb_ingest.MAX_UPLOAD_BYTES:
            raise ValidationFailedError("That file is too big.")
        data = await upload.read()
        if len(data) > kb_ingest.MAX_UPLOAD_BYTES:
            raise ValidationFailedError("That file is too big.")
        title = str(form.get("title") or upload.filename or "Uploaded note")
        doc = await kb_ingest.ingest_upload(
            ctx.session,
            ctx.org.id,
            title=title,
            filename=upload.filename or "",
            data=data,
        )
    else:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise ValidationFailedError("We could not read that request.") from exc
        if not isinstance(body, dict):
            raise ValidationFailedError("We could not read that request.")
        if body.get("url"):
            doc = await kb_ingest.ingest_url(
                ctx.session,
                ctx.org.id,
                title=body.get("title") or body["url"],
                url=body["url"],
            )
        elif body.get("text"):
            doc = await kb_ingest.ingest_text(
                ctx.session,
                ctx.org.id,
                title=body.get("title") or "Pasted note",
                text=body["text"],
            )
        else:
            raise ValidationFailedError("Send a file, a web address, or some text.")

    await ctx.session.commit()
    return _kb_document_out(doc)


@router.get("/kb/documents", response_model=list[KbDocumentOut])
async def list_kb_documents(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[KbDocumentOut]:
    docs = await kb_svc.list_documents(ctx.session, ctx.org.id)
    return [_kb_document_out(doc) for doc in docs]


@router.delete("/kb/documents/{document_id}", status_code=204)
async def delete_kb_document(
    document_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> None:
    doc = (
        await ctx.session.execute(
            sa.select(KbDocument).where(
                KbDocument.org_id == ctx.org.id, KbDocument.id == document_id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise NotFoundError("We could not find that knowledge document.")
    await kb_svc.delete_document(ctx.session, doc.id)
    await ctx.session.commit()
