"""Help menu: support contact details (ops-editable) and support requests.

A member sends a request from the Help menu; it lands in the Switchboard's Support tab and
ops are emailed. Ops answer from the Switchboard; the answer is emailed to the person.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import NotFoundError, ValidationFailedError
from app.models import Org, PlatformSetting, SupportRequest, User

log = structlog.get_logger("support")

CONTACTS_KEY = "support_contacts"
#: What the Help menu shows until ops change it in the Switchboard (the same contact the
#: legal pages on ringlite.io publish).
DEFAULT_CONTACTS: dict[str, str | None] = {
    "email": "support@ringlite.io",
    "phone": "+1 (469) 461-7576",
    "knowledge_base_url": None,
    "whats_new_url": None,
    "status_url": "/status",
    "terms_url": "/terms",
    "privacy_url": "/privacy",
}
_URL_KEYS = ("knowledge_base_url", "whats_new_url", "status_url", "terms_url", "privacy_url")


async def contacts(session: AsyncSession) -> dict:
    row = await session.get(PlatformSetting, CONTACTS_KEY)
    stored = (row.value if row is not None else None) or {}
    return {k: stored.get(k, v) for k, v in DEFAULT_CONTACTS.items()}


def _clean_contacts(payload: dict) -> dict:
    out: dict[str, str | None] = {}
    for key in DEFAULT_CONTACTS:
        value = payload.get(key)
        value = (value or "").strip() or None
        if value and key in _URL_KEYS and not (value.startswith("https://") or value.startswith("/")):
            raise ValidationFailedError(f"{key} must start with https:// or /")
        if value and len(value) > 500:
            raise ValidationFailedError(f"{key} is too long")
        out[key] = value
    return out


async def set_contacts(session: AsyncSession, payload: dict, *, actor_user_id: uuid.UUID) -> dict:
    """Ops: replace the Help menu's contact details. Does not commit."""
    value = _clean_contacts(payload)
    row = await session.get(PlatformSetting, CONTACTS_KEY)
    if row is None:
        row = PlatformSetting(key=CONTACTS_KEY, value=value, updated_by=actor_user_id)
        session.add(row)
    else:
        row.value = value
        row.updated_by = actor_user_id
    return value


async def create_request(
    session: AsyncSession, settings, org_id: uuid.UUID, user: User,  # noqa: ANN001
    *, subject: str, body: str, page: str | None,
) -> SupportRequest:
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise ValidationFailedError("Add a subject and describe what you need")
    if len(subject) > 200 or len(body) > 5000:
        raise ValidationFailedError("Keep the subject under 200 and the message under 5,000 characters")
    set_org_context(session, org_id)
    row = SupportRequest(
        id=uuid.uuid4(), org_id=org_id, user_id=user.id, email=user.email,
        subject=subject, body=body, page=(page or None) and page[:255], status="open",
    )
    session.add(row)
    await session.commit()
    await _notify_ops(session, settings, row)
    return row


async def _notify_ops(session: AsyncSession, settings, row: SupportRequest) -> None:  # noqa: ANN001
    """Email platform admins that a request arrived. Never fails the request."""
    try:
        from app.services import email_delivery, mailer

        emails = await email_delivery._operator_emails()
        if not emails:
            return
        org = (
            await session.execute(
                sa.select(Org.name).where(Org.id == row.org_id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        await mailer.send(
            settings,
            emails,
            f"Support request: {row.subject}",
            f"From {row.email} ({org or 'workspace'}).\n\n{row.body}\n\n"
            "Answer it in the Switchboard -> Support.",
        )
    except Exception:  # noqa: BLE001
        log.warning("support_notify_failed", request_id=str(row.id), exc_info=True)


def to_dict(row: SupportRequest, org_name: str | None = None) -> dict:
    return {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "org_name": org_name,
        "email": row.email,
        "subject": row.subject,
        "body": row.body,
        "page": row.page,
        "status": row.status,
        "reply": row.reply,
        "replied_at": row.replied_at.isoformat() if row.replied_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def my_requests(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            sa.select(SupportRequest)
            .where(SupportRequest.org_id == org_id, SupportRequest.user_id == user_id)
            .order_by(SupportRequest.created_at.desc())
            .limit(50)
        )
    ).scalars()
    return [to_dict(r) for r in rows]


async def ops_list(session: AsyncSession, status: str | None) -> list[dict]:
    stmt = (
        sa.select(SupportRequest, Org.name)
        .join(Org, Org.id == SupportRequest.org_id)
        .order_by(SupportRequest.created_at.desc())
        .limit(200)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    if status:
        stmt = stmt.where(SupportRequest.status == status)
    return [to_dict(r, name) for r, name in (await session.execute(stmt)).all()]


async def ops_reply(
    session: AsyncSession, settings, request_id: uuid.UUID, *, reply: str,  # noqa: ANN001
    actor_user_id: uuid.UUID, close: bool,
) -> dict:
    reply = (reply or "").strip()
    if not reply:
        raise ValidationFailedError("Write a reply")
    row = (
        await session.execute(
            sa.select(SupportRequest)
            .where(SupportRequest.id == request_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Support request not found")
    set_org_context(session, row.org_id)
    row.reply = reply
    row.replied_by = actor_user_id
    row.replied_at = datetime.now(timezone.utc)
    row.status = "closed" if close else "answered"
    await session.commit()
    from app.services import mailer

    sent = await mailer.send(
        settings, [row.email], f"Re: {row.subject}", f"{reply}\n\n---\nYou wrote:\n{row.body}"
    )
    return {**to_dict(row), "emailed": bool(sent)}
