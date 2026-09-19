"""P41 suspension: the operator's off switch for a business.

Suspending takes effect immediately and everywhere:
  - status -> suspended, so the telephony gate refuses every new text, call and number
  - every signed-in session of every member is revoked (they may sign back in to see why)
  - every API key is revoked
  - scheduled texts are cancelled; running and scheduled campaigns are paused
  - live calls are hung up
  - optionally, the business's identifiers go on the ban list
  - audited with the operator, and the owners are emailed

Org.is_active is deliberately NOT used: it would lock the owners out of the console
entirely, including the page that explains the suspension.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, Call, Message, OrgMembership, OutboundCampaign, Role, User
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services import identity as identity_svc
from app.services import kyc as kyc_svc
from app.services import mailer, session_cache

log = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _owner_emails(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    rows = (
        (
            await session.execute(
                sa.select(User.email)
                .join(OrgMembership, OrgMembership.user_id == User.id)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id == org_id, Role.name == "owner")
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _hang_up_live_calls(session: AsyncSession, app, org_id: uuid.UUID) -> int:
    from app.services import calls as calls_svc
    from app.voice_plane import service as voice_service

    calls = (
        (
            await session.execute(
                sa.select(Call).where(
                    Call.org_id == org_id, Call.status.not_in(tuple(TERMINAL_CALL_STATUSES))
                )
            )
        )
        .scalars()
        .all()
    )
    ended = 0
    for call in calls:
        try:
            if (call.extra or {}).get("via") == "livekit":
                await voice_service.hangup_room_call(
                    session, getattr(app.state, "livekit", None), app.state.event_bus, call
                )
            else:
                await calls_svc.hangup_active_leg(session, app.state.carriers, call)
            ended += 1
        except Exception:  # noqa: BLE001 - one stuck call must not block the suspension
            log.warning("suspension_hangup_failed", call_id=str(call.id), exc_info=True)
    return ended


async def suspend(
    session: AsyncSession,
    app,
    org_id: uuid.UUID,
    *,
    operator_id: uuid.UUID,
    reason: str,
    ban: bool,
) -> dict:
    profile = await kyc_svc.load_for_operator(session, org_id)
    previous = profile.status
    kyc_svc.transition(profile, "suspended")
    profile.status_before_suspension = previous
    profile.suspended_at = _now()
    profile.suspended_by = operator_id
    profile.suspension_reason = reason.strip()

    member_ids = (
        (
            await session.execute(
                sa.select(OrgMembership.user_id).where(OrgMembership.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    revoked_sessions: list[uuid.UUID] = []
    for user_id in member_ids:
        revoked_sessions += await identity_svc.revoke_all_for_user(
            session, user_id, revoked_by=operator_id
        )

    keys = await session.execute(
        sa.update(ApiKey)
        .where(ApiKey.org_id == org_id, ApiKey.status == "active")
        .values(status="revoked")
        .execution_options(synchronize_session=False)
    )
    messages = await session.execute(
        sa.update(Message)
        .where(Message.org_id == org_id, Message.status == "scheduled")
        .values(
            status="rejected",
            error_code="account_suspended",
            error_detail="Account suspended",
            failure_reason_public="Not sent - this account is suspended.",
        )
        .execution_options(synchronize_session=False)
    )
    campaigns = await session.execute(
        sa.update(OutboundCampaign)
        .where(
            OutboundCampaign.org_id == org_id,
            OutboundCampaign.status.in_(("running", "scheduled")),
        )
        .values(status="paused")
        .execution_options(synchronize_session=False)
    )
    banned = await kyc_svc.ban_org_identifiers(session, profile, operator_id, reason) if ban else 0
    summary = {
        "sessions_revoked": len(revoked_sessions),
        "api_keys_revoked": keys.rowcount or 0,
        "scheduled_texts_cancelled": messages.rowcount or 0,
        "campaigns_paused": campaigns.rowcount or 0,
        "identifiers_banned": banned,
    }
    kyc_svc._audit_operator(
        session, profile, "kyc.suspended", operator_id, reason=reason[:500], **summary
    )
    await session.commit()

    for sid in revoked_sessions:
        await session_cache.mark_revoked(app.state.settings, sid)

    from app.db.base import set_org_context

    set_org_context(session, org_id)
    summary["calls_ended"] = await _hang_up_live_calls(session, app, org_id)
    await session.commit()

    await mailer.send(
        app.state.settings,
        await _owner_emails(session, org_id),
        f"Your {app.state.settings.app_name} account has been suspended",
        "Calling, texting and number orders for your business have been suspended "
        "following a compliance review.\n\n"
        f"Reason: {reason.strip()}\n\n"
        "You can still sign in to see your account. Reply to this email or contact support "
        "if you believe this is a mistake.",
    )
    set_org_context(session, org_id)
    return summary


async def unsuspend(
    session: AsyncSession, org_id: uuid.UUID, *, operator_id: uuid.UUID, note: str
) -> None:
    profile = await kyc_svc.load_for_operator(session, org_id)
    target = profile.status_before_suspension or "approved"
    if target not in ("approved", "reverification_due"):
        target = "approved"
    kyc_svc.transition(profile, target)
    profile.status_before_suspension = None
    profile.suspended_at = None
    profile.suspended_by = None
    profile.suspension_reason = None
    kyc_svc._audit_operator(session, profile, "kyc.unsuspended", operator_id, note=note[:500])
