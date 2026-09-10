from __future__ import annotations

import asyncio
import csv
import json
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import NotFoundError, ValidationFailedError
from app.models import (
    Contact,
    ContactListRow,
    ContactNote,
    ContactPhone,
    ContactTag,
    CustomFieldDef,
    Department,
    MessageThread,
    Org,
    Tag,
    User,
)
from app.services import audit as audit_svc
from app.services import contact_visibility
from app.services.contacts import BUILTIN_CONTACT_ATTRIBUTES, active_contacts_filter

log = structlog.get_logger("contact_export")

EXPORT_BATCH_SIZE = 500
DUPES_CACHE_TTL_SECONDS = 24 * 3600


def export_status_key(org_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"org/{org_id}/exports/{job_id}/status.json"


def export_csv_key(org_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"org/{org_id}/exports/{job_id}/contacts.csv"


async def write_export_status(store, org_id: uuid.UUID, job_id: uuid.UUID, status: dict) -> None:
    data = json.dumps(status, default=str).encode("utf-8")
    await store.put(export_status_key(org_id, job_id), data, "application/json")


async def read_export_status(store, org_id: uuid.UUID, job_id: uuid.UUID) -> dict | None:
    try:
        data = await store.get(export_status_key(org_id, job_id))
    except KeyError:
        return None
    return json.loads(data.decode("utf-8"))


async def read_export_csv(store, org_id: uuid.UUID, job_id: uuid.UUID) -> bytes:
    return await store.get(export_csv_key(org_id, job_id))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_EXPORT_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _EXPORT_TASKS.add(task)
    task.add_done_callback(_EXPORT_TASKS.discard)
    return task


async def wait_for_pending_export_tasks() -> None:
    """Test-only hook: await every in-flight background export task deterministically."""
    pending = list(_EXPORT_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def spawn_export(
    sessionmaker: async_sessionmaker[AsyncSession],
    store,
    *,
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    requester_user_id: uuid.UUID | None,
    permissions: list[str],
    filters: dict | None = None,
    contact_id: uuid.UUID | None = None,
) -> asyncio.Task:
    """Fire-and-forget: this function itself never raises. Mirrors list_import's shape."""

    async def _run() -> None:
        try:
            await run_export(
                sessionmaker,
                store,
                org_id=org_id,
                job_id=job_id,
                requester_user_id=requester_user_id,
                permissions=permissions,
                filters=filters,
                contact_id=contact_id,
            )
        except Exception:  # noqa: BLE001 - background task: must never crash the loop
            log.exception("contact_export_task_crashed", job_id=str(job_id))
            try:
                await write_export_status(
                    store,
                    org_id,
                    job_id,
                    {
                        "status": "failed",
                        "rows": 0,
                        "error": "That export did not finish. Please try again.",
                        "job_id": str(job_id),
                        "requested_by": str(requester_user_id) if requester_user_id else None,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:  # noqa: BLE001 - best-effort failure recording only
                log.exception("contact_export_failure_record_failed", job_id=str(job_id))

    return _spawn(_run())


async def run_export(
    sessionmaker: async_sessionmaker[AsyncSession],
    store,
    *,
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    requester_user_id: uuid.UUID | None,
    permissions: list[str],
    filters: dict | None = None,
    contact_id: uuid.UUID | None = None,
) -> dict:
    created_at = datetime.now(timezone.utc)
    status = {
        "status": "running",
        "rows": 0,
        "error": None,
        "job_id": str(job_id),
        "requested_by": str(requester_user_id) if requester_user_id else None,
        "created_at": created_at.isoformat(),
        "finished_at": None,
    }
    await write_export_status(store, org_id, job_id, status)

    rows_written = 0
    try:
        async with sessionmaker() as session:
            set_org_context(session, org_id)
            org = await session.get(Org, org_id)
            if org is None:
                raise NotFoundError("Workspace not found")

            vis_scope = await contact_visibility.resolve_scope(
                session,
                org,
                user_id=requester_user_id,
                permissions=permissions,
            )
            vis_filter = contact_visibility.visible_contacts_filter(vis_scope)

            stmt = sa.select(Contact).where(active_contacts_filter())
            if vis_filter is not None:
                stmt = stmt.where(vis_filter)

            if filters:
                scope_name = filters.get("scope")
                if scope_name is not None:
                    if scope_name == "mine":
                        if requester_user_id is None:
                            stmt = stmt.where(sa.false())
                        else:
                            stmt = stmt.where(Contact.owner_user_id == requester_user_id)
                    elif scope_name == "team":
                        if not vis_scope.department_ids:
                            stmt = stmt.where(sa.false())
                        else:
                            stmt = stmt.where(Contact.department_id.in_(vis_scope.department_ids))
                    elif scope_name == "unowned":
                        stmt = stmt.where(
                            Contact.owner_user_id.is_(None),
                            Contact.department_id.is_(None),
                        )
                    else:
                        raise ValidationFailedError("scope must be mine, team or unowned")

                q = filters.get("q")
                if q:
                    needle = f"%{_escape_like(q.strip().lower())}%"
                    phone_match = sa.select(ContactPhone.contact_id).where(
                        sa.func.lower(ContactPhone.e164).like(needle, escape="\\")
                    )
                    stmt = stmt.where(
                        sa.or_(
                            sa.func.lower(Contact.display_name).like(needle, escape="\\"),
                            Contact.id.in_(phone_match),
                        )
                    )

            if contact_id is not None:
                stmt = stmt.where(Contact.id == contact_id)

            stmt = stmt.order_by(Contact.display_name.asc(), Contact.id.asc())

            custom_fields = sorted(
                (await session.execute(sa.select(CustomFieldDef.key))).scalars().all()
            )
            builtins = sorted(BUILTIN_CONTACT_ATTRIBUTES)

            headers = [
                "id",
                "display_name",
                "first_name",
                "last_name",
                "primary_phone",
                "other_phones",
                "tags",
                "owner_email",
                "team",
                "created_at",
            ] + builtins + custom_fields

            with tempfile.SpooledTemporaryFile(
                mode="w+", max_size=1024 * 1024, encoding="utf-8", newline=""
            ) as spool:
                writer = csv.writer(spool, lineterminator="\n")
                writer.writerow(headers)

                result = await session.stream_scalars(
                    stmt.execution_options(yield_per=EXPORT_BATCH_SIZE)
                )
                async for batch in result.partitions(EXPORT_BATCH_SIZE):
                    batch_contacts = list(batch)
                    if not batch_contacts:
                        continue

                    ids = [c.id for c in batch_contacts]

                    phones = (
                        await session.execute(
                            sa.select(ContactPhone).where(ContactPhone.contact_id.in_(ids))
                        )
                    ).scalars().all()
                    phones_by_contact: dict[uuid.UUID, list[ContactPhone]] = {}
                    for phone in phones:
                        phones_by_contact.setdefault(phone.contact_id, []).append(phone)

                    tag_rows = (
                        await session.execute(
                            sa.select(ContactTag.contact_id, Tag.name)
                            .join(Tag, Tag.id == ContactTag.tag_id)
                            .where(ContactTag.contact_id.in_(ids))
                            .order_by(Tag.name.asc(), Tag.id.asc())
                        )
                    ).all()
                    tags_by_contact: dict[uuid.UUID, list[str]] = {}
                    for cid, tag_name in tag_rows:
                        tags_by_contact.setdefault(cid, []).append(tag_name)

                    owner_ids = [
                        c.owner_user_id for c in batch_contacts if c.owner_user_id is not None
                    ]
                    owner_email_by_id: dict[uuid.UUID, str] = {}
                    if owner_ids:
                        user_rows = (
                            await session.execute(
                                sa.select(User.id, User.email).where(User.id.in_(owner_ids))
                            )
                        ).all()
                        owner_email_by_id = dict(user_rows)

                    dept_ids = [
                        c.department_id for c in batch_contacts if c.department_id is not None
                    ]
                    dept_name_by_id: dict[uuid.UUID, str] = {}
                    if dept_ids:
                        dept_rows = (
                            await session.execute(
                                sa.select(Department.id, Department.name).where(
                                    Department.id.in_(dept_ids)
                                )
                            )
                        ).all()
                        dept_name_by_id = dict(dept_rows)

                    for contact in batch_contacts:
                        contact_phones = sorted(
                            phones_by_contact.get(contact.id, []),
                            key=lambda p: (not p.is_primary, p.e164 or ""),
                        )
                        primary_phone = contact_phones[0].e164 if contact_phones else ""
                        other_phones = [p.e164 for p in contact_phones[1:]]
                        attrs = contact.attributes or {}
                        row = [
                            str(contact.id),
                            contact.display_name,
                            contact.first_name or "",
                            contact.last_name or "",
                            primary_phone or "",
                            ";".join(other_phones),
                            ";".join(tags_by_contact.get(contact.id, [])),
                            owner_email_by_id.get(contact.owner_user_id, "")
                            if contact.owner_user_id
                            else "",
                            dept_name_by_id.get(contact.department_id, "")
                            if contact.department_id
                            else "",
                            contact.created_at.isoformat() if contact.created_at else "",
                        ]
                        for key in builtins:
                            row.append(attrs.get(key) or "")
                        for key in custom_fields:
                            row.append(attrs.get(key) or "")
                        writer.writerow(row)
                        rows_written += 1

                spool.flush()
                spool.seek(0)
                csv_data = spool.read().encode("utf-8")

            await store.put(
                export_csv_key(org_id, job_id), csv_data, "text/csv; charset=utf-8"
            )

            status = {
                "status": "done",
                "rows": rows_written,
                "error": None,
                "job_id": str(job_id),
                "requested_by": str(requester_user_id) if requester_user_id else None,
                "created_at": created_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            await write_export_status(store, org_id, job_id, status)
            return status

    except Exception:
        log.exception("contact_export_failed", org_id=str(org_id), job_id=str(job_id))
        status = {
            "status": "failed",
            "rows": rows_written,
            "error": "That export did not finish. Please try again.",
            "job_id": str(job_id),
            "requested_by": str(requester_user_id) if requester_user_id else None,
            "created_at": created_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await write_export_status(store, org_id, job_id, status)
        except Exception:
            log.exception(
                "contact_export_failure_record_failed",
                org_id=str(org_id),
                job_id=str(job_id),
            )
        return status


# --------------------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------------------

# Redis key namespace (``dupes:{org}``, 24 h) the deployment will move to a shared Redis
# under; the app has no Redis client today, so an in-process cache with the same key and
# TTL is what ships.
_DUPES_CACHE: dict[str, tuple[float, list[dict]]] = {}


def dupes_cache_key(org_id: uuid.UUID) -> str:
    return f"dupes:{org_id}"


def cache_get(org_id: uuid.UUID) -> list[dict] | None:
    key = dupes_cache_key(org_id)
    item = _DUPES_CACHE.get(key)
    if item is None:
        return None
    expires_at, groups = item
    if time.monotonic() >= expires_at:
        _DUPES_CACHE.pop(key, None)
        return None
    return groups


def cache_put(org_id: uuid.UUID, groups: list[dict]) -> None:
    _DUPES_CACHE[dupes_cache_key(org_id)] = (
        time.monotonic() + DUPES_CACHE_TTL_SECONDS,
        groups,
    )


def clear_dupes_cache() -> None:
    _DUPES_CACHE.clear()


def _normalised_name(contact: Contact) -> str:
    parts = " ".join([contact.first_name or "", contact.last_name or ""]).strip()
    if parts:
        return " ".join(parts.lower().split())
    return " ".join((contact.display_name or "").lower().split())


def _normalised_email(attributes: dict) -> str:
    value = attributes.get("email")
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _phone_key(e164: str) -> str | None:
    digits = re.sub(r"\D", "", e164 or "")
    if not digits:
        return None
    return digits[-10:]


async def find_duplicates(session: AsyncSession, org_id: uuid.UUID) -> list[dict]:
    set_org_context(session, org_id)

    records: list[dict] = []
    stmt = sa.select(Contact).where(active_contacts_filter()).order_by(Contact.id.asc())
    result = await session.stream_scalars(stmt.execution_options(yield_per=EXPORT_BATCH_SIZE))

    async for batch in result.partitions(EXPORT_BATCH_SIZE):
        batch_contacts = list(batch)
        if not batch_contacts:
            continue

        ids = [c.id for c in batch_contacts]
        phone_rows = (
            await session.execute(
                sa.select(ContactPhone.contact_id, ContactPhone.e164).where(
                    ContactPhone.contact_id.in_(ids)
                )
            )
        ).all()
        phones_by_id: dict[uuid.UUID, list[str]] = {}
        for cid, e164 in phone_rows:
            phones_by_id.setdefault(cid, []).append(e164)

        for contact in batch_contacts:
            records.append(
                {
                    "id": str(contact.id),
                    "name": _normalised_name(contact),
                    "email": _normalised_email(contact.attributes or {}),
                    "phone_keys": {
                        key
                        for key in (
                            _phone_key(e164) for e164 in phones_by_id.get(contact.id, [])
                        )
                        if key is not None
                    },
                }
            )

    name_email_groups: dict[tuple[str, str], set[str]] = {}
    phone_groups: dict[str, set[str]] = {}

    for rec in records:
        name = rec["name"]
        email = rec["email"]
        if name and email:
            name_email_groups.setdefault((name, email), set()).add(rec["id"])
        for key in rec["phone_keys"]:
            phone_groups.setdefault(key, set()).add(rec["id"])

    groups: list[dict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    for ids in name_email_groups.values():
        if len(ids) >= 2:
            group = {"contact_ids": sorted(ids), "reason": "name_and_email"}
            tag = (group["reason"], tuple(group["contact_ids"]))
            if tag not in seen:
                seen.add(tag)
                groups.append(group)

    for ids in phone_groups.values():
        if len(ids) >= 2:
            group = {"contact_ids": sorted(ids), "reason": "phone"}
            tag = (group["reason"], tuple(group["contact_ids"]))
            if tag not in seen:
                seen.add(tag)
                groups.append(group)

    groups.sort(key=lambda g: (g["reason"], g["contact_ids"]))
    return groups


async def duplicates_tick(session: AsyncSession) -> dict[str, int]:
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    result = {"orgs": len(org_ids), "groups": 0}

    for org_id in org_ids:
        set_org_context(session, org_id)
        groups = await find_duplicates(session, org_id)
        cache_put(org_id, groups)
        result["groups"] += len(groups)

    return result


async def duplicates_for_contact(
    session: AsyncSession, org_id: uuid.UUID, contact_id: uuid.UUID
) -> list[dict]:
    cached = cache_get(org_id)
    if cached is None:
        cached = await find_duplicates(session, org_id)
        cache_put(org_id, cached)

    target = str(contact_id)
    return [group for group in cached if target in group["contact_ids"]]


# --------------------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------------------

def _rowcount(result) -> int:  # noqa: ANN001
    count = result.rowcount or 0
    return count if count > 0 else 0


def _has_value(value) -> bool:  # noqa: ANN001
    return value is not None and value != ""


async def merge_contacts(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    survivor: Contact,
    losers: list[Contact],
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> dict:
    set_org_context(session, org_id)

    if not losers:
        raise ValidationFailedError("Choose at least one contact to merge")

    unique_losers: list[Contact] = []
    seen_loser_ids: set[uuid.UUID] = set()
    for loser in losers:
        if loser.id not in seen_loser_ids:
            unique_losers.append(loser)
            seen_loser_ids.add(loser.id)
    losers = unique_losers

    if survivor.merged_into_contact_id is not None:
        raise ValidationFailedError("The contact to keep is already merged")

    for loser in losers:
        if loser.id == survivor.id:
            raise ValidationFailedError("A contact cannot be merged into itself")
        if loser.merged_into_contact_id is not None:
            raise ValidationFailedError("One of the selected contacts is already merged")

    loser_ids = [loser.id for loser in losers]

    phones_moved = _rowcount(
        await session.execute(
            sa.update(ContactPhone)
            .where(ContactPhone.contact_id.in_(loser_ids))
            .values(contact_id=survivor.id)
        )
    )

    notes_moved = _rowcount(
        await session.execute(
            sa.update(ContactNote)
            .where(ContactNote.contact_id.in_(loser_ids))
            .values(contact_id=survivor.id)
        )
    )

    threads_moved = _rowcount(
        await session.execute(
            sa.update(MessageThread)
            .where(MessageThread.contact_id.in_(loser_ids))
            .values(contact_id=survivor.id)
        )
    )

    list_rows_moved = _rowcount(
        await session.execute(
            sa.update(ContactListRow)
            .where(ContactListRow.contact_id.in_(loser_ids))
            .values(contact_id=survivor.id)
        )
    )

    survivor_tag_ids = set(
        (
            await session.execute(
                sa.select(ContactTag.tag_id).where(ContactTag.contact_id == survivor.id)
            )
        )
        .scalars()
        .all()
    )
    loser_tag_rows = list(
        (
            await session.execute(
                sa.select(ContactTag).where(ContactTag.contact_id.in_(loser_ids))
            )
        )
        .scalars()
        .all()
    )

    tags_moved = 0
    for row in loser_tag_rows:
        if row.tag_id in survivor_tag_ids:
            await session.delete(row)
        else:
            row.contact_id = survivor.id
            survivor_tag_ids.add(row.tag_id)
            tags_moved += 1

    new_attributes = dict(survivor.attributes or {})
    for loser in losers:
        for key, value in (loser.attributes or {}).items():
            if key not in new_attributes:
                new_attributes[key] = value
            elif _has_value(value) and not _has_value(new_attributes[key]):
                new_attributes[key] = value
    survivor.attributes = new_attributes

    for loser in losers:
        if not survivor.first_name and loser.first_name:
            survivor.first_name = loser.first_name
        if not survivor.last_name and loser.last_name:
            survivor.last_name = loser.last_name
        if not survivor.company_id and loser.company_id:
            survivor.company_id = loser.company_id
        if not survivor.timezone and loser.timezone:
            survivor.timezone = loser.timezone

    for loser in losers:
        loser.merged_into_contact_id = survivor.id

    audit_svc.record(
        session,
        org_id,
        action="contact.merge",
        target_type="contact",
        target_id=str(survivor.id),
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        detail={
            "survivor_id": str(survivor.id),
            "loser_ids": [str(c.id) for c in losers],
            "phones_moved": phones_moved,
            "threads_moved": threads_moved,
            "notes_moved": notes_moved,
            "tags_moved": tags_moved,
            "list_rows_moved": list_rows_moved,
        },
    )

    await session.flush()
    _DUPES_CACHE.pop(dupes_cache_key(org_id), None)

    return {
        "phones_moved": phones_moved,
        "threads_moved": threads_moved,
        "notes_moved": notes_moved,
        "tags_moved": tags_moved,
        "list_rows_moved": list_rows_moved,
    }
