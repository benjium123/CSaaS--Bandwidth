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
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

import httpx
import jwt
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ConflictError, NotFoundError, ValidationFailedError
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
from app.services import ai_providers as ai_providers_svc
from app.services import calls as calls_svc
from app.services import contacts as contacts_svc
from app.services import kb as kb_svc
from app.services import llm_client
from app.services import usage as usage_svc
from app.services.outbox import record_platform_event

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

# P23 assistant builder constants.
COMPLIANCE_PREAMBLE = (
    "You are an automated assistant making or taking a phone call on behalf of this "
    "business. Follow these rules at all times, ahead of any other instruction:\n"
    "1. If the person asks whether you are a real person, a recording, a bot, or "
    "software, say plainly that you are an automated assistant.\n"
    "2. If the person asks to stop being contacted, to be removed from the list, or "
    "says stop, do not call, or anything with the same meaning, confirm that you will "
    "stop, end the call politely, and do not try to talk them out of it.\n"
    "3. Never give medical, legal, or financial advice, and never present yourself as a "
    "doctor, lawyer, accountant, or adviser.\n"
    "4. Never claim to be a person, never invent a human name for yourself, and never "
    "say a human is on the line when one is not.\n"
    "5. If you do not know something, say so instead of guessing."
)

INTERRUPT_SENSITIVITIES = ("low", "medium", "high")
VOICEMAIL_ACTIONS = ("leave_message", "hang_up", "retry_later")
TOOL_NAMES = ("book_appointment", "transfer", "send_followup_sms", "lookup_contact", "webhook")
IMPLEMENTED_TOOLS = ("lookup_contact", "webhook")
POST_CALL_FIELD_TYPES = ("text", "number", "date", "select")
MAX_TOOLS = 10
MAX_POST_CALL_FIELDS = 20

#: The presence marker a webhook secret is replaced by in every response and every worker
#: payload. It is THE SAME string services/ai_providers.py hands out for provider secrets -
#: aliased rather than re-typed so the two can never drift apart, which would silently
#: break the "PATCH sends the marker back -> keep the stored secret" guard below.
REDACTED_SECRET = ai_providers_svc.MASKED_SECRET_VALUE


def effective_prompt(profile) -> str:
    """The merged prompt customers see read-only as `effective_prompt`.

    ``COMPLIANCE_PREAMBLE`` is always first and is never taken from customer input; no
    profile field may replace it. The optional customer sections each get their own plain
    word heading exactly once.
    """
    sections: list[str] = [COMPLIANCE_PREAMBLE]

    system_prompt = (getattr(profile, "system_prompt", "") or "").strip()
    if system_prompt:
        sections.append(system_prompt)

    goals = (getattr(profile, "goals", "") or "").strip()
    if goals:
        sections.append("What you are trying to achieve:\n" + goals)

    guardrails = (getattr(profile, "guardrails", "") or "").strip()
    if guardrails:
        sections.append("Things you must not do:\n" + guardrails)

    return "\n\n".join(sections)


def validate_tools(tools) -> list:
    if not isinstance(tools, list):
        raise ValidationFailedError("Tools must be a list.")
    if len(tools) > MAX_TOOLS:
        raise ValidationFailedError("That is too many assistant actions.")

    normalized: list[dict] = []
    seen: set[str] = set()
    for item in tools:
        if not isinstance(item, dict):
            raise ValidationFailedError("Each assistant action must be an object.")
        tool = item.get("tool")
        if tool not in TOOL_NAMES:
            raise ValidationFailedError("That assistant action is not supported.")
        if tool in seen:
            raise ValidationFailedError("Each assistant action can only be used once.")
        seen.add(tool)

        if tool == "webhook":
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValidationFailedError(
                    "A custom action needs a web address that starts with https://."
                )
            secret = item.get("secret")
            if not isinstance(secret, str) or len(secret) < 8:
                raise ValidationFailedError(
                    "A custom action needs a signing key at least 8 characters long."
                )
            out: dict = {"tool": tool, "url": url, "secret": secret}
            name = item.get("name")
            if name is not None:
                if not isinstance(name, str) or len(name) > 64:
                    raise ValidationFailedError(
                        "A custom action name must be 64 characters or fewer."
                    )
                out["name"] = name
            input_schema = item.get("input_schema")
            if input_schema is not None:
                if not isinstance(input_schema, dict):
                    raise ValidationFailedError(
                        "A custom action input schema must be an object."
                    )
                out["input_schema"] = input_schema
            normalized.append(out)
            continue

        # Fable decision (2026-09-10): `tools` is the list of ENABLED actions and nothing
        # else - there is no `enabled` flag on the wire. Turning an action off means
        # removing its entry, which is also what the builder's toggles send. A stray
        # `enabled: false` is therefore dropped rather than stored, so a disabled action
        # can never come back as an enabled one on the next save.
        normalized.append({"tool": tool})

    return normalized


