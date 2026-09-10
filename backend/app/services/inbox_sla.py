"""Inbox response targets.

Each inbox may set two targets: first reply within N minutes, or resolve within
N minutes. This file stamps the first reply, sweeps overdue conversations, and
reopens due snoozes. An inbox with neither target set is simply ignored by this
module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    Department,
    DepartmentMember,
    Inbox,
    InboxGrant,
    Message,
    MessageThread,
    Org,
    OrgNumber,
)
from app.services import notifications as notifications_svc

log = structlog.get_logger("inbox_sla")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class SlaState:
    due_at: datetime | None
    breached: bool


def sla_state_for(
    thread: MessageThread,
    inbox: Inbox | None,
    last_inbound_at: datetime | None,
    now: datetime,
) -> SlaState:
    """Compute due-at and breach state for one thread. Pure - no I/O."""
    now = _aware(now) or datetime.now(timezone.utc)

    if inbox is None or (
        inbox.sla_first_response_minutes is None and inbox.sla_resolution_minutes is None
    ):
        return SlaState(due_at=None, breached=bool(thread.sla_breached_at))

    first_response_at = _aware(thread.first_response_at)
    last_inbound_at = _aware(last_inbound_at)
    thread_created_at = _aware(thread.created_at)

    due_at: datetime | None = None
    if (
        first_response_at is None
        and inbox.sla_first_response_minutes is not None
        and last_inbound_at is not None
    ):
        due_at = last_inbound_at + timedelta(minutes=inbox.sla_first_response_minutes)
    elif (
        inbox.sla_resolution_minutes is not None
        and thread_created_at is not None
        and thread.status != "closed"
    ):
        due_at = thread_created_at + timedelta(minutes=inbox.sla_resolution_minutes)

    breached = thread.sla_breached_at is not None or (due_at is not None and now > due_at)
    return SlaState(due_at=due_at, breached=breached)


async def last_inbound_at_by_thread(
    session: AsyncSession,
    thread_ids: list[uuid.UUID] | set[uuid.UUID],
) -> dict[uuid.UUID, datetime]:
    """Return the most recent inbound message time per thread in one grouped query."""
    if not thread_ids:
        return {}

    rows = (
        await session.execute(
            sa.select(Message.thread_id, sa.func.max(Message.created_at))
            .where(
                Message.thread_id.in_(list(thread_ids)),
                Message.direction == "inbound",
            )
            .group_by(Message.thread_id)
        )
    ).all()
    return dict(rows)


async def inbox_by_e164(session: AsyncSession, e164s: list[str] | set[str]) -> dict[str, Inbox]:
    """Return inboxes keyed by number, in one query. Empty input -> empty dict."""
    if not e164s:
        return {}

    rows = (
        await session.execute(
            sa.select(OrgNumber.e164, Inbox)
            .join(Inbox, Inbox.number_id == OrgNumber.id)
            .where(OrgNumber.e164.in_(list(e164s)))
        )
    ).all()
    return dict(rows)


async def sla_for_threads(
    session: AsyncSession,
    threads: list[MessageThread],
) -> dict[uuid.UUID, SlaState]:
    """Compute SLA state for a batch of threads.

    This always performs two queries regardless of how many threads are passed:
    one grouped inbound-time lookup and one inbox lookup. Threads whose inbox has
    no target are simply omitted from the result.
    """
    if not threads:
        return {}

    thread_ids = [thread.id for thread in threads]
    e164s = list({thread.our_e164 for thread in threads})

    last_inbound = await last_inbound_at_by_thread(session, thread_ids)
    inboxes = await inbox_by_e164(session, e164s)
    now = datetime.now(timezone.utc)

    result: dict[uuid.UUID, SlaState] = {}
    for thread in threads:
        inbox = inboxes.get(thread.our_e164)
        if inbox is None:
            continue
        if inbox.sla_first_response_minutes is None and inbox.sla_resolution_minutes is None:
            continue
        result[thread.id] = sla_state_for(thread, inbox, last_inbound.get(thread.id), now)
    return result


async def stamp_first_response(session: AsyncSession, thread: MessageThread) -> bool:
    """Stamp the first reply when it exists. Does not commit.

    Cold outreach (outbound message first) has nothing to respond to and is never
    stamped.
    """
    if thread.first_response_at is not None:
        return False

    has_inbound = (
        await session.execute(
            sa.select(Message.id)
            .where(
                Message.thread_id == thread.id,
                Message.direction == "inbound",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_inbound is None:
        return False

    thread.first_response_at = datetime.now(timezone.utc)
    return True


async def leads_for_inbox(session: AsyncSession, inbox_id: uuid.UUID) -> set[uuid.UUID]:
    """Return department leads for every active department that can use this inbox."""
    rows = (
        await session.execute(
            sa.select(DepartmentMember.user_id)
            .join(Department, Department.id == DepartmentMember.department_id)
            .join(InboxGrant, InboxGrant.grantee_id == DepartmentMember.department_id)
            .where(
                InboxGrant.inbox_id == inbox_id,
                InboxGrant.grantee_type == "department",
                DepartmentMember.is_lead.is_(True),
                Department.is_active.is_(True),
            )
        )
    ).scalars().all()
    return set(rows)


async def reopen_due_snoozes(session: AsyncSession, now: datetime | None = None) -> int:
    """Sweep all orgs and return due snoozes to the open list.

    A due snooze simply reappears in the list; deliberately no notification is
    created for it.
    """
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    now = _aware(now) or datetime.now(timezone.utc)
    reopened = 0

    for org_id in org_ids:
        set_org_context(session, org_id)
        try:
            threads = (
                (
                    await session.execute(
                        sa.select(MessageThread)
                        .where(
                            MessageThread.snoozed_until.is_not(None),
                            MessageThread.snoozed_until <= now,
                        )
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            for thread in threads:
                thread.snoozed_until = None
                thread.status = "open"
                await session.commit()
                reopened += 1
        except Exception:
            await session.rollback()
            log.exception("reopen_due_snoozes_org_failed", org_id=str(org_id))
            continue

    return reopened


async def sla_tick(session: AsyncSession, bus: object | None = None) -> dict[str, int]:
    """Sweep all orgs and mark each inbox conversation that has become overdue."""
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    result = {"orgs": len(org_ids), "breached": 0, "notifications": 0}

    for org_id in org_ids:
        set_org_context(session, org_id)
        try:
            e164s = list((await session.execute(sa.select(OrgNumber.e164))).scalars().all())
            inboxes = await inbox_by_e164(session, e164s)
            active_inboxes = {
                e164: inbox
                for e164, inbox in inboxes.items()
                if inbox.sla_first_response_minutes is not None
                or inbox.sla_resolution_minutes is not None
            }

            # The overwhelmingly common case is an org with no inbox targets at
            # all - skip it without loading threads or messages.
            if not active_inboxes:
                continue

            threads = (
                (
                    await session.execute(
                        sa.select(MessageThread)
                        .where(
                            MessageThread.sla_breached_at.is_(None),
                            MessageThread.status != "closed",
                            # Only conversations on a number whose inbox actually has a
                            # target. Without this the 500-row window can fill entirely
                            # with threads from untargeted inboxes and never reach the
                            # ones this tick exists to check.
                            MessageThread.our_e164.in_(list(active_inboxes)),
                        )
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )

            last_inbound = await last_inbound_at_by_thread(
                session, [thread.id for thread in threads]
            )
            leads_cache: dict[uuid.UUID, set[uuid.UUID]] = {}

            for thread in threads:
                inbox = active_inboxes.get(thread.our_e164)
                if inbox is None:
                    continue

                state = sla_state_for(thread, inbox, last_inbound.get(thread.id), now)
                if not state.breached:
                    continue

                thread.sla_breached_at = now
                rule = (
                    "first"
                    if thread.first_response_at is None
                    and inbox.sla_first_response_minutes is not None
                    else "resolution"
                )

                if inbox.id not in leads_cache:
                    leads_cache[inbox.id] = await leads_for_inbox(session, inbox.id)
                recipients = set(leads_cache[inbox.id])
                if thread.assigned_user_id is not None:
                    recipients.add(thread.assigned_user_id)

                created = await notifications_svc.notify_overdue(
                    session,
                    org_id,
                    thread_id=thread.id,
                    user_ids=recipients,
                    rule=rule,
                    bus=bus,
                )
                await session.commit()

                result["breached"] += 1
                result["notifications"] += created
        except Exception:
            await session.rollback()
            log.exception("sla_tick_org_failed", org_id=str(org_id))
            continue

    # Because sla_breached_at is only ever set once and notify_overdue carries the
    # dedupe key, re-running this tick can never mark or notify the same thread
    # twice.
    return result
