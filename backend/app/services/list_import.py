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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.compliance import service as compliance_svc
from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import ContactList, ContactListRow
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


def parse_upload(filename: str, data: bytes) -> ParsedFile:
    """Dispatch on extension - CSV (stdlib) or XLSX (openpyxl), the only two DR-8 approves."""
    lower = (filename or "").lower()
    if lower.endswith(".xlsx"):
        return parse_xlsx_bytes(data)
    if lower.endswith(".csv"):
        return parse_csv_bytes(data)
    raise ValidationFailedError("Only .csv and .xlsx files are supported")


def preview(filename: str, data: bytes) -> dict:
    """Step 1: headers + first five rows + a suggested mapping. No DB write."""
    parsed = parse_upload(filename, data)
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
) -> None:
    """The import itself, as a free function so tests can await it directly instead of
    only through the fire-and-forget :func:`spawn_import` wrapper."""
    if "phone" not in mapping:
        raise ValidationFailedError("mapping must include 'phone'")

    parsed = parse_upload(filename, data)

    async with sessionmaker() as session:
        set_org_context(session, org_id)
        lst = await session.get(ContactList, list_id)
        if lst is None:
            return

        seen_e164: set[str] = set()
        counts = {"accepted": 0, "invalid": 0, "duplicate": 0, "dnc": 0}

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
                set_org_context(session, org_id)

        lst = await session.get(ContactList, list_id)
        lst.total_rows = len(parsed.rows)
        lst.accepted_count = counts["accepted"]
        lst.invalid_count = counts["invalid"]
        lst.duplicate_count = counts["duplicate"]
        lst.dnc_count = counts["dnc"]
        lst.status = "ready"
        await session.commit()
