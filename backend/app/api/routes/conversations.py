from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, ValidationFailedError
from app.models import (
    Call,
    CallLeg,
    CallRecording,
    Contact,
    ContactPhone,
    Inbox,
    Message,
    MessageThread,
    OrgNumber,
    VoiceEvent,
    Voicemail,
)
from app.services import inbox_access as inbox_access_svc

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


# ----------------------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------------------
class ConversationContact(BaseModel):
    id: uuid.UUID
    display_name: str


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


class CallRecordingOut(BaseModel):
    id: uuid.UUID
    status: str
    duration_seconds: int | None


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


TimelineEvent = Annotated[
    MessageTimelineEvent | CallTimelineEvent | VoicemailTimelineEvent,
    Field(discriminator="kind"),
]


class TimelineResponse(BaseModel):
    items: list[TimelineEvent]
    next_cursor: str | None


# ----------------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------------
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
# Endpoints
# ----------------------------------------------------------------------------------
@router.get("/conversations")
async def list_conversations(
    ctx: Annotated[OrgContext, Depends(require_permission("inbox:read"))],
    inbox_id: uuid.UUID | None = None,
    tab: str = Query("chats", pattern="^(chats|calls)$"),
    # Named `filter_` internally so it never shadows the `filter` builtin; the wire
    # param name (`?filter=`) is unchanged via `alias` (P16 Opus review point 12).
    filter_: str = Query("open", alias="filter", pattern="^(open|unread|unresponded|all)$"),
    q: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> ConversationListResponse:
    # P16 Opus review point 1: call/voicemail-derived data additionally requires
    # calls:read (inbox:read alone only ever grants message visibility here).
    has_calls_read = ctx.role.grants("calls:read")

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
    # zero real matches (empirically: 20 read threads newer than 2 unread, limit=5).
    # tab, unresponded and call-derived unread stay Python-only (see the safety net at
    # the bottom of this function) - they depend on state this query alone can't express.
    if filter_ == "open":
        thread_stmt = thread_stmt.where(MessageThread.status != "closed")
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
    if q:
        # Same substring-match semantics as the Python q check below (contacts.py's
        # list_contacts uses this identical lower()/like() pattern), just pushed to SQL.
        needle = f"%{q.strip().lower()}%"
        matching_contact_e164s = (
            sa.select(ContactPhone.e164)
            .join(Contact, Contact.id == ContactPhone.contact_id)
            .where(sa.func.lower(Contact.display_name).like(needle))
        )
        thread_stmt = thread_stmt.where(
            sa.or_(
                sa.func.lower(MessageThread.contact_e164).like(needle),
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
        # Latest message PER thread via a group_by/max subquery join - never select a
        # thread's full message history (same pattern as the call pair_latest below).
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
                            # An untranscribed voicemail never becomes the last event -
                            # there is nothing yet worth showing as the snippet.
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
            "thread": thread,
            "latest_msg": msg,
            "latest_call": None,
        }

    # A call-derived pair whose MessageThread wasn't captured by the (bounded, possibly
    # filter_/q-narrowed) thread fetch above must still get its REAL thread state before
    # being merged - never silently default to "open"/no-read-cursor. This happens
    # whenever the thread's own message recency (or the filter_/q predicates just
    # pushed into thread_stmt) put it outside that window while its call stayed inside
    # the calls window (P16 Opus re-review: this is also what keeps filter=open/unread
    # correct now that thread_stmt is filtered - status and last_read_at for these
    # pairs would otherwise come from nowhere).
    extra_threads_by_key: dict[tuple[str, str], MessageThread] = {}
    missing_keys = [key for key in latest_call_by_pair if key not in pairs]
    if missing_keys:
        missing_our = {key[0] for key in missing_keys}
        missing_contact = {key[1] for key in missing_keys}
        missing_keys_set = set(missing_keys)
        candidate_threads = list(
            (
                await ctx.session.execute(
                    sa.select(MessageThread).where(
                        MessageThread.our_e164.in_(missing_our),
                        MessageThread.contact_e164.in_(missing_contact),
                    )
                )
            )
            .scalars()
            .all()
        )
        extra_threads_by_key = {
            (t.our_e164, t.contact_e164): t
            for t in candidate_threads
            if (t.our_e164, t.contact_e164) in missing_keys_set
        }

    for (our_e164, contact_e164), call in latest_call_by_pair.items():
        key = (our_e164, contact_e164)
        # P16 Opus review point 10: rank/merge by the same coalesce(ended_at,
        # created_at) expression the SQL candidate query above ranks by.
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
    # Every pair (message-led AND call-only) resolves its contact the same way a phone
    # number resolves everywhere else in this codebase (services/contacts.py:
    # find_contact_by_phone) - one batch lookup keyed by contact_e164, not a
    # thread.contact_id FK that a call-only pair never has.
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
        if filter_ == "open" and pair["status"] == "closed":
            continue
        if filter_ == "unread" and not pair["unread"]:
            continue
        if filter_ == "unresponded" and not _is_unresponded(pair):
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

    # P16 bugfix (Opus review, empirical repro: 20 read threads newer than 2 unread,
    # limit=5, filter=unread -> [] / None): a `limit * 3` per-source window can be
    # exhausted entirely by non-matching rows (tab=calls, unresponded, and call-based
    # unread all stay Python-only per the plan, so the calls window especially can fill
    # with rows the page below never keeps). When that happens the page can come back
    # shorter than `limit` with matches still sitting deeper in the table, even though
    # the normal has_more check above (driven purely by the materialized `items` count)
    # sees nothing to page past. Safety net: if any source's window was completely
    # full and the surviving page is short, hand back a continuation cursor instead of
    # reporting next_cursor=None as if every source were exhausted.
    #
    # P16 second-round bugfix (Opus review BLOCKER): the continuation cursor must NOT
    # be the overall oldest row across both sources - that publishes the deepest
    # frontier and makes the SQL `<= cursor_dt` bound on the next request skip every
    # not-yet-examined row of the SHALLOWER source that sits between the two
    # frontiers (repro: 15 decoy threads pass the loose SQL unread predicate but fail
    # the exact Python check at minutes 1-15, so the thread window - full at 15 rows -
    # never reaches the real unread thread at minute 20; 16 unrelated calls at minutes
    # 30-45 make the calls window - also full - bottom out at minute 44; taking the
    # global min picks minute 44, and `thread_order_expr <= (now - 44min)` then
    # excludes the minute-20 thread forever since it is NEWER than that bound).
    # Only a source whose window came back full might have more rows beyond what was
    # fetched; a source with a partial window already returned everything relevant and
    # must never cap how far the other source is allowed to look. So: compute the
    # oldest-examined tuple separately PER full source, then take the MAX (shallowest)
    # of those - the next request's `<=` bound stays permissive enough to reach every
    # unexamined row of every full source, while a source that already finished just
    # re-scans (and Python-filters out) rows it already covered. Emit the cursor only
    # when it is strictly older than the incoming cursor tuple, so it is guaranteed to
    # shrink each round and paging terminates.
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

    # P16 Opus review point 11: a voicemail on this page may reference a recording
    # whose owning call already fell outside `call_ids` (paginated away on an earlier
    # page) - so this lookup must run whenever EITHER source could name a recording,
    # never only nested inside `if call_ids:`.
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
            }
        )

    for call in calls:
        recording = _latest_recording(recordings, call.id)
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

    return TimelineResponse(items=page, next_cursor=next_cursor)