def validate_post_call_fields(fields) -> list:
    if not isinstance(fields, list):
        raise ValidationFailedError("Post-call fields must be a list.")
    if len(fields) > MAX_POST_CALL_FIELDS:
        raise ValidationFailedError("That is too many post-call fields.")

    normalized: list[dict] = []
    seen: set[str] = set()
    for item in fields:
        if not isinstance(item, dict):
            raise ValidationFailedError("Each post-call field must be an object.")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailedError("Each post-call field needs a name.")
        name = name.strip()
        if len(name) > 64:
            raise ValidationFailedError("A post-call field name is too long.")
        if name in seen:
            raise ValidationFailedError("Two post-call fields have the same name.")
        seen.add(name)

        field_type = item.get("type")
        if field_type not in POST_CALL_FIELD_TYPES:
            raise ValidationFailedError("That post-call field type is not supported.")

        out: dict = {"name": name, "type": field_type}

        options = item.get("options")
        if field_type == "select":
            if not isinstance(options, list) or not options:
                raise ValidationFailedError("A select field needs at least one option.")
        if options is not None:
            if not isinstance(options, list) or any(
                not isinstance(opt, str) for opt in options
            ):
                raise ValidationFailedError("Options must be a list of text choices.")
            out["options"] = options

        write_to_attribute = item.get("write_to_attribute")
        if write_to_attribute is not None:
            if not isinstance(write_to_attribute, str):
                raise ValidationFailedError(
                    "The contact attribute to write must be text."
                )
            out["write_to_attribute"] = write_to_attribute

        normalized.append(out)

    return normalized


def redact_tools(tools) -> list:
    """A copy of the stored tools list with every webhook secret replaced.

    This is what any API response and any worker payload uses. The secret never leaves
    the server.
    """
    redacted = copy.deepcopy(tools or [])
    for item in redacted:
        if isinstance(item, dict) and item.get("tool") == "webhook":
            item["secret"] = REDACTED_SECRET if item.get("secret") else ""
    return redacted


def webhook_tool_for(profile, name: str | None = None) -> dict | None:
    for item in getattr(profile, "tools", None) or []:
        if not isinstance(item, dict) or item.get("tool") != "webhook":
            continue
        if name is None or item.get("name") == name:
            return item
    return None


