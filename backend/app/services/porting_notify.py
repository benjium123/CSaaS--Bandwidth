"""P1: customer notifications for a port-in request.

Every status a workspace can see gets a bell entry; the ones that carry a date or need an
action also get an email. The workspace is only ever told about "the carrier" - never which
one we use, and never an operator-only note.

Callers own the transaction (this module never commits) and a lost notification must never
fail the port itself (this module never raises).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Notification, User
from app.models.porting import PortRequest
from app.services import billing_alerts, mailer, notifications

log = structlog.get_logger("porting_notify")

#: Only these carry an email; the rest are bell-only.
EMAIL_STATUSES = frozenset({"foc_confirmed", "exception", "ported", "rejected"})

#: Email subject tail per status (never a carrier name).
SUBJECTS = {
    "foc_confirmed": "port date confirmed",
    "exception": "your number transfer needs attention",
    "ported": "your numbers are live",
    "rejected": "transfer request not accepted",
}

#: A bell body is 255 characters, so at most this many numbers are named.
NUMBER_LIMIT = 3

#: Our carriers, never named to a customer.
_CARRIER_NAMES = re.compile(r"telnyx|signalwire", re.IGNORECASE)


def _scrub(text: str) -> str:
    """Never name the carrier we use, whatever the caller passed in."""
    return _CARRIER_NAMES.sub("the carrier", text or "")


def _numbers_text(port: PortRequest) -> str:
    numbers = [str(n) for n in (port.numbers or []) if n]
    if not numbers:
        return "your numbers"
    shown = ", ".join(numbers[:NUMBER_LIMIT])
    extra = len(numbers) - NUMBER_LIMIT
    if extra > 0:
        shown = f"{shown} and {extra} more"
    return shown


def _foc_text(port: PortRequest) -> str:
    """The carrier commits a date as a timestamp; the customer reads "Tue Oct 14"."""
    raw = str(port.foc_date or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return f"{parsed:%a %b} {parsed.day}"
    return "the date we agreed"


def message_for(
    settings, port: PortRequest, status: str, reason: str | None  # noqa: ANN001
) -> str | None:
    """The customer's sentence for a status, or None when the status is not announced."""
    numbers = _numbers_text(port)
    app_name = getattr(settings, "app_name", "") or "Ringlite"
    if status == "submitted":
        return f"Your request to move {numbers} was filed. We'll tell you when a date is set."
    if status == "foc_confirmed":
        return f"Your numbers move on {_foc_text(port)}. Keep your old service active until then."
    if status == "exception":
        why = (reason or "the carrier needs something corrected").strip()
        return (
            f"Your number transfer needs your attention: {why}. "
            "Open Numbers > Porting to fix and resubmit."
        )
    if status == "ported":
        body = f"{numbers} are now live on {app_name}."
        if reason:
            body = f"{body} {reason.strip()}"
        return body
    if status == "rejected":
        why = (reason or "it could not be approved").strip()
        return f"We couldn't accept your transfer request: {why}. You can fix it and resubmit."
    if status == "cancelled":
        return f"Your transfer request for {numbers} was cancelled."
    return None


async def _recipients(session: AsyncSession, port: PortRequest) -> list[tuple[uuid.UUID, str]]:
    """Who hears about it: whoever asked, plus the workspace owners/admins."""
    people: dict[uuid.UUID, str] = {}
    for user_id, email in await billing_alerts._recipients(session, port.org_id):
        people[user_id] = email or ""
    if port.submitted_by is not None and port.submitted_by not in people:
        email = (
            await session.execute(
                sa.select(User.email)
                .where(User.id == port.submitted_by)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        people[port.submitted_by] = email or ""
    return list(people.items())


async def notify(
    session: AsyncSession,
    settings,  # noqa: ANN001
    port: PortRequest,
    status: str,
    *,
    reason: str | None = None,
) -> int:
    """Bell row for ``status`` (plus an email where the status earns one). Never raises,
    never commits, returns the number of bells created."""
    try:
        if settings is None or port is None:
            return 0
        body = message_for(settings, port, status, reason)
        if body is None:
            return 0
        body = _scrub(body)[:255]
        # The event count makes a re-entered status (after a resubmit) notify again while
        # the same status, re-read by the sweeper, stays quiet.
        dedupe_key = f"porting:{port.id}:{status}:{len(port.events or [])}"[:128]
        recipients = await _recipients(session, port)
        set_org_context(session, port.org_id)
        created = 0
        for user_id, _email in recipients:
            row = await notifications.create(
                session,
                port.org_id,
                user_id=user_id,
                kind="porting",
                body=body,
                dedupe_key=dedupe_key,
            )
            if row is not None:
                created += 1
        if status in EMAIL_STATUSES:
            emails = sorted({email for _uid, email in recipients if email})
            if emails:
                base = (getattr(settings, "public_web_url", "") or "").rstrip("/")
                subject = f"{settings.app_name}: {SUBJECTS[status]}"
                text = f"{body}\n\nManage your numbers: {base}/settings/numbers"
                await mailer.send(settings, emails, subject, text)
        return created
    except Exception:
        log.exception(
            "porting_notify_failed",
            port_id=str(getattr(port, "id", "")),
            status=status,
        )
        return 0


__all__ = ["notify", "message_for", "EMAIL_STATUSES", "SUBJECTS", "Notification"]
