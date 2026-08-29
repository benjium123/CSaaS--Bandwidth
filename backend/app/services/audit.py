"""Append-only audit log (P13 DR-6).

``record`` writes the row into the CALLER's session and does NOT commit - same
discipline as ``services/outbox.record_platform_event``: the caller controls the
transaction boundary, so a mutating route can add the audit row and commit once,
atomically with the domain change it describes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ValidationFailedError
from app.models import AuditLogEntry


def record(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> AuditLogEntry:
    row = AuditLogEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
    )
    session.add(row)
    return row


async def list_entries(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    action: str | None = None,
    target_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[AuditLogEntry], str | None]:
    """Cursor pagination, newest first. A cursor is ``"<iso created_at>|<id>"`` of the
    last row of the previous page - opaque to callers, generated only by this function."""
    stmt = sa.select(AuditLogEntry).where(AuditLogEntry.org_id == org_id)
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    if target_type:
        stmt = stmt.where(AuditLogEntry.target_type == target_type)
    if actor_user_id:
        stmt = stmt.where(AuditLogEntry.actor_user_id == actor_user_id)

    if cursor:
        created_str, _, id_str = cursor.partition("|")
        try:
            created_at = datetime.fromisoformat(created_str)
            cursor_id = uuid.UUID(id_str)
        except ValueError as exc:
            raise ValidationFailedError("Invalid cursor") from exc
        stmt = stmt.where(
            sa.or_(
                AuditLogEntry.created_at < created_at,
                sa.and_(
                    AuditLogEntry.created_at == created_at, AuditLogEntry.id < cursor_id
                ),
            )
        )

    stmt = stmt.order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc()).limit(
        limit + 1
    )
    rows = list((await session.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"
    return rows, next_cursor
