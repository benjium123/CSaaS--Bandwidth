from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.routes.calls import _livekit_route_reason
from app.api.routes.inbox import _get_thread
from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, ValidationFailedError
from app.models import (
    AgentProfile,
    Call,
    CallLeg,
    CallRecording,
    CallScore,
    CallTranscriptSegment,
    Contact,
    ContactPhone,
    Inbox,
    Message,
    MessageThread,
    OrgMembership,
    OrgNumber,
    Role,
    ThreadNote,
    User,
    VoiceEvent,
    Voicemail,
)
from app.services import inbox_access as inbox_access_svc
from app.services import inbox_sla as inbox_sla_svc
from app.services import notifications as notifications_svc

router = APIRouter(prefix="/api/v1", tags=["conversations"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Terminal inbound statuses that read as "missed" everywhere in this module: the
#: middle-pane snippet ("Missed call"), the unresponded filter, and the unread flag.
MISSED_CALL_STATUSES = frozenset({"no_answer", "busy", "canceled", "failed"})


# ----------------------------------------------------------------------------------
# Cursor helpers
# ----------------------------------------------------------------------------------
def _urlsafe_b64encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _urlsafe_b64decode(token: str) -> str:
    try:
        padding = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValidationFailedError("Invalid cursor") from exc


def _encode_pair_cursor(last_event_at: datetime, our_e164: str, contact_e164: str) -> str:
    # P16 Opus review point 5: our_e164 joins the key so two pairs sharing a contact
    # across two of our numbers with an identical last_event_at never collide.
    return _urlsafe_b64encode(f"{last_event_at.isoformat()}|{our_e164}|{contact_e164}")


def _decode_pair_cursor(token: str) -> tuple[datetime, str, str]:
    try:
        dt_s, our_e164, contact_e164 = _urlsafe_b64decode(token).split("|", 2)
        return datetime.fromisoformat(dt_s), our_e164, contact_e164
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("Invalid cursor") from exc


def _encode_item_cursor(occurred_at: datetime, item_id: uuid.UUID) -> str:
    return _urlsafe_b64encode(f"{occurred_at.isoformat()}|{item_id}")


def _decode_item_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    try:
        dt_s, id_s = _urlsafe_b64decode(token).split("|", 1)
        return datetime.fromisoformat(dt_s), uuid.UUID(id_s)
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("Invalid cursor") from exc


def _aware(dt: datetime | None) -> datetime | None:
    """Attach UTC to a naive SQLite timestamp for comparisons against aware now."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ----------------------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------------------
class ConversationContact(BaseModel):
    id: uuid.UUID
    display_name: str


class SlaOut(BaseModel):
    due_at: datetime | None
    breached: bool


class ConversationItem(BaseModel):
    contact_e164: str
    our_e164: str
    inbox_id: uuid.UUID | None
    thread_id: uuid.UUID | None
    last_event_at: datetime
    last_event_type: Literal["message", "call", "voicemail"]
    direction: str | None
    snippet: str | None
    unread: bool
    contact: ConversationContact | None
    status: str
    important: bool
    snoozed_until: datetime | None = None
    sla: SlaOut | None = None


class ConversationListResponse(BaseModel):
    items: list[ConversationItem]
    next_cursor: str | None


class MessageTimelineEvent(BaseModel):
    kind: Literal["message"] = "message"
    id: uuid.UUID
    direction: str
    body: str | None
    media: list
    status: str
    occurred_at: datetime
    error_code: str | None
    # P21: why this route was chosen (plain sentence) - tooltip in the unified timeline.
    route_reason: str | None = None


class CallRecordingOut(BaseModel):
    id: uuid.UUID
    status: str
    duration_seconds: int | None


class AssistantCallOut(BaseModel):
    #: The assistant that handled the call, when the outcome row named one.
    name: str | None = None
    summary: str | None
    disposition: str | None
    sentiment: str | None
    has_transcript: bool


class CallTimelineEvent(BaseModel):
    kind: Literal["call"] = "call"
    id: uuid.UUID
    direction: str
    status: str
    duration_seconds: int | None
    occurred_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None
    failure_detail: str | None
    recording: CallRecordingOut | None
    has_voicemail: bool
    route_reason: str | None = None
    assistant: AssistantCallOut | None = None


class VoicemailTimelineEvent(BaseModel):
    kind: Literal["voicemail"] = "voicemail"
    id: uuid.UUID
    call_id: uuid.UUID
    occurred_at: datetime
    transcript: str | None
    duration_seconds: int | None
    transcript_status: str
    #: Same {id, status, duration_seconds} shape CallTimelineEvent.recording exposes -
    #: resolved from the same recordings_by_id lookup that already supplies
    #: duration_seconds above (P16 Opus review follow-up).
    recording: CallRecordingOut | None


class NoteMention(BaseModel):
    user_id: uuid.UUID
    name: str


class NoteOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    author_user_id: uuid.UUID | None
    author_name: str
    body: str
    mentions: list[NoteMention]
    created_at: datetime


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    mention_user_ids: list[uuid.UUID] = []


class SnoozeIn(BaseModel):
    until: datetime


class NoteTimelineEvent(BaseModel):
    kind: Literal["note"] = "note"
    id: uuid.UUID
    author_name: str
    body: str
    mentions: list[NoteMention]
    occurred_at: datetime


TimelineEvent = Annotated[
    MessageTimelineEvent | CallTimelineEvent | VoicemailTimelineEvent | NoteTimelineEvent,
    Field(discriminator="kind"),
]


class TimelineResponse(BaseModel):
    items: list[TimelineEvent]
    next_cursor: str | None
    snoozed_until: datetime | None = None
    sla: SlaOut | None = None


# ----------------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------------
def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters so a caller-supplied `q` cannot smuggle its own
    wildcards into the pattern (5.9). Backslash first - escaping % and _ before it would
    double-escape a literal backslash already present in the input."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _latest_message_by_thread(messages: list[Message]) -> dict[uuid.UUID, Message]:
    result: dict[uuid.UUID, Message] = {}
    for msg in messages:
        cur = result.get(msg.thread_id)
        if cur is None or (msg.created_at, msg.id) > (cur.created_at, cur.id):
            result[msg.thread_id] = msg
    return result


def _message_snippet(msg: Message | None) -> str | None:
    if msg is None:
        return None
    body = msg.body or ""
    if msg.direction == "outbound":
        return f"You: {body[:80]}"
    return body[:80]


def _call_snippet(call: Call) -> str:
    if call.direction == "inbound":
        if call.status in MISSED_CALL_STATUSES:
            return "Missed call"
        return "Called you"
    if call.status == "failed":
        return "Call failed"
    return "You called"


def _voicemail_snippet(vm: Voicemail) -> str | None:
    if vm.transcript:
        return f"Voicemail: {vm.transcript[:80]}"
    return None


def _is_unresponded(pair: dict[str, Any]) -> bool:
    if pair["last_event_type"] == "message":
        return pair["direction"] == "inbound"
    if pair["last_event_type"] == "call":
        call: Call | None = pair.get("latest_call")
        return (
            call is not None
            and call.direction == "inbound"
            and call.status in MISSED_CALL_STATUSES
        )
    return False


def _call_unread(call: Call, call_dt: datetime, thread: MessageThread | None) -> bool:
    """A missed inbound call is unread exactly like an inbound message: unread until the
    thread's read cursor passes it. A call-only pair (no thread => no read cursor to
    consult) is always unread while it stands as the pair's missed-call event. Scope:
    only last_event_type == "call" uses this - a voicemail's own unread state is
    untouched (P16 Opus review point 9)."""
    if call.direction != "inbound" or call.status not in MISSED_CALL_STATUSES:
        return False
    if thread is None:
        return True
    return thread.last_read_at is None or thread.last_read_at < call_dt


def _parse_failure_text(raw: str) -> str | None:
    """A rejection body may be plain text or a JSON object (e.g. Bandwidth's 402 body
    ``{"type": "...", "description": "..."}``). Prefer the human-readable description/
    detail/error field when present; otherwise fall back to the raw text itself."""
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            nested = json.loads(stripped)
        except (ValueError, TypeError):
            return stripped
        if isinstance(nested, dict):
            for key in ("description", "detail", "error"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return stripped
    return stripped


def _extract_failure_detail(
    call: Call,
    legs: list[CallLeg],
    events: list[VoiceEvent],
) -> str | None:
    """For failed calls, surface the most useful failure text we stored, in priority
    order (P16 Opus review point 3):

    1. ``CallLeg.extra["error_detail"]`` - the raw rejection body
       ``services/calls.py::create_outbound_call`` writes at dispatch time when the
       carrier rejects synchronously; a Bandwidth 402 body lands here verbatim.
    2. The latest ``VoiceEvent`` payload - a webhook-delivered failure reason.
    3. ``CallLeg.hangup_cause`` - a generic carrier code, used only when nothing more
       specific was captured.
    """
    if call.status != "failed":
        return None

    for leg in sorted(legs, key=lambda l: (l.created_at, l.id), reverse=True):
        raw = (leg.extra or {}).get("error_detail")
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_failure_text(raw)
            if parsed:
                return parsed

    for event in sorted(events, key=lambda e: (e.created_at, e.id), reverse=True):
        payload = event.payload or {}
        for key in ("detail", "description", "error"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            parsed = _parse_failure_text(value)
            if parsed:
                return parsed

        # Some adapters nest a JSON string under a non-standard key.
        for value in payload.values():
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if not stripped.startswith("{"):
                continue
            try:
                nested = json.loads(stripped)
            except (ValueError, TypeError):
                continue
            if isinstance(nested, dict):
                for key in ("description", "detail", "error"):
                    nested_value = nested.get(key)
                    if isinstance(nested_value, str) and nested_value.strip():
                        return nested_value.strip()

    for leg in sorted(legs, key=lambda l: (l.created_at, l.id), reverse=True):
        if leg.hangup_cause:
            return leg.hangup_cause

    return None


def _latest_recording(
    recordings: list[CallRecording],
    call_id: uuid.UUID,
) -> CallRecording | None:
    matching = [r for r in recordings if r.call_id == call_id]
    if not matching:
        return None
    return max(matching, key=lambda r: (r.created_at, r.id))


# ----------------------------------------------------------------------------------
# Note helpers
# ----------------------------------------------------------------------------------
def _user_display_name(user: User | None) -> str:
    if user is None:
        return "Automation"
    return user.full_name or user.email


def _first_word(display_name: str) -> str:
    stripped = display_name.strip()
    if not stripped:
        return ""
    return stripped.split()[0].lower()


async def _mentionable_members(session) -> dict[uuid.UUID, str]:
    """Who may be @-mentioned: every member of THIS org whose role can work the inbox.

    OrgMembership is selected as an entity (not merely joined) so the session-level
    tenant guard attaches its org filter to it too - a join-only mapper is not
    guaranteed to be seen by the guard.
    """
    rows = (
        await session.execute(
            sa.select(User, Role, OrgMembership)
            .join(OrgMembership, OrgMembership.user_id == User.id)
            .join(Role, Role.id == OrgMembership.role_id)
        )
    ).all()

    mentionable: dict[uuid.UUID, str] = {}
    for user, role, _membership in rows:
        permissions = role.permissions or []
        if "*" not in permissions and "inbox:read" not in permissions:
            continue
        display = user.full_name or user.email
        if display:
            mentionable[user.id] = display
    return mentionable


def _mentions_name(body: str, name: str) -> bool:
    """True when `body` contains "@name" as a whole token.

    The trailing guard matters: without it "@Sam" would also fire on "@Sammy Jones",
    quietly notifying the wrong teammate. A name is matched case-insensitively and only
    when the character right after it is not another word character.
    """
    if not name:
        return False
    pattern = re.compile("@" + re.escape(name) + r"(?![\w'\-])", re.IGNORECASE)
    return pattern.search(body) is not None


def _extract_parsed_mentions(body: str, mentionable: dict[uuid.UUID, str]) -> set[uuid.UUID]:
    first_word_counts: dict[str, int] = {}
    for display_name in mentionable.values():
        first = _first_word(display_name)
        if first:
            first_word_counts[first] = first_word_counts.get(first, 0) + 1

    matched: set[uuid.UUID] = set()
    for user_id, display_name in mentionable.items():
        if _mentions_name(body, display_name.strip()):
            matched.add(user_id)
            continue
        # A first name only counts when it belongs to exactly one teammate - an
        # ambiguous "@Sam" with two Sams in the org deliberately matches nobody.
        first = _first_word(display_name)
        if first and first_word_counts.get(first) == 1 and _mentions_name(body, first):
            matched.add(user_id)
    return matched


def _note_mention_uuids(mentions: list | None) -> list[uuid.UUID]:
    result: list[uuid.UUID] = []
    for raw in mentions or []:
        try:
            result.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    return result


async def _resolve_note_names(session, notes: list[ThreadNote]) -> dict[uuid.UUID, str]:
    user_ids: set[uuid.UUID] = set()
    for note in notes:
        if note.author_user_id is not None:
            user_ids.add(note.author_user_id)
        user_ids.update(_note_mention_uuids(note.mentions))
    if not user_ids:
        return {}

    users = list(
        (await session.execute(sa.select(User).where(User.id.in_(user_ids))))
        .scalars()
        .all()
    )
    return {user.id: (user.full_name or user.email) for user in users}


def _note_author_name(name_by_id: dict[uuid.UUID, str], author_user_id: uuid.UUID | None) -> str:
    if author_user_id is None:
        return "Automation"
    return name_by_id.get(author_user_id, "Unknown user")


def _note_out_from(note: ThreadNote, name_by_id: dict[uuid.UUID, str]) -> NoteOut:
    mentions = [
        NoteMention(user_id=user_id, name=name_by_id.get(user_id, "Unknown user"))
        for user_id in _note_mention_uuids(note.mentions)
    ]
    return NoteOut(
        id=note.id,
        thread_id=note.thread_id,
        author_user_id=note.author_user_id,
        author_name=_note_author_name(name_by_id, note.author_user_id),
        body=note.body,
        mentions=mentions,
        created_at=note.created_at,
    )


def _note_timeline_item(note: ThreadNote, name_by_id: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "kind": "note",
        "id": note.id,
        "author_name": _note_author_name(name_by_id, note.author_user_id),
        "body": note.body,
        "mentions": [
            NoteMention(user_id=user_id, name=name_by_id.get(user_id, "Unknown user"))
            for user_id in _note_mention_uuids(note.mentions)
        ],
        "occurred_at": note.created_at,
    }


# ----------------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------------
@router.get("/conversations/{thread_id}/notes", response_model=list[NoteOut])
async def list_notes(
    thread_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[NoteOut]:
    thread = await _get_thread(ctx, thread_id, require_use=True)

    # Newest-first in SQL so a long-running conversation returns its RECENT notes, then
    # reversed so the response still reads oldest -> newest like the timeline does.
    notes = list(
        (
            await ctx.session.execute(
                sa.select(ThreadNote)
                .where(ThreadNote.thread_id == thread.id)
                .order_by(ThreadNote.created_at.desc(), ThreadNote.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    notes.reverse()
    name_by_id = await _resolve_note_names(ctx.session, notes)
    return [_note_out_from(note, name_by_id) for note in notes]


@router.post("/conversations/{thread_id}/notes", response_model=NoteOut, status_code=201)
async def create_note(
    thread_id: uuid.UUID,
    payload: NoteIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
) -> NoteOut:
    thread = await _get_thread(ctx, thread_id, require_use=True)

    mentionable = await _mentionable_members(ctx.session)
    parsed_ids = _extract_parsed_mentions(payload.body, mentionable)
    valid_ids = set(mentionable.keys())
    requested_ids = set(payload.mention_user_ids)
    if not requested_ids.issubset(valid_ids):
        raise ValidationFailedError("You can only mention teammates who can use the inbox.")

    final_ids = parsed_ids | requested_ids
    if ctx.actor_user_id in final_ids:
        final_ids.remove(ctx.actor_user_id)

    if ctx.actor_user_id is not None:
        author_user = await ctx.session.get(User, ctx.actor_user_id)
        author_name = _user_display_name(author_user)
    else:
        author_name = "Automation"

    note = ThreadNote(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        thread_id=thread.id,
        author_user_id=ctx.actor_user_id,
        body=payload.body,
        mentions=[str(user_id) for user_id in sorted(final_ids)],
    )
    ctx.session.add(note)
    await ctx.session.flush()

    note_id = note.id
    if final_ids:
        await notifications_svc.notify_mention(
            ctx.session,
            ctx.org.id,
            thread_id=thread.id,
            note_id=note_id,
            author_name=author_name,
            user_ids=final_ids,
            bus=notifications_svc.bus_from_session(ctx.session),
        )

    await ctx.session.commit()
    await ctx.session.refresh(note)

    response_names = dict(mentionable)
    if ctx.actor_user_id is not None:
        response_names[ctx.actor_user_id] = author_name
    return _note_out_from(note, response_names)


@router.post("/conversations/{thread_id}/snooze")
async def snooze_thread(
    thread_id: uuid.UUID,
    payload: SnoozeIn,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
) -> dict:
    thread = await _get_thread(ctx, thread_id, require_use=True)

    until = payload.until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    if until <= now:
        raise ValidationFailedError("Choose a time in the future to bring this conversation back.")
    if until > now + timedelta(days=365):
        raise ValidationFailedError("Snooze for up to a year at a time.")

    thread.snoozed_until = until
    await ctx.session.commit()
    return {"id": thread.id, "snoozed_until": until}


@router.delete("/conversations/{thread_id}/snooze")
async def unsnooze_thread(
    thread_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:send"))],
) -> dict:
    thread = await _get_thread(ctx, thread_id, require_use=True)
    thread.snoozed_until = None
    await ctx.session.commit()
    return {"id": thread.id, "snoozed_until": None}


@router.get("/conversations")
async def list_conversations(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    inbox_id: uuid.UUID | None = None,
    tab: str = Query("chats", pattern="^(chats|calls)$"),
    # Named `filter_` internally so it never shadows the `filter` builtin; the wire
    # param name (`?filter=`) is unchanged via `alias` (P16 Opus review point 12).
    filter_: str = Query(
        "open",
        alias="filter",
        pattern="^(open|unread|unresponded|all|important|snoozed|overdue)$",
    ),
    q: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> ConversationListResponse:
    # P16 Opus review point 1: call/voicemail-derived data additionally requires
    # calls:read (inbox:read alone only ever grants message visibility here).
    has_calls_read = ctx.role.grants("calls:read")
    now = datetime.now(timezone.utc)

    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )

    if inbox_id is not None:
        inbox = await ctx.session.get(Inbox, inbox_id)
        if inbox is None:
            raise NotFoundError("Inbox not found")
        number = await ctx.session.get(OrgNumber, inbox.number_id)
        if number is None:
            raise NotFoundError("Inbox not found")
        if not access.can_view(number.e164):
            raise NotFoundError("Inbox not found")
        visible_e164s = {number.e164}
    else:
        if access.is_admin:
            visible_e164s = set(
                (await ctx.session.execute(sa.select(OrgNumber.e164))).scalars().all()
            )
        else:
            visible_e164s = set(access.member_e164s) | set(access.viewer_e164s)

    if not visible_e164s:
        return ConversationListResponse(items=[], next_cursor=None)

    cursor_dt: datetime | None = None
    cursor_our: str | None = None
    cursor_contact: str | None = None
    if cursor:
        cursor_dt, cursor_our, cursor_contact = _decode_pair_cursor(cursor)

    inbox_rows = (
        await ctx.session.execute(
            sa.select(Inbox.id, OrgNumber.e164)
            .join(OrgNumber, Inbox.number_id == OrgNumber.id)
            .where(OrgNumber.e164.in_(visible_e164s))
        )
    ).all()
    inbox_id_by_e164 = {e164: inbox_id_value for inbox_id_value, e164 in inbox_rows}

    # ---- Threads (message source) -------------------------------------------------
    # P16 Opus review point 4: never load every thread/message for the org. Candidates
    # are bounded to `limit * 3` per source, ordered desc on the source's own
    # last-event timestamp, with the cursor pushed down as a loose (<=) SQL pre-filter.
    # This is a heuristic over-fetch, not exact pagination - the precise `<` comparison
    # against the full (last_event_at, our_e164, contact_e164) cursor tuple still runs
    # in Python after the merge below. A page could in principle come back short of
    # `limit` if far more than `limit * 3` events from ONE source land at/after the
    # cursor while the other source contributes nothing in that window - an accepted
    # tradeoff for bounding the query instead of scanning the whole table.
    thread_order_expr = sa.func.coalesce(MessageThread.last_message_at, MessageThread.created_at)
    thread_stmt = sa.select(MessageThread).where(MessageThread.our_e164.in_(visible_e164s))
    if cursor_dt is not None:
        thread_stmt = thread_stmt.where(thread_order_expr <= cursor_dt)
    # P16 Opus re-review: push what's expressible on MessageThread alone into SQL so the
    # window is drawn from rows that actually matter for this request, instead of a
    # generic recency window that a heavy read/closed/non-matching tail can exhaust with
    # zero real matches. P26 adds snooze and overdue filters to the same push-down
    # strategy; the Python re-check below keeps call-derived pairs honest.
    if filter_ == "open":
        thread_stmt = thread_stmt.where(
            MessageThread.status != "closed",
            sa.or_(
                MessageThread.snoozed_until.is_(None),
                MessageThread.snoozed_until <= now,
            ),
        )
    elif filter_ == "unread":
        # Approximate: a thread whose own last message was never read - or was read
        # before that message arrived - is a CANDIDATE unread thread. This may still
        # admit a thread whose last message was outbound (not actually unread); the
        # exact check below (using the fetched Message row) still gates the real
        # result. It must never EXCLUDE a genuinely-unread thread.
        thread_stmt = thread_stmt.where(
            MessageThread.last_message_at.is_not(None),
            sa.or_(
                MessageThread.last_read_at.is_(None),
                MessageThread.last_read_at < MessageThread.last_message_at,
            ),
        )
    elif filter_ == "important":
        thread_stmt = thread_stmt.where(MessageThread.is_important.is_(True))
    elif filter_ == "snoozed":
        thread_stmt = thread_stmt.where(
            MessageThread.snoozed_until.is_not(None),
            MessageThread.snoozed_until > now,
        )
    elif filter_ == "overdue":
        thread_stmt = thread_stmt.where(
            MessageThread.sla_breached_at.is_not(None),
            MessageThread.status != "closed",
        )

    if q:
        needle = f"%{_escape_like(q.strip().lower())}%"
        matching_contact_e164s = (
            sa.select(ContactPhone.e164)
            .join(Contact, Contact.id == ContactPhone.contact_id)
            .where(sa.func.lower(Contact.display_name).like(needle, escape="\\"))
        )
        thread_stmt = thread_stmt.where(
            sa.or_(
                sa.func.lower(MessageThread.contact_e164).like(needle, escape="\\"),
                MessageThread.contact_e164.in_(matching_contact_e164s),
            )
        )
    thread_stmt = thread_stmt.order_by(thread_order_expr.desc(), MessageThread.id.desc()).limit(
        limit * 3
    )
    threads = list((await ctx.session.execute(thread_stmt)).scalars().all())
    thread_window_full = len(threads) == limit * 3

    messages: list[Message] = []
    thread_ids = [t.id for t in threads]
    if thread_ids:
        latest_msg_sub = (
            sa.select(Message.thread_id, sa.func.max(Message.created_at).label("max_created"))
            .where(Message.thread_id.in_(thread_ids))
            .group_by(Message.thread_id)
        ).subquery()
        messages = list(
            (
                await ctx.session.execute(
                    sa.select(Message).join(
                        latest_msg_sub,
                        sa.and_(
                            Message.thread_id == latest_msg_sub.c.thread_id,
                            Message.created_at == latest_msg_sub.c.max_created,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    latest_msg_by_thread = _latest_message_by_thread(messages)

    # ---- Calls + voicemails (call source) - gated on calls:read --------------------
    latest_call_by_pair: dict[tuple[str, str], Call] = {}
    voicemail_by_call: dict[uuid.UUID, Voicemail] = {}
    calls: list[Call] = []
    calls_window_full = False

    if has_calls_read:
        call_order_expr = sa.func.coalesce(Call.ended_at, Call.created_at)
        pair_latest_stmt = (
            sa.select(
                Call.our_e164.label("our_e164"),
                Call.contact_e164.label("contact_e164"),
                sa.func.max(call_order_expr).label("max_dt"),
            )
            .where(Call.our_e164.in_(visible_e164s))
            .group_by(Call.our_e164, Call.contact_e164)
        )
        if cursor_dt is not None:
            pair_latest_stmt = pair_latest_stmt.having(sa.func.max(call_order_expr) <= cursor_dt)
        pair_latest = pair_latest_stmt.order_by(sa.func.max(call_order_expr).desc()).limit(
            limit * 3
        ).subquery()

        calls = list(
            (
                await ctx.session.execute(
                    sa.select(Call).join(
                        pair_latest,
                        sa.and_(
                            Call.our_e164 == pair_latest.c.our_e164,
                            Call.contact_e164 == pair_latest.c.contact_e164,
                            call_order_expr == pair_latest.c.max_dt,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        calls_window_full = len(calls) == limit * 3

        for call in calls:
            key = (call.our_e164, call.contact_e164)
            call_dt_val = call.ended_at or call.created_at
            cur = latest_call_by_pair.get(key)
            if cur is None:
                latest_call_by_pair[key] = call
            else:
                cur_dt_val = cur.ended_at or cur.created_at
                if (call_dt_val, call.id) > (cur_dt_val, cur.id):
                    latest_call_by_pair[key] = call

        call_ids = [c.id for c in latest_call_by_pair.values()]
        if call_ids:
            voicemails = list(
                (
                    await ctx.session.execute(
                        sa.select(Voicemail).where(
                            Voicemail.call_id.in_(call_ids),
                            Voicemail.transcript.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for vm in voicemails:
                cur = voicemail_by_call.get(vm.call_id)
                if cur is None or (vm.created_at, vm.id) > (cur.created_at, cur.id):
                    voicemail_by_call[vm.call_id] = vm

    pairs: dict[tuple[str, str], dict[str, Any]] = {}

    for thread in threads:
        msg = latest_msg_by_thread.get(thread.id)
        last_event_at = thread.last_message_at or thread.created_at
        unread = (
            bool(
                thread.last_message_at is not None
                and (
                    thread.last_read_at is None
                    or thread.last_read_at < thread.last_message_at
                )
            )
            and msg is not None
            and msg.direction == "inbound"
        )

        pairs[(thread.our_e164, thread.contact_e164)] = {
            "thread_id": thread.id,
            "contact_e164": thread.contact_e164,
            "our_e164": thread.our_e164,
            "last_event_at": last_event_at,
            "last_event_type": "message",
            "direction": msg.direction if msg else None,
            "snippet": _message_snippet(msg),
            "unread": unread,
            "status": thread.status,
            "important": thread.is_important,
            "snoozed_until": thread.snoozed_until,
            "sla_breached_at": thread.sla_breached_at,
            "thread": thread,
            "latest_msg": msg,
            "latest_call": None,
        }

    extra_threads_by_key: dict[tuple[str, str], MessageThread] = {}
    missing_keys = [key for key in latest_call_by_pair if key not in pairs]
    if missing_keys:
        candidate_threads = list(
            (
                await ctx.session.execute(
                    sa.select(MessageThread).where(
                        sa.or_(
                            *(
                                sa.and_(
                                    MessageThread.our_e164 == our_e164,
                                    MessageThread.contact_e164 == contact_e164,
                                )
                                for our_e164, contact_e164 in missing_keys
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        extra_threads_by_key = {
            (t.our_e164, t.contact_e164): t for t in candidate_threads
        }

    for (our_e164, contact_e164), call in latest_call_by_pair.items():
        key = (our_e164, contact_e164)
        call_dt = call.ended_at or call.created_at
        vm = voicemail_by_call.get(call.id)
        existing_pair = pairs.get(key)
        if existing_pair is not None:
            thread_for_pair: MessageThread | None = existing_pair["thread"]
            base_status = existing_pair["status"]
        else:
            extra_thread = extra_threads_by_key.get(key)
            thread_for_pair = extra_thread
            base_status = extra_thread.status if extra_thread is not None else "open"

        if vm is not None:
            event_type = "voicemail"
            snippet = _voicemail_snippet(vm)
            call_unread = False
        else:
            event_type = "call"
            snippet = _call_snippet(call)
            call_unread = _call_unread(call, call_dt, thread_for_pair)

        if existing_pair is None:
            pairs[key] = {
                "thread_id": thread_for_pair.id if thread_for_pair else None,
                "contact_e164": contact_e164,
                "our_e164": our_e164,
                "last_event_at": call_dt,
                "last_event_type": event_type,
                "direction": call.direction,
                "snippet": snippet,
                "unread": call_unread,
                "status": base_status,
                "important": thread_for_pair.is_important if thread_for_pair else False,
                "snoozed_until": thread_for_pair.snoozed_until if thread_for_pair else None,
                "sla_breached_at": thread_for_pair.sla_breached_at if thread_for_pair else None,
                "thread": thread_for_pair,
                "latest_msg": None,
                "latest_call": call,
            }
        elif call_dt > existing_pair["last_event_at"]:
            existing_pair.update(
                {
                    "last_event_at": call_dt,
                    "last_event_type": event_type,
                    "direction": call.direction,
                    "snippet": snippet,
                    "unread": call_unread,
                    "latest_call": call,
                }
            )

    # ---- Contact resolution - batch, via ContactPhone (P16 Opus review point 6) ----
    all_contact_e164s = {key[1] for key in pairs}
    contact_by_e164: dict[str, Contact] = {}
    if all_contact_e164s:
        contact_rows = (
            await ctx.session.execute(
                sa.select(ContactPhone.e164, Contact)
                .join(Contact, Contact.id == ContactPhone.contact_id)
                .where(ContactPhone.e164.in_(all_contact_e164s))
            )
        ).all()
        contact_by_e164 = {e164: contact for e164, contact in contact_rows}

    items: list[ConversationItem] = []
    for pair in pairs.values():
        if tab == "calls" and pair["last_event_type"] not in {"call", "voicemail"}:
            continue
        if filter_ == "open":
            if pair["status"] == "closed":
                continue
            snoozed_until = _aware(pair.get("snoozed_until"))
            if snoozed_until is not None and snoozed_until > now:
                continue
        if filter_ == "unread" and not pair["unread"]:
            continue
        if filter_ == "unresponded" and not _is_unresponded(pair):
            continue
        if filter_ == "important" and not pair["important"]:
            continue
        if filter_ == "snoozed":
            snoozed_until = _aware(pair.get("snoozed_until"))
            if snoozed_until is None or snoozed_until <= now:
                continue
        if filter_ == "overdue" and (
            pair.get("sla_breached_at") is None or pair["status"] == "closed"
        ):
            continue

        contact_obj = contact_by_e164.get(pair["contact_e164"])
        if q:
            contact_display = contact_obj.display_name if contact_obj else ""
            q_lower = q.lower()
            if (
                q_lower not in pair["contact_e164"].lower()
                and q_lower not in contact_display.lower()
            ):
                continue

        items.append(
            ConversationItem(
                contact_e164=pair["contact_e164"],
                our_e164=pair["our_e164"],
                inbox_id=inbox_id_by_e164.get(pair["our_e164"]),
                thread_id=pair["thread_id"],
                last_event_at=pair["last_event_at"],
                last_event_type=pair["last_event_type"],
                direction=pair["direction"],
                snippet=pair["snippet"],
                unread=pair["unread"],
                contact=ConversationContact(
                    id=contact_obj.id, display_name=contact_obj.display_name
                )
                if contact_obj
                else None,
                status=pair["status"],
                important=pair["important"],
                snoozed_until=_aware(pair["snoozed_until"]),
            )
        )

    items.sort(key=lambda x: (x.last_event_at, x.our_e164, x.contact_e164), reverse=True)

    if cursor_dt is not None:
        items = [
            item
            for item in items
            if (item.last_event_at, item.our_e164, item.contact_e164)
            < (cursor_dt, cursor_our, cursor_contact)
        ]

    has_more = len(items) > limit
    page = items[:limit]
    next_cursor = (
        _encode_pair_cursor(page[-1].last_event_at, page[-1].our_e164, page[-1].contact_e164)
        if has_more
        else None
    )

    if next_cursor is None and len(page) < limit and (thread_window_full or calls_window_full):
        frontiers: list[tuple[datetime, str, str]] = []
        if thread_window_full and threads:
            frontiers.append(
                min(
                    (t.last_message_at or t.created_at, t.our_e164, t.contact_e164)
                    for t in threads
                )
            )
        if calls_window_full and calls:
            frontiers.append(
                min((c.ended_at or c.created_at, c.our_e164, c.contact_e164) for c in calls)
            )
        if frontiers:
            frontier = max(frontiers)
            prev_cursor_tuple = (
                (cursor_dt, cursor_our, cursor_contact) if cursor_dt is not None else None
            )
            if prev_cursor_tuple is None or frontier < prev_cursor_tuple:
                next_cursor = _encode_pair_cursor(*frontier)

    # P26: SLA is fetched once for only the rows actually shipped on this page.
    thread_by_pair_key: dict[tuple[str, str], MessageThread] = {
        (pair["our_e164"], pair["contact_e164"]): pair["thread"]
        for pair in pairs.values()
        if pair.get("thread") is not None
    }
    page_threads = [
        thread_by_pair_key[(item.our_e164, item.contact_e164)]
        for item in page
        if (item.our_e164, item.contact_e164) in thread_by_pair_key
    ]
    if page_threads:
        sla_states = await inbox_sla_svc.sla_for_threads(ctx.session, page_threads)
        for item in page:
            thread = thread_by_pair_key.get((item.our_e164, item.contact_e164))
            if thread is not None:
                state = sla_states.get(thread.id)
                if state is not None:
                    item.sla = SlaOut(due_at=state.due_at, breached=state.breached)

    return ConversationListResponse(items=page, next_cursor=next_cursor)


@router.get("/conversations/{contact_e164}/timeline")
async def conversation_timeline(
    contact_e164: str,
    our_e164: str,
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> TimelineResponse:
    # P16 Opus review point 8: normalize the same way messages.py does before gating or
    # querying - a caller-supplied number format must not slip past the P15 check.
    contact_e164 = to_e164(contact_e164)
    our_e164 = to_e164(our_e164)

    # P16 Opus review point 1: call/voicemail events additionally require calls:read.
    has_calls_read = ctx.role.grants("calls:read")

    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.can_view(our_e164):
        raise NotFoundError("Conversation not found")

    thread = (
        await ctx.session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == our_e164,
                MessageThread.contact_e164 == contact_e164,
            )
        )
    ).scalar_one_or_none()

    cursor_dt: datetime | None = None
    cursor_id: uuid.UUID | None = None
    if cursor:
        cursor_dt, cursor_id = _decode_item_cursor(cursor)

    def apply_cursor(stmt, model):
        if cursor_dt is None or cursor_id is None:
            return stmt
        return stmt.where(
            sa.or_(
                model.created_at < cursor_dt,
                sa.and_(model.created_at == cursor_dt, model.id < cursor_id),
            )
        )

    message_stmt = (
        sa.select(Message)
        .join(MessageThread, Message.thread_id == MessageThread.id)
        .where(
            MessageThread.our_e164 == our_e164,
            MessageThread.contact_e164 == contact_e164,
        )
    )
    message_stmt = apply_cursor(message_stmt, Message)
    message_stmt = message_stmt.order_by(
        Message.created_at.desc(), Message.id.desc()
    ).limit(limit + 1)
    messages = list(
        (await ctx.session.execute(message_stmt)).scalars().all()
    )

    calls: list[Call] = []
    voicemail_page: list[Voicemail] = []

    if has_calls_read:
        call_stmt = sa.select(Call).where(
            Call.our_e164 == our_e164,
            Call.contact_e164 == contact_e164,
        )
        call_stmt = apply_cursor(call_stmt, Call)
        call_stmt = call_stmt.order_by(Call.created_at.desc(), Call.id.desc()).limit(
            limit + 1
        )
        calls = list((await ctx.session.execute(call_stmt)).scalars().all())

        voicemail_stmt = (
            sa.select(Voicemail)
            .join(Call, Voicemail.call_id == Call.id)
            .where(
                Call.our_e164 == our_e164,
                Call.contact_e164 == contact_e164,
            )
        )
        voicemail_stmt = apply_cursor(voicemail_stmt, Voicemail)
        voicemail_stmt = voicemail_stmt.order_by(
            Voicemail.created_at.desc(), Voicemail.id.desc()
        ).limit(limit + 1)
        voicemail_page = list(
            (await ctx.session.execute(voicemail_stmt)).scalars().all()
        )

    notes: list[ThreadNote] = []
    if thread is not None:
        note_stmt = sa.select(ThreadNote).where(ThreadNote.thread_id == thread.id)
        note_stmt = apply_cursor(note_stmt, ThreadNote)
        note_stmt = note_stmt.order_by(
            ThreadNote.created_at.desc(), ThreadNote.id.desc()
        ).limit(limit + 1)
        notes = list((await ctx.session.execute(note_stmt)).scalars().all())

    call_ids = {c.id for c in calls}

    all_call_voicemails: list[Voicemail] = []
    legs: list[CallLeg] = []
    events: list[VoiceEvent] = []

    if call_ids:
        all_call_voicemails = list(
            (
                await ctx.session.execute(
                    sa.select(Voicemail).where(Voicemail.call_id.in_(call_ids))
                )
            )
            .scalars()
            .all()
        )
        legs = list(
            (
                await ctx.session.execute(
                    sa.select(CallLeg).where(CallLeg.call_id.in_(call_ids))
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await ctx.session.execute(
                    sa.select(VoiceEvent).where(VoiceEvent.call_id.in_(call_ids))
                )
            )
            .scalars()
            .all()
        )

    recording_ids = {
        vm.recording_id
        for vm in list(voicemail_page) + all_call_voicemails
        if vm.recording_id
    }
    recordings: list[CallRecording] = []
    if call_ids or recording_ids:
        recording_conditions = []
        if call_ids:
            recording_conditions.append(CallRecording.call_id.in_(call_ids))
        if recording_ids:
            recording_conditions.append(CallRecording.id.in_(recording_ids))
        recordings = list(
            (
                await ctx.session.execute(
                    sa.select(CallRecording).where(sa.or_(*recording_conditions))
                )
            )
            .scalars()
            .all()
        )

    legs_by_call: dict[uuid.UUID, list[CallLeg]] = {}
    for leg in legs:
        legs_by_call.setdefault(leg.call_id, []).append(leg)

    events_by_call: dict[uuid.UUID, list[VoiceEvent]] = {}
    for event in events:
        events_by_call.setdefault(event.call_id, []).append(event)

    recordings_by_id = {r.id: r for r in recordings}
    has_voicemail_by_call = {vm.call_id for vm in all_call_voicemails}
    note_names = await _resolve_note_names(ctx.session, notes)

    scores_by_call: dict[uuid.UUID, CallScore] = {}
    transcript_call_ids: set[uuid.UUID] = set()
    assistant_names: dict[uuid.UUID, str] = {}
    if call_ids:
        score_rows = (
            await ctx.session.execute(
                sa.select(CallScore).where(CallScore.call_id.in_(call_ids))
            )
        ).scalars().all()
        scores_by_call = {score.call_id: score for score in score_rows}
        transcript_call_ids = set(
            (
                await ctx.session.execute(
                    sa.select(CallTranscriptSegment.call_id)
                    .where(CallTranscriptSegment.call_id.in_(call_ids))
                    .distinct()
                )
            ).scalars().all()
        )
        # One more batched query for the assistant NAMES those outcome rows point at -
        # never one lookup per call.
        profile_ids = {
            score.profile_id
            for score in scores_by_call.values()
            if score.profile_id is not None
        }
        if profile_ids:
            assistant_names = dict(
                (
                    await ctx.session.execute(
                        sa.select(AgentProfile.id, AgentProfile.name).where(
                            AgentProfile.id.in_(profile_ids)
                        )
                    )
                ).all()
            )

    note_names = await _resolve_note_names(ctx.session, notes)

    timeline_items: list[dict[str, Any]] = []

    for msg in messages:
        timeline_items.append(
            {
                "kind": "message",
                "id": msg.id,
                "direction": msg.direction,
                "body": msg.body,
                "media": msg.media,
                "status": msg.status,
                "occurred_at": msg.created_at,
                "error_code": msg.error_code,
                "route_reason": msg.route_reason,
            }
        )

    for call in calls:
        recording = _latest_recording(recordings, call.id)
        # A call with no call_scores row has no assistant block at all - which is what
        # every plain human call is, and always was.
        score = scores_by_call.get(call.id)
        timeline_items.append(
            {
                "kind": "call",
                "id": call.id,
                "direction": call.direction,
                "status": call.status,
                "duration_seconds": call.duration_seconds,
                "occurred_at": call.created_at,
                "answered_at": call.answered_at,
                "ended_at": call.ended_at,
                "failure_detail": _extract_failure_detail(
                    call,
                    legs_by_call.get(call.id, []),
                    events_by_call.get(call.id, []),
                ),
                "recording": {
                    "id": recording.id,
                    "status": recording.status,
                    "duration_seconds": recording.duration_seconds,
                }
                if recording
                else None,
                "has_voicemail": call.id in has_voicemail_by_call,
                # Stored sentence for provider-API calls; derived trunk sentence for LiveKit
                # room calls (same rule as GET /calls, see routes/calls.py).
                "route_reason": call.route_reason or _livekit_route_reason(call),
                "assistant": {
                    "name": assistant_names.get(score.profile_id)
                    if score is not None and score.profile_id is not None
                    else None,
                    "summary": score.summary,
                    "disposition": score.disposition,
                    "sentiment": score.sentiment,
                    "has_transcript": call.id in transcript_call_ids,
                }
                if score is not None
                else None,
            }
        )

    for vm in voicemail_page:
        recording = recordings_by_id.get(vm.recording_id) if vm.recording_id else None
        timeline_items.append(
            {
                "kind": "voicemail",
                "id": vm.id,
                "call_id": vm.call_id,
                "occurred_at": vm.created_at,
                "transcript": vm.transcript,
                "duration_seconds": recording.duration_seconds
                if recording
                else None,
                "transcript_status": vm.transcript_status,
                "recording": {
                    "id": recording.id,
                    "status": recording.status,
                    "duration_seconds": recording.duration_seconds,
                }
                if recording
                else None,
            }
        )

    for note in notes:
        timeline_items.append(_note_timeline_item(note, note_names))

    timeline_items.sort(key=lambda x: (x["occurred_at"], x["id"]), reverse=True)

    if cursor_dt is not None and cursor_id is not None:
        timeline_items = [
            item
            for item in timeline_items
            if (item["occurred_at"], item["id"]) < (cursor_dt, cursor_id)
        ]

    has_more = len(timeline_items) > limit
    page = timeline_items[:limit]
    next_cursor = (
        _encode_item_cursor(page[-1]["occurred_at"], page[-1]["id"])
        if has_more
        else None
    )

    snoozed_until = _aware(thread.snoozed_until) if thread is not None else None
    sla = None
    if thread is not None:
        sla_states = await inbox_sla_svc.sla_for_threads(ctx.session, [thread])
        state = sla_states.get(thread.id)
        if state is not None:
            sla = SlaOut(due_at=state.due_at, breached=state.breached)

    return TimelineResponse(
        items=page,
        next_cursor=next_cursor,
        snoozed_until=snoozed_until,
        sla=sla,
    )
