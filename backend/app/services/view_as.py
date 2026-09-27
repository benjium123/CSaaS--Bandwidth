"""H3 view as workspace: a platform operator looks at a customer workspace, read-only.

Started from the ops console with a reason and a fresh second factor (routes/ops_view_as.py,
guarded ``ops:support`` major). The grant lasts VIEW_AS_TTL. The operator keeps their own
sign-in; requests carrying ``X-View-As: <grant id>`` resolve to a read-only OrgContext for
that workspace (auth/deps.py get_current_org -> ``org_context``), every one of them - reads
included - is written to operator_audit_log, and any request that is not a read is refused.
The owners are emailed when a view starts and see it in their account activity.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import PermissionDeniedError
from app.models import OperatorViewSession, Org, OrgMembership, Role, User
from app.models.rbac import PERMISSIONS

HEADER = "X-View-As"
VIEW_AS_TTL = timedelta(minutes=30)
#: What the viewer's transient role grants: every read, plus the two "see everything"
#: permissions so the view is not narrowed by inbox grants or contact visibility.
#: Writes are refused by method regardless (org_context), so these never change anything.
VIEW_AS_PERMISSIONS: list[str] = [
    p for p in PERMISSIONS if p.endswith(":read") or p in ("contacts:read_all", "inboxes:admin")
]
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_pending_emails: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_live(row: OperatorViewSession, now: datetime | None = None) -> bool:
    now = now or _now()
    return row.ended_at is None and (_aware(row.expires_at) or now) > now


async def owners_of(session: AsyncSession, org_id: uuid.UUID) -> list[User]:
    # JUSTIFIED allow_unscoped: the operator has no membership in this workspace.
    return list(
        (
            await session.execute(
                sa.select(User)
                .join(OrgMembership, OrgMembership.user_id == User.id)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id == org_id, Role.name == "owner")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


async def start(
    session: AsyncSession,
    settings: Settings,
    *,
    operator: User,
    org: Org,
    reason: str,
    request: Request | None = None,
) -> OperatorViewSession:
    """Open a grant, end any other live grant this operator holds, tell the owners.
    The caller commits."""
    from app.services import account_security, mailer

    now = _now()
    for old in (
        await session.execute(
            sa.select(OperatorViewSession).where(
                OperatorViewSession.operator_user_id == operator.id,
                OperatorViewSession.ended_at.is_(None),
            )
        )
    ).scalars():
        old.ended_at = now
    row = OperatorViewSession(
        id=uuid.uuid4(),
        operator_user_id=operator.id,
        org_id=org.id,
        reason=reason,
        started_at=now,
        expires_at=now + VIEW_AS_TTL,
    )
    session.add(row)

    owners = await owners_of(session, org.id)
    for owner in owners:
        account_security.audit(
            session,
            owner.id,
            "support.viewed_workspace",
            actor_user_id=operator.id,
            request=request,
            detail={"org_id": str(org.id), "workspace": org.name, "reason": reason},
        )
    recipients = sorted({o.email for o in owners if o.email})
    if recipients:
        minutes = int(VIEW_AS_TTL.total_seconds() // 60)
        body = (
            f"{settings.app_name} support opened a read-only view of your workspace "
            f"\"{org.name}\" for up to {minutes} minutes.\n\n"
            f"Reason given: {reason}\n\n"
            "Support can see your workspace during this time but cannot send, change or "
            "delete anything. Every page they open is logged. If you did not expect this, "
            "reply to this email or contact support."
        )
        task = asyncio.create_task(
            mailer.send(
                settings, recipients, f"{settings.app_name} support viewed your workspace", body
            )
        )
        _pending_emails.add(task)
        task.add_done_callback(_pending_emails.discard)
        if settings.app_env == "test":
            await task
    return row


async def org_context(
    request: Request,
    session: AsyncSession,
    user: User,
    org_id: uuid.UUID,
    grant_id: str,
):
    """The read-only OrgContext for a live view, or 403. Called by get_current_org."""
    from app.auth.deps import OrgContext
    from app.services import operator_audit
    from app.services import operators as operators_svc

    ended = PermissionDeniedError(
        "This support view has ended. Start a new one from the Switchboard.",
        code="view_as_ended",
    )
    try:
        gid = uuid.UUID(grant_id)
    except ValueError as exc:
        raise ended from exc
    row = await session.get(OperatorViewSession, gid)
    if (
        row is None
        or row.operator_user_id != user.id
        or row.org_id != org_id
        or not is_live(row)
    ):
        raise ended
    operator = await operators_svc.get_active(session, user.id)
    if operator is None or not operators_svc.has_permission(operator.role, "ops:support"):
        raise ended
    # Tag first, so refused writes are in the audit log too.
    operator_audit.tag(
        request,
        user_id=user.id,
        email=user.email,
        role=operator.role,
        log_reads=True,
        org_id=org_id,
    )
    if request.method not in READ_METHODS:
        raise PermissionDeniedError(
            "A support view is read-only. Nothing can be sent, changed or deleted.",
            code="view_as_read_only",
        )
    org = await session.get(Org, org_id)
    if org is None:
        raise ended
    set_org_context(session, org.id)
    role = Role(
        id=uuid.uuid4(), org_id=org.id, name="support-view", permissions=VIEW_AS_PERMISSIONS
    )
    return OrgContext(org=org, membership=None, role=role, session=session)