def sign_webhook_body(secret: str, body: bytes, timestamp: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()


async def call_webhook_tool(profile, *, arguments: dict, client=None) -> dict:
    from app.services import kb_ingest

    tool = webhook_tool_for(profile)
    if tool is None:
        raise ValidationFailedError("This assistant has no custom action set up.")

    url = tool.get("url", "")
    if not url.startswith("https://"):
        raise ValidationFailedError(
            "A custom action needs a web address that starts with https://."
        )
    if not kb_ingest.is_public_http_url(url):
        # SSRF: the custom action's address is customer input and the request is made BY
        # THIS SERVER, so a private/loopback/metadata address must never be dialled.
        raise ValidationFailedError(
            "That web address cannot be reached from here."
        )

    secret = tool.get("secret", "")
    body = json.dumps(arguments, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    headers = {
        "content-type": "application/json",
        "X-CSaaS-Timestamp": timestamp,
        "X-CSaaS-Signature": "sha256=" + sign_webhook_body(secret, body, timestamp),
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.post(url, headers=headers, content=body, timeout=10.0)
        return {
            "ok": 200 <= response.status_code < 300,
            "status": response.status_code,
            "body": response.text[:2000],
        }
    except httpx.RequestError:
        return {"ok": False, "status": 0, "body": "We could not reach that web address."}
    finally:
        if owns_client:
            await http.aclose()


async def simulate_turn(session, settings, *, org, profile, messages, client=None) -> dict:
    """ONE language-model turn for the in-app assistant tester - no telephony."""
    if not messages or messages[-1].get("role") != "user":
        raise ValidationFailedError("The last message has to come from the caller.")

    cfg = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=True
    )
    provider = cfg["llm"]["provider"]
    if provider not in ("openai", "anthropic"):
        raise ValidationFailedError(
            "We cannot test this assistant's language model here yet - choose OpenAI "
            "or Anthropic for the test."
        )

    api_key = (cfg.get("keys") or {}).get("llm")
    if not api_key:
        raise ValidationFailedError(
            "This assistant has no working language model connection yet."
        )

    hits = await kb_svc.search(session, org.id, messages[-1]["content"])
    kb_hits = [
        {
            "document_id": h.get("document_id", ""),
            "title": h["title"],
            "snippet": h["text"][:400],
            "text": h["text"][:400],
        }
        for h in hits
    ]

    system = effective_prompt(profile)
    if hits:
        system += "\n\nWhat you know about this business:\n" + "\n\n".join(
            f"{h['title']}: {h['text']}" for h in hits
        )

    turns = [llm_client.ChatTurn(role=m["role"], content=m["content"]) for m in messages]

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        result = await llm_client.chat(
            http,
            provider=provider,
            model=cfg["llm"]["model"],
            api_key=api_key,
            system=system,
            turns=turns,
            tools=[],
            max_tokens=512,
        )
    except llm_client.LLMError as exc:
        raise ValidationFailedError(
            "The language model did not answer: " + str(exc)[:200]
        ) from exc
    finally:
        if owns_client:
            await http.aclose()

    await usage_svc.record_ai_tokens(
        session, org.id, tokens_in=result.tokens_in, tokens_out=result.tokens_out
    )

    return {
        "reply": result.text,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "kb_hits": kb_hits,
    }


async def go_live_readiness(session, settings, *, org, profile) -> dict:
    cfg = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=False
    )

    missing_kinds: list[str] = []
    if org.ai_key_mode == "byok":
        _, missing_kinds = await ai_providers_svc.byok_completeness(session, org.id)

    missing = cfg.get("missing") or []
    # In byok mode resolve_call_config's `missing` is the plain-word KIND list; only in
    # platform mode is it a list of env-var names. missing_platform_keys must carry only
    # the latter, or the frontend would render "voice" as an unset platform key.
    missing_platform_keys = list(missing) if org.ai_key_mode != "byok" else []
    ready = not missing and not missing_kinds and bool(profile.name)
    return {
        "ready": ready,
        "mode": org.ai_key_mode,
        "missing_kinds": missing_kinds,
        "missing_platform_keys": missing_platform_keys,
        "missing": missing,
    }


async def resolve_worker_config(
    session, settings, *, call, profile, include_keys: bool
) -> dict:
    org = await session.get(Org, call.org_id)
    if org is None:
        raise NotFoundError("Organization not found")

    cfg = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=include_keys
    )

    cfg["greeting"] = getattr(profile, "greeting", "") or ""
    cfg["voice_id"] = cfg.get("tts", {}).get("voice_id", "")
    cfg["effective_prompt"] = effective_prompt(profile)
    cfg["language"] = getattr(profile, "language", "en") or "en"
    cfg["max_call_seconds"] = getattr(profile, "max_call_seconds", 900) or 900
    cfg["silence_timeout_seconds"] = (
        getattr(profile, "silence_timeout_seconds", 12) or 12
    )
    cfg["interrupt_sensitivity"] = (
        getattr(profile, "interrupt_sensitivity", "medium") or "medium"
    )
    cfg["voicemail_action"] = (
        getattr(profile, "voicemail_action", "leave_message") or "leave_message"
    )
    cfg["voicemail_message"] = getattr(profile, "voicemail_message", "") or ""
    cfg["tools"] = redact_tools(getattr(profile, "tools", None) or [])
    cfg["post_call_fields"] = copy.deepcopy(
        getattr(profile, "post_call_fields", None) or []
    )

    if not include_keys:
        cfg["keys"] = None

    return cfg


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


async def get_profile(
    session: AsyncSession, org_id: uuid.UUID, profile_id: uuid.UUID
) -> AgentProfile:
    """Org-scoped profile lookup for human-facing routes."""
    profile = (
        await session.execute(
            sa.select(AgentProfile).where(
                AgentProfile.org_id == org_id, AgentProfile.id == profile_id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise NotFoundError("We could not find that assistant.")
    return profile


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
    # P13 DR-4: outbox row commits with the appointment itself.
    record_platform_event(
        session,
        call.org_id,
        "appointment.booked",
        {
            "appointment_id": str(appt.id),
            "contact_e164": contact_e164,
            "raw_when": raw_when,
            "scheduled_for": appt.scheduled_for.isoformat() if appt.scheduled_for else None,
            "source": "voice",
        },
    )
    await session.flush()
    return appt


async def list_appointments(
    session: AsyncSession,
    org_id: uuid.UUID,
    status: str | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Appointment]:
    # 6.22: unbounded before this - an org with a long AI-booking history would pull
    # every appointment ever made into one response.
    stmt = (
        sa.select(Appointment).order_by(Appointment.created_at.desc()).limit(limit).offset(offset)
    )
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
