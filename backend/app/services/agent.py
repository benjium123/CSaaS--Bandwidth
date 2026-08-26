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

import jwt
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import NotFoundError
from app.models import AgentProfile, Call, CallTranscriptSegment, Org

log = structlog.get_logger("agent")

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
