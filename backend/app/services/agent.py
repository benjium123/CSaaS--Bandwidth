"""AI voice agent: worker auth, transcript ingest, context resolution, profile CRUD.

The worker (agents/ai_agent.py) is a separate process with NO DB access (agents/README
law) - it authenticates to the two machine seams (agent.py routes) with a JWT signed with
the LiveKit secret, reusing `livekit_api.mint_access_token`'s HS256 shape rather than
inventing a new auth scheme. That JWT carries no org membership of its own: the worker is
a single fixed identity ("agent-worker"), never a member of anything, so org context comes
from the CALL row it is asking about - exactly the ALLOW_UNSCOPED_KEY discipline webhooks.py
already uses to resolve org from an unauthenticated carrier callback.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime

import jwt
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ConflictError, NotFoundError
from app.models import (
    AgentProfile,
    Appointment,
    Call,
    CallTranscriptSegment,
    Contact,
    ContactTag,
    Message,
    MessageThread,
    Org,
    Tag,
)
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services import calls as calls_svc
from app.services import contacts as contacts_svc

log = structlog.get_logger("agent")

#: Maps the internal Message.direction vocabulary ("outbound"/"inbound") to the short
#: worker-facing one the contact-lookup seam's contract fixes ("out"/"in").
_DIRECTION_OUT: dict[str, str] = {"outbound": "out", "inbound": "in"}
MAX_LAST_MESSAGES = 5

#: Fixed identity the worker signs its JWT `sub` claim as. Never a real user - the seams
#: it calls take no OrgContext at all.
WORKER_IDENTITY = "agent-worker"

_VALID_ROLES = frozenset({"user", "agent"})
MAX_TRANSCRIPT_BATCH = 200

#: What resolve_context returns when an org has no usable AgentProfile - the worker still
#: needs a well-formed context object to build a (silent) pipeline from, not a 4xx.
DEFAULT_AGENT_FIELDS: dict = {
    "system_prompt": "",
    "greeting": "",
    "voice_id": "",
    "llm_provider": "",
    "llm_model": "",
    "voicemail_message": "",
    "extra_rules": [],
}


# ----------------------------------------------------------------------------------
# Worker auth
# ----------------------------------------------------------------------------------
def verify_worker_token(headers: Mapping[str, str], settings: Settings) -> bool:
    """True iff `headers` carry a valid AI-worker JWT: HS256, signed with the LiveKit
    API secret, `iss` == our LiveKit API key, `sub` == "agent-worker", `exp` present and
    unexpired. A user's own bearer token (signed with `jwt_secret`, a different secret)
    fails signature verification here and is correctly rejected - this is a machine seam,
    not a user endpoint.
    """
    auth_header = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            auth_header = value
            break
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        return False

    secret = settings.livekit_api_secret.get_secret_value()
    if not secret:
        # LiveKit is not configured on this deployment - there is no key to verify
        # against, so nothing can be a valid worker token.
        return False

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False, "require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        log.warning("agent_worker_jwt_rejected", error=str(exc))
        return False

    if claims.get("iss") != settings.livekit_api_key:
        return False
    if claims.get("sub") != WORKER_IDENTITY:
        return False
    return True


async def get_call_unscoped(session: AsyncSession, call_id: uuid.UUID) -> Call | None:
    """Look up a Call with NO tenant context bound yet - the machine seams call this
    BEFORE they know the org; the caller must `set_org_context` from the result before
    doing anything else with the session."""
    return (
        await session.execute(
            sa.select(Call)
            .where(Call.id == call_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()


# ----------------------------------------------------------------------------------
# Transcript ingest
# ----------------------------------------------------------------------------------
async def upsert_transcript_segments(
    session: AsyncSession, call: Call, segments: Iterable
) -> int:
    """Insert each valid segment, deduped on (call_id, role, at_ms) via the DB unique
    constraint - the worker posts batches AT LEAST ONCE, so exact redelivery must be a
    no-op rather than a duplicated conversation. Same nested-savepoint / IntegrityError
    pattern as every other at-least-once ingest here (voice_events, recordings).

    A segment failing basic validation (bad role, empty text, negative at_ms) is skipped
    silently, same as a deduped one - the caller only ever sees a single `accepted` count.
    Requires `set_org_context` to already be bound to `call.org_id` on this session.
    """
    accepted = 0
    for seg in segments:
        role = seg.role
        text = (seg.text or "").strip()
        at_ms = seg.at_ms
        if role not in _VALID_ROLES or not text or at_ms is None or at_ms < 0:
            continue

        row = CallTranscriptSegment(
            id=uuid.uuid4(),
            org_id=call.org_id,
            call_id=call.id,
            role=role,
            text=text,
            at_ms=at_ms,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue
        accepted += 1
    return accepted


# ----------------------------------------------------------------------------------
# Context resolution
# ----------------------------------------------------------------------------------
async def _pick_profile(session: AsyncSession, org_id: uuid.UUID) -> AgentProfile | None:
    """The default profile wins; with none marked default but exactly one profile on the
    org, that one is used (the common single-profile case should not force an operator to
    also click "make default"); two-or-more with no default resolves to None (defaults)."""
    rows = list(
        (
            await session.execute(sa.select(AgentProfile).where(AgentProfile.org_id == org_id))
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    for profile in rows:
        if profile.is_default:
            return profile
    if len(rows) == 1:
        return rows[0]
    return None


async def resolve_context(session: AsyncSession, call: Call) -> dict:
    """What the worker needs to run one call: org name, contact/direction off the Call
    row, and the org's agent config (or defaults if none is usable). Requires
    `set_org_context` to already be bound to `call.org_id` on this session."""
    org = await session.get(Org, call.org_id)
    profile = await _pick_profile(session, call.org_id)

    # dict(DEFAULT_AGENT_FIELDS) is only a SHALLOW copy: the "extra_rules" list value
    # would still be the SAME list object shared across every call that hits the
    # no-usable-profile branch below, so a caller mutating it in place would leak that
    # mutation into every other org's resolved context. deepcopy avoids that.
    fields = copy.deepcopy(DEFAULT_AGENT_FIELDS)
    if profile is not None:
        fields.update(
            system_prompt=profile.system_prompt,
            greeting=profile.greeting,
            voice_id=profile.voice_id,
            llm_provider=profile.llm_provider,
            llm_model=profile.llm_model,
            voicemail_message=profile.voicemail_message,
            extra_rules=list((profile.extra or {}).get("rules", [])),
        )

    return {
        "org_name": org.name if org is not None else "",
        "contact_e164": call.contact_e164,
        "direction": call.direction,
        **fields,
    }


# ----------------------------------------------------------------------------------
# Profile CRUD (human-facing; org-scoped by the caller's OrgContext)
# ----------------------------------------------------------------------------------
async def list_profiles(session: AsyncSession, org_id: uuid.UUID) -> list[AgentProfile]:
    rows = (
        await session.execute(
            sa.select(AgentProfile).where(AgentProfile.org_id == org_id).order_by(AgentProfile.name)
        )
    ).scalars().all()
    return list(rows)


async def create_profile(session: AsyncSession, org_id: uuid.UUID, **fields) -> AgentProfile:
    profile = AgentProfile(id=uuid.uuid4(), org_id=org_id, **fields)
    session.add(profile)
    await session.flush()
    return profile


async def update_profile(session: AsyncSession, profile_id: uuid.UUID, **fields) -> AgentProfile:
    profile = await session.get(AgentProfile, profile_id)
    if profile is None:
        raise NotFoundError("Agent profile not found")
    for key, value in fields.items():
        setattr(profile, key, value)
    await session.flush()
    return profile


async def delete_profile(session: AsyncSession, profile_id: uuid.UUID) -> None:
    profile = await session.get(AgentProfile, profile_id)
    if profile is None:
        raise NotFoundError("Agent profile not found")
    await session.delete(profile)


async def set_default_profile(session: AsyncSession, profile_id: uuid.UUID) -> AgentProfile:
    """Exactly one default per org is a service-layer invariant (the model docstring):
    a partial unique index isn't portable to SQLite. Clear every other default in the
    SAME transaction as setting this one, never as a separate commit, so a crash
    mid-way can never leave two defaults (or zero) standing."""
    profile = await session.get(AgentProfile, profile_id)
    if profile is None:
        raise NotFoundError("Agent profile not found")

    others = (
        await session.execute(
            sa.select(AgentProfile).where(
                AgentProfile.org_id == profile.org_id,
                AgentProfile.id != profile.id,
                AgentProfile.is_default.is_(True),
            )
        )
    ).scalars().all()
    for other in others:
        other.is_default = False
    profile.is_default = True
    await session.flush()
    return profile


# ----------------------------------------------------------------------------------
# P9 machine seam: contact lookup (worker-auth; org resolved from the Call row).
# ----------------------------------------------------------------------------------
async def get_contact_context(session: AsyncSession, e164: str) -> dict:
    """What `lookup_contact` needs mid-call: the contact's name/tags (empty if this
    number has no Contact yet) and up to MAX_LAST_MESSAGES most-recent messages with
    this number, across every thread. Requires `set_org_context` to already be bound."""
    name = ""
    tags: list[str] = []

    found = await contacts_svc.find_contact_by_phone(session, e164)
    if found is not None:
        contact: Contact = found[0]
        name = contact.display_name
        tags = list(
            (
                await session.execute(
                    sa.select(Tag.name)
                    .join(ContactTag, ContactTag.tag_id == Tag.id)
                    .where(ContactTag.contact_id == contact.id)
                    .order_by(Tag.name)
                )
            )
            .scalars()
            .all()
        )

    rows = (
        await session.execute(
            sa.select(Message)
            .join(MessageThread, MessageThread.id == Message.thread_id)
            .where(MessageThread.contact_e164 == e164)
            .order_by(Message.created_at.desc())
            .limit(MAX_LAST_MESSAGES)
        )
    ).scalars().all()
    last_messages = [
        {
            "direction": _DIRECTION_OUT.get(m.direction, m.direction),
            "body": m.body or "",
            "at": m.created_at.isoformat(),
        }
        for m in rows
    ]

    return {"name": name, "tags": tags, "last_messages": last_messages}


# ----------------------------------------------------------------------------------
# P9 machine seam + human-facing CRUD: appointments.
# ----------------------------------------------------------------------------------
def _parse_scheduled_for(raw_when: str) -> datetime | None:
    """ONLY datetime.fromisoformat, per contract - the LLM's own "tomorrow at 3" style
    normalization is never trusted. A trailing 'Z' is rewritten to '+00:00' first since
    fromisoformat historically rejects it; anything else that fails to parse yields None
    and the raw string is kept verbatim on the row (the model docstring's whole point).

    A parse that comes back NAIVE (no tzinfo) also yields None: an un-anchored
    wall-clock time is exactly the guess we do not trust - raw_when carries the truth,
    and an org timezone (to anchor a naive time against) is a future schema decision,
    not something to assume here.
    """
    candidate = raw_when.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


async def book_appointment(
    session: AsyncSession, call: Call, *, contact_e164: str, raw_when: str, notes: str
) -> Appointment:
    appt = Appointment(
        id=uuid.uuid4(),
        org_id=call.org_id,
        call_id=call.id,
        contact_e164=contact_e164,
        raw_when=raw_when,
        scheduled_for=_parse_scheduled_for(raw_when),
        notes=notes,
        status="booked",
        created_by="ai",
    )
    session.add(appt)
    await session.flush()
    return appt


async def list_appointments(
    session: AsyncSession, org_id: uuid.UUID, status: str | None = None
) -> list[Appointment]:
    stmt = sa.select(Appointment).order_by(Appointment.created_at.desc())
    if status:
        stmt = stmt.where(Appointment.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def get_appointment(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    appt = await session.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError("Appointment not found")
    return appt


async def update_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, **fields
) -> Appointment:
    appt = await get_appointment(session, appointment_id)
    for key, value in fields.items():
        setattr(appt, key, value)
    await session.flush()
    return appt


# ----------------------------------------------------------------------------------
# P9 machine seam: warm handoff. Publishes only - no DB write.
# ----------------------------------------------------------------------------------
def publish_handoff(bus, call: Call, *, reason: str, summary: str) -> None:
    """Raises ConflictError if this call cannot be handed off: it must be a live LiveKit
    room call (a carrier-path call has no room a human softphone could join)."""
    room = (call.extra or {}).get("room") if (call.extra or {}).get("via") == "livekit" else None
    if room is None or call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call cannot be handed off")

    bus.publish(
        call.org_id,
        {
            "type": "call.handoff",
            "call_id": str(call.id),
            "room": room,
            "reason": reason,
            "summary": summary,
            "contact": call.contact_e164,
        },
    )


# ----------------------------------------------------------------------------------
# P9 machine seam: async AMD verdict. Monotonic - first write on a leg wins.
# ----------------------------------------------------------------------------------
async def set_amd_result(session: AsyncSession, call: Call, result: str) -> bool:
    """Same monotonic rule as the webhook-driven AMD path in services/calls.py: the
    ACTIVE leg's amd_result is set only if it is currently None. Returns whether the
    write happened."""
    legs = await calls_svc.load_legs(session, call.id)
    leg = calls_svc.active_leg(legs)
    if leg is None or leg.amd_result is not None:
        return False
    leg.amd_result = result
    await session.flush()
    return True
