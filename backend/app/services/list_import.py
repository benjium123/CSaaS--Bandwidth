"""DB-aware contact-list import (phase-11-plan DR-8/DR-9), built on the pure parsing core
in ``app.services.list_parsing``.

Two-step upload:
  1. The route (``api/routes/outbound.py``) parses the uploaded bytes for an immediate
     preview + suggested mapping and creates the ``ContactList`` row (status
     ``importing``); it stashes the SAME raw bytes in the app's object store so this
     module can re-parse them once the operator confirms a column mapping.
  2. The route's commit step calls :func:`spawn_import`, which owns a background
     asyncio task on its OWN session - the same fire-and-forget shape as
     ``services.sms_agent.spawn_from_ingest`` / ``voice_plane.service.start_room_call``'s
     dial task. :func:`wait_for_pending_import_tasks` is the test hook that awaits it
     deterministically instead of sleeping/polling.

Per-row outcome (DR-9): ``invalid`` (unparseable/impossible phone) -> ``duplicate``
(same E.164 already seen earlier in THIS list; first wins) -> ``dnc`` (internal DNC or the
contact's latest consent event is an opt-out - the row is KEPT, never deleted, and the
compliance gate still re-checks it at send time) -> otherwise ``accepted``, which upserts a
``Contact`` matched on E.164.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.compliance import service as compliance_svc
from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import ContactList, ContactListRow, Department, OrgMembership, User
from app.services.contacts import resolve_or_create_contact
from app.services.list_parsing import (
    ParsedFile,
    extract_row,
    normalize_phone,
    parse_csv_bytes,
    parse_xlsx_bytes,
    suggest_mapping,
)

log = structlog.get_logger("list_import")

#: The background task commits every this-many rows, so a very large list shows live
#: progress on the list's counters while it runs and a crash mid-import loses at most one
#: batch rather than silently holding the whole thing open in a single giant transaction.
COMMIT_EVERY = 200

#: Upload guardrails (enforced by the route, before/while parsing - see api/routes/
#: outbound.py). Kept here so the limits live next to the format they bound, not scattered
#: across the route file.
MAX_LIST_BYTES = 10_000_000
MAX_LIST_ROWS = 100_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_upload(filename: str, data: bytes, *, max_rows: int | None = None) -> ParsedFile:
    """Dispatch on extension - CSV (stdlib) or XLSX (openpyxl), the only two DR-8 approves."""
    lower = (filename or "").lower()
    if lower.endswith(".xlsx"):
        return parse_xlsx_bytes(data, max_rows=max_rows)
    if lower.endswith(".csv"):
        return parse_csv_bytes(data, max_rows=max_rows)
    raise ValidationFailedError("Only .csv and .xlsx files are supported")


def preview(filename: str, data: bytes) -> dict:
    """Step 1: headers + first five rows + a suggested mapping. No DB write.

    6.7: aborts DURING parsing once MAX_LIST_ROWS is exceeded, rather than fully
    materializing an oversized file before rejecting it.
    """
    parsed = parse_upload(filename, data, max_rows=MAX_LIST_ROWS)
    if not parsed.headers:
        raise ValidationFailedError("The file has no header row")
    return {
        "headers": parsed.headers,
        "preview_rows": parsed.preview,
        "suggested_mapping": suggest_mapping(parsed.headers),
        "row_count": len(parsed.rows),
    }


# --------------------------------------------------------------------------------------
# Background task tracking - same shape as sms_agent._SMS_TASKS / voice_plane._DIAL_TASKS.
# --------------------------------------------------------------------------------------
_IMPORT_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _IMPORT_TASKS.add(task)
    task.add_done_callback(_IMPORT_TASKS.discard)
    return task


async def wait_for_pending_import_tasks() -> None:
    """Test-only hook: await every in-flight background import task deterministically.
    Safe to call with nothing pending."""
    pending = list(_IMPORT_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def spawn_import(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    list_id: uuid.UUID,
    org_id: uuid.UUID,
    filename: str,
    data: bytes,
    mapping: dict[str, str],
    assign_all_to: uuid.UUID | None = None,
    assign_department: uuid.UUID | None = None,
) -> asyncio.Task:
    """Fire-and-forget: this function itself never raises. Mirrors
    ``sms_agent.spawn_from_ingest`` - a background failure is caught, logged, and recorded
    on the list row (status ``failed``) rather than lost."""

    async def _run() -> None:
        try:
            await run_import(
                sessionmaker,
                list_id=list_id,
                org_id=org_id,
                filename=filename,
                data=data,
                mapping=mapping,
                assign_all_to=assign_all_to,
                assign_department=assign_department,
            )
        except Exception:  # noqa: BLE001 - background task: must never crash the loop
            log.exception("list_import_task_crashed", list_id=str(list_id))
            try:
                async with sessionmaker() as session:
                    set_org_context(session, org_id)
                    row = await session.get(ContactList, list_id)
                    if row is not None and row.status == "importing":
                        row.status = "failed"
                        row.error = "Import crashed; see server logs"[:255]
                        # D5: clear the one-shot claim on terminal failure too - a
                        # status reset back to "importing" (a retry path) must not
                        # find import_started_at already set and be treated as a
                        # phantom concurrent import.
                        row.import_started_at = None
                        await session.commit()
            except Exception:  # noqa: BLE001 - best-effort failure recording only
                log.exception("list_import_failure_record_failed", list_id=str(list_id))

    return _spawn(_run())


async def run_import(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    list_id: uuid.UUID,
    org_id: uuid.UUID,
    filename: str,
    data: bytes,
    mapping: dict[str, str],
    assign_all_to: uuid.UUID | None = None,
    assign_department: uuid.UUID | None = None,
) -> dict:
    """The import itself, as a free function so tests can await it directly instead of
    only through the fire-and-forget :func:`spawn_import` wrapper."""
    if "phone" not in mapping:
        raise ValidationFailedError("mapping must include 'phone'")

    parsed = parse_upload(filename, data, max_rows=MAX_LIST_ROWS)

    async with sessionmaker() as session:
        set_org_context(session, org_id)
        lst = await session.get(ContactList, list_id)
        if lst is None:
            return {"unknown_owner_emails": [], "assigned": 0}

        # B2 (Opus P22 verify): the route validates these, but the service is also called
        # directly - never write an owner/department that is not in this org.
        if assign_all_to is not None:
            _m = (await session.execute(
                sa.select(OrgMembership.id).where(OrgMembership.user_id == assign_all_to)
            )).scalar_one_or_none()
            if _m is None:
                assign_all_to = None
        if assign_department is not None:
            if await session.get(Department, assign_department) is None:
                assign_department = None
        email_map: dict[str, uuid.UUID] = {}
        member_rows = (
            await session.execute(
                sa.select(User.id, User.email)
                .join(OrgMembership, OrgMembership.user_id == User.id)
                .where(OrgMembership.org_id == org_id)
            )
        ).all()
        for user_id, email in member_rows:
            email_map[email.lower()] = user_id

        seen_e164: set[str] = set()
        counts = {"accepted": 0, "invalid": 0, "duplicate": 0, "dnc": 0}
        unknown_owner_emails: set[str] = set()
        assigned = 0

        for row_number, raw_row in enumerate(parsed.rows, start=1):
            fields = extract_row(raw_row, mapping)
            e164, parse_reason = normalize_phone(fields.get("phone", ""))
            contact_id = None

            if e164 is None:
                status, reason = "invalid", parse_reason or "invalid phone"
            elif e164 in seen_e164:
                status, reason = "duplicate", "duplicate phone number within this list"
            else:
                seen_e164.add(e164)
                opted_out = await compliance_svc.is_opted_out(session, e164)
                on_dnc = await compliance_svc.is_dnc(session, e164)
                if opted_out or on_dnc:
                    status = "dnc"
                    reason = "opted_out" if opted_out else "dnc"
                else:
                    status, reason = "accepted", None

            if status == "accepted":
                contact = await resolve_or_create_contact(session, org_id, e164)
                # Fill in blanks only - an import must never clobber data an operator or a
                # prior conversation already put on this contact.
                if fields.get("first_name") and not contact.first_name:
                    contact.first_name = fields["first_name"][:127]
                if fields.get("last_name") and not contact.last_name:
                    contact.last_name = fields["last_name"][:127]

                raw_owner = fields.get("owner", "").strip().lower()
                new_owner_id: uuid.UUID | None = None
                if raw_owner:
                    known_owner_id = email_map.get(raw_owner)
                    if known_owner_id is None:
                        unknown_owner_emails.add(raw_owner)
                    else:
                        new_owner_id = known_owner_id
                else:
                    new_owner_id = assign_all_to

                if new_owner_id is not None and contact.owner_user_id is None:
                    contact.owner_user_id = new_owner_id
                    assigned += 1

                if assign_department is not None and contact.department_id is None:
                    contact.department_id = assign_department

                contact_id = contact.id

            counts[status] += 1
            session.add(
                ContactListRow(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    list_id=list_id,
                    row_number=row_number,
                    raw=dict(raw_row),
                    e164=e164,
                    contact_id=contact_id,
                    status=status,
                    reason=reason,
                    fields=fields,
                )
            )

            if row_number % COMMIT_EVERY == 0:
                await session.commit()
                # 6.17: the periodic commit already bounds the DB-side transaction size,
                # but the ORM identity map keeps every row/contact object from every
                # earlier batch alive in memory for the rest of the run unless expunged -
                # a 100k-row import would otherwise hold the whole thing regardless of
                # how often it commits.
                session.expunge_all()
                set_org_context(session, org_id)

        lst = await session.get(ContactList, list_id)
        lst.total_rows = len(parsed.rows)
        lst.accepted_count = counts["accepted"]
        lst.invalid_count = counts["invalid"]
        lst.duplicate_count = counts["duplicate"]
        lst.dnc_count = counts["dnc"]
        lst.status = "ready"

        summary = {
            "unknown_owner_emails": sorted(unknown_owner_emails),
            "assigned": assigned,
            "counts": dict(counts),
            "finished_at": _now().isoformat(),
        }
        lst.import_summary = summary
        await session.commit()

        log.info("list_import_owner_summary", **summary)
        return summary
