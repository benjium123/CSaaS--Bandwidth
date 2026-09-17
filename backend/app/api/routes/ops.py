"""P41 operator console API: review queue, application decisions, security alerts, ban list,
suspension.

Named operators only (auth/deps.py require_operator) - the shared ops token is refused.
Reviewers read and decide applications; admins also suspend, unsuspend and edit the ban
list, and those destructive actions need a second factor proven in the last few minutes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.auth.deps import OperatorContext, check_step_up, require_operator
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import NotFoundError, ValidationFailedError
from app.models import (
    FRAUD_IDENTIFIER_KINDS,
    KYC_STATUSES,
    FraudIdentifier,
    KycCheck,
    KycDocument,
    KycProfile,
    Org,
    SecurityAlert,
    User,
)
from app.services import ban_list, kyc_checks, kyc_documents, suspension
from app.services import kyc as kyc_svc

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

Reviewer = Annotated[OperatorContext, Depends(require_operator("reviewer"))]
Admin = Annotated[OperatorContext, Depends(require_operator("admin"))]

QUEUE_STATUSES = ("submitted", "in_review", "needs_info", "reverification_due", "suspended")


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    ban: bool = False


class RegistryIn(BaseModel):
    result: str
    link: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=1000)


class LimitsIn(BaseModel):
    deposit_required_cents: int | None = None
    limits: dict | None = None


class SuspendIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    ban: bool = False


class BanIn(BaseModel):
    kind: str
    value: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=1, max_length=500)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------------------
@router.get("/queue")
async def queue(
    op: Reviewer,
    status: str | None = Query(default=None),
    risk: str | None = Query(default=None),
) -> dict:
    if status is not None and status not in KYC_STATUSES:
        raise ValidationFailedError(f"Unknown status: {status}")
    statuses = (status,) if status else QUEUE_STATUSES
    # JUSTIFIED allow_unscoped: the operator queue lists applications across every org.
    stmt = (
        sa.select(KycProfile, Org.name)
        .join(Org, Org.id == KycProfile.org_id)
        .where(KycProfile.status.in_(statuses))
        .order_by(KycProfile.submitted_at.asc().nulls_last(), KycProfile.created_at.asc())
        .limit(200)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    if risk:
        stmt = stmt.where(KycProfile.risk_tier == risk)
    rows = (await op.session.execute(stmt)).all()
    open_alerts = (
        await op.session.execute(
            sa.select(sa.func.count(SecurityAlert.id)).where(SecurityAlert.status == "open")
        )
    ).scalar_one()
    return {
        "applications": [
            {
                "org_id": str(p.org_id),
                "org_name": name,
                "legal_name": p.legal_name,
                "country": p.country,
                "status": p.status,
                "risk_tier": p.risk_tier,
                "risk_reasons": p.risk_reasons or [],
                "video_call_required": p.video_call_required,
                "video_call_done": p.video_call_done_at is not None,
                "use_case_change_pending": p.use_case_pending is not None,
                "submitted_at": _iso(p.submitted_at),
            }
            for p, name in rows
        ],
        "open_security_alerts": open_alerts,
    }


@router.get("/applications/{org_id}")
async def application(org_id: uuid.UUID, op: Reviewer) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    org = await op.session.get(Org, org_id)
    persons = await kyc_checks.persons_for(op.session, org_id)
    documents = (
        (
            await op.session.execute(
                sa.select(KycDocument)
                .where(KycDocument.org_id == org_id)
                .order_by(KycDocument.created_at)
            )
        )
        .scalars()
        .all()
    )
    checks = (
        (
            await op.session.execute(
                sa.select(KycCheck)
                .where(KycCheck.org_id == org_id)
                .order_by(KycCheck.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, KycCheck] = {}
    for c in checks:
        latest.setdefault(c.kind, c)
    return {
        "org": {"id": str(org.id), "name": org.name, "slug": org.slug},
        "status": profile.status,
        "business": {
            "country": profile.country,
            "legal_name": profile.legal_name,
            "dba_name": profile.dba_name,
            "entity_type": profile.entity_type,
            "registration_number": profile.registration_number,
            "tax_id": profile.tax_id,
            "incorporation_date": (
                profile.incorporation_date.isoformat() if profile.incorporation_date else None
            ),
            "registered_address": profile.registered_address,
            "operating_address": profile.operating_address,
            "website": profile.website,
            "business_email": profile.business_email,
            "business_phone": profile.business_phone,
        },
        "use_case": profile.use_case,
        "use_case_pending": profile.use_case_pending,
        "risk": {
            "tier": profile.risk_tier,
            "reasons": profile.risk_reasons or [],
            "submitted_from_flagged_login": profile.submitted_from_flagged_login,
            "video_call_required": profile.video_call_required,
            "video_call_done_at": _iso(profile.video_call_done_at),
            "video_call_note": profile.video_call_note,
        },
        "persons": [
            {
                "id": str(p.id),
                "role": p.role,
                "full_name": p.full_name,
                "email": p.email,
                "ownership_percent": p.ownership_percent,
                "status": p.status,
                "verified_name": p.verified_name,
                "document_type": p.document_type,
                "document_country": p.document_country,
                "verified_at": _iso(p.verified_at),
                "last_error": p.last_error,
                "residential_address": p.residential_address,
            }
            for p in persons
        ],
        "documents": [
            {
                "id": str(d.id),
                "kind": d.kind,
                "filename": d.filename,
                "content_type": d.content_type,
                "size_bytes": d.size_bytes,
                "sha256": d.sha256,
                "uploaded_at": _iso(d.created_at),
                "person_id": str(d.person_id) if d.person_id else None,
                "review_result": d.review_result,
                "review": d.review,
                "reviewed_at": _iso(d.reviewed_at),
            }
            for d in documents
        ],
        "checks": {
            k: {
                "result": c.result,
                "summary": c.summary,
                "detail": c.detail,
                "at": _iso(c.created_at),
                "manual": c.created_by is not None,
            }
            for k, c in latest.items()
        },
        "approval_blockers": await kyc_svc.approval_blockers(op.session, profile),
        "agreement": {
            "version": profile.agreement_version,
            "accepted_at": _iso(profile.agreement_accepted_at),
            "ip": profile.agreement_ip,
        },
        "deposit_required_cents": profile.deposit_required_cents,
        "limits": profile.limits,
        "submitted_at": _iso(profile.submitted_at),
        "decided_at": _iso(profile.decided_at),
        "decision_reason": profile.decision_reason,
        "info_request": profile.info_request,
        "suspended_at": _iso(profile.suspended_at),
        "suspension_reason": profile.suspension_reason,
        "next_reverification_at": _iso(profile.next_reverification_at),
    }


@router.get("/applications/{org_id}/documents/{document_id}")
async def download_document(
    org_id: uuid.UUID, document_id: uuid.UUID, request: Request, op: Reviewer
) -> Response:
    await kyc_svc.load_for_operator(op.session, org_id)
    doc = await kyc_documents.get(op.session, org_id, document_id)
    data = await kyc_documents.read(request.app.state.settings, request.app.state.media_store, doc)
    from app.services import audit as audit_svc

    audit_svc.record(
        op.session,
        org_id,
        action="kyc.document_viewed",
        target_type="kyc_document",
        target_id=str(doc.id),
        actor_user_id=op.user.id,
        detail={"operator_user_id": str(op.user.id)},
    )
    await op.session.commit()
    return Response(
        content=data,
        media_type=doc.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{doc.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# --------------------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------------------
async def _done(op: OperatorContext, org_id: uuid.UUID) -> dict:
    await op.session.commit()
    set_org_context(op.session, org_id)
    return await application(org_id, op)


DECISION_EMAILS = {
    "approved": (
        "Your business is verified",
        "Good news - your business passed verification. Calling, texting and phone numbers "
        "are now available in your workspace.",
    ),
    "needs_info": (
        "We need a little more information to verify your business",
        "Our reviewer needs something else before approving your business:\n\n{message}\n\n"
        "Open Settings > Business verification to respond.",
    ),
    "rejected": (
        "Your business could not be verified",
        "We were unable to verify your business, so calling and texting are not available.\n\n"
        "Reason: {message}\n\nReply to this email if you believe this is a mistake.",
    ),
}


async def _email_decision(
    request: Request, op: OperatorContext, org_id: uuid.UUID, status: str, message: str = ""
) -> None:
    template = DECISION_EMAILS.get(status)
    if template is None:
        return
    from app.services import mailer

    settings = request.app.state.settings
    recipients = await suspension._owner_emails(op.session, org_id)
    subject, body = template
    await mailer.send(
        settings, recipients, f"{settings.app_name}: {subject}", body.format(message=message)
    )
    set_org_context(op.session, org_id)


@router.post("/applications/{org_id}/review")
async def start_review(org_id: uuid.UUID, op: Reviewer) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.start_review(op.session, profile, op.user.id)
    return await _done(op, org_id)


@router.post("/applications/{org_id}/request-info")
async def request_info(
    org_id: uuid.UUID, payload: MessageIn, request: Request, op: Reviewer
) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.request_info(op.session, profile, op.user.id, payload.message)
    out = await _done(op, org_id)
    await _email_decision(request, op, org_id, "needs_info", payload.message)
    return out


@router.post("/applications/{org_id}/approve")
async def approve(org_id: uuid.UUID, payload: NoteIn, request: Request, op: Reviewer) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.approve(op.session, request.app.state.settings, profile, op.user.id, payload.note)
    out = await _done(op, org_id)
    await _email_decision(request, op, org_id, "approved")
    return out


@router.post("/applications/{org_id}/reject")
async def reject(org_id: uuid.UUID, payload: RejectIn, request: Request, op: Reviewer) -> dict:
    if payload.ban:
        await check_step_up(request, op.session, op.user, kind="recent_2fa", action="ban")
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.reject(op.session, profile, op.user.id, payload.reason, ban=payload.ban)
    out = await _done(op, org_id)
    await _email_decision(request, op, org_id, "rejected", payload.reason)
    return out


@router.post("/applications/{org_id}/video-call")
async def video_call(org_id: uuid.UUID, payload: NoteIn, op: Reviewer) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.record_video_call(op.session, profile, op.user.id, payload.note)
    return await _done(op, org_id)


@router.post("/applications/{org_id}/registry")
async def registry(org_id: uuid.UUID, payload: RegistryIn, request: Request, op: Reviewer) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.record_manual_registry(
        op.session,
        profile,
        op.user.id,
        result=payload.result,
        link=payload.link,
        note=payload.note,
    )
    await op.session.flush()
    await kyc_svc.refresh_risk(op.session, request.app.state.settings, profile)
    return await _done(op, org_id)


@router.post("/applications/{org_id}/rerun-checks")
async def rerun_checks(org_id: uuid.UUID, request: Request, op: Reviewer) -> dict:
    settings = request.app.state.settings
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    client = getattr(request.app.state, "kyc_http_client", None)
    await kyc_checks.run_all(op.session, settings, profile, client=client)
    await op.session.flush()
    await kyc_svc.refresh_risk(op.session, settings, profile)
    await op.session.commit()
    # P43: re-read documents that errored, roll up, and refresh the AI decision pack.
    from app.services import kyc_automation

    await kyc_automation.process(
        op.session,
        settings,
        request.app.state.media_store,
        org_id,
        http_client=client,
        force_reviews=True,
    )
    set_org_context(op.session, org_id)
    return await _done(op, org_id)


@router.post("/applications/{org_id}/limits")
async def set_limits(org_id: uuid.UUID, payload: LimitsIn, op: Reviewer) -> dict:
    profile = await kyc_svc.load_for_operator(op.session, org_id)
    await kyc_svc.set_limits(
        op.session,
        profile,
        op.user.id,
        deposit_required_cents=payload.deposit_required_cents,
        limits=payload.limits,
    )
    return await _done(op, org_id)


@router.post("/applications/{org_id}/suspend")
async def suspend(org_id: uuid.UUID, payload: SuspendIn, request: Request, op: Admin) -> dict:
    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="suspend")
    await suspension.suspend(
        op.session,
        request.app,
        org_id,
        operator_id=op.user.id,
        reason=payload.reason,
        ban=payload.ban,
    )
    return await _done(op, org_id)


@router.post("/applications/{org_id}/unsuspend")
async def unsuspend(org_id: uuid.UUID, payload: NoteIn, request: Request, op: Admin) -> dict:
    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="unsuspend")
    await suspension.unsuspend(op.session, org_id, operator_id=op.user.id, note=payload.note)
    return await _done(op, org_id)


# --------------------------------------------------------------------------------------
# Security alerts
# --------------------------------------------------------------------------------------
@router.get("/alerts")
async def alerts(op: Reviewer, status: str = Query(default="open")) -> list[dict]:
    rows = (
        (
            await op.session.execute(
                sa.select(SecurityAlert)
                .where(SecurityAlert.status == status)
                .order_by(SecurityAlert.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(a.id),
            "kind": a.kind,
            "user_id": str(a.user_id) if a.user_id else None,
            "status": a.status,
            "detail": a.detail,
            "created_at": _iso(a.created_at),
            "review_note": a.review_note,
        }
        for a in rows
    ]


@router.post("/alerts/{alert_id}/review")
async def review_alert(alert_id: uuid.UUID, payload: NoteIn, op: Reviewer) -> dict:
    row = await op.session.get(SecurityAlert, alert_id)
    if row is None:
        raise NotFoundError("Alert not found")
    row.status = "reviewed"
    row.reviewed_by = op.user.id
    row.reviewed_at = datetime.now(timezone.utc)
    row.review_note = payload.note[:500] or None
    await op.session.commit()
    return {"id": str(row.id), "status": row.status}


# --------------------------------------------------------------------------------------
# Ban list
# --------------------------------------------------------------------------------------
@router.get("/ban-list")
async def list_bans(op: Reviewer) -> list[dict]:
    rows = (
        await op.session.execute(
            sa.select(FraudIdentifier, User.email)
            .outerjoin(User, User.id == FraudIdentifier.created_by)
            .where(FraudIdentifier.is_active.is_(True))
            .order_by(FraudIdentifier.created_at.desc())
            .limit(500)
        )
    ).all()
    return [
        {
            "id": str(f.id),
            "kind": f.kind,
            "hint": f.display_hint,
            "reason": f.reason,
            "source_org_id": str(f.source_org_id) if f.source_org_id else None,
            "added_by": email,
            "added_at": _iso(f.created_at),
        }
        for f, email in rows
    ]


@router.post("/ban-list", status_code=201)
async def add_ban(payload: BanIn, request: Request, op: Admin) -> dict:
    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="ban")
    if payload.kind not in FRAUD_IDENTIFIER_KINDS or payload.kind in ("person", "device"):
        raise ValidationFailedError("That kind of identifier is added from an application")
    row = await ban_list.add(
        op.session,
        kind=payload.kind,
        value=payload.value,
        reason=payload.reason,
        created_by=op.user.id,
    )
    if row is None:
        raise ValidationFailedError("That value is empty after normalising")
    await op.session.commit()
    return {"id": str(row.id), "kind": row.kind, "hint": row.display_hint}


@router.delete("/ban-list/{identifier_id}", status_code=204)
async def remove_ban(identifier_id: uuid.UUID, request: Request, op: Admin) -> Response:
    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="ban")
    row = await op.session.get(FraudIdentifier, identifier_id)
    if row is None:
        raise NotFoundError("Not on the ban list")
    row.is_active = False
    await op.session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# P42 account support: find, unlock, reset sign-in methods, deactivate
# --------------------------------------------------------------------------------------
class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.get("/users")
async def find_user(op: Reviewer, email: str = Query(min_length=3)) -> dict:
    from app.repositories import users as users_repo
    from app.services import lockout, recovery_codes

    user = await users_repo.get_by_email(op.session, email)
    if user is None:
        raise NotFoundError("No account with that email")
    until = await lockout.locked_until(op.session, user.id)
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "totp_enabled": user.totp_enabled,
        "has_passkey": user.has_passkey,
        "recovery_codes_remaining": await recovery_codes.remaining(op.session, user.id),
        "locked_until": _iso(until),
        "step_up_blocked_until": _iso(user.step_up_blocked_until),
    }


async def _target_user(op: OperatorContext, user_id: uuid.UUID) -> User:
    user = await op.session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if user.id == op.user.id:
        raise ValidationFailedError("Operators cannot change their own account here")
    return user


@router.post("/users/{user_id}/unlock", status_code=204)
async def unlock_user(user_id: uuid.UUID, request: Request, op: Admin) -> Response:
    from app.services import lockout

    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="user_support")
    user = await _target_user(op, user_id)
    await lockout.unlock(op.session, user.id, actor_user_id=op.user.id, request=request)
    await op.session.commit()
    return Response(status_code=204)


@router.post("/users/{user_id}/reset-2fa", status_code=204)
async def operator_reset_factors(
    user_id: uuid.UUID, payload: ReasonIn, request: Request, op: Admin
) -> Response:
    """Last resort, after verifying the person out of band (e.g. a video call with ID)."""
    from app.services import account_security

    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="user_support")
    settings = request.app.state.settings
    user = await _target_user(op, user_id)
    cleared = await account_security.clear_second_factors(op.session, user)
    user.step_up_blocked_until = datetime.now(timezone.utc) + timedelta(
        hours=settings.recovery_cooldown_hours
    )
    revoked = await account_security.revoke_sessions(
        op.session, settings, user.id, revoked_by=op.user.id
    )
    account_security.audit(
        op.session,
        user.id,
        "factors.reset_by_operator",
        actor_user_id=op.user.id,
        request=request,
        detail={**cleared, "reason": payload.reason},
    )
    await op.session.commit()
    await account_security.mark_revoked(settings, revoked)
    await account_security.notify_now(
        settings,
        user.email,
        "Your sign-in methods were reset by support",
        "Our support team reset your passkeys and authenticator app. Sign in with your "
        "password and set up a new passkey.",
    )
    return Response(status_code=204)


@router.post("/users/{user_id}/deactivate", status_code=204)
async def deactivate_user(
    user_id: uuid.UUID, payload: ReasonIn, request: Request, op: Admin
) -> Response:
    from app.services import account_security

    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="user_support")
    settings = request.app.state.settings
    user = await _target_user(op, user_id)
    user.is_active = False
    revoked = await account_security.revoke_sessions(
        op.session, settings, user.id, revoked_by=op.user.id
    )
    account_security.audit(
        op.session,
        user.id,
        "account.deactivated",
        actor_user_id=op.user.id,
        request=request,
        detail={"reason": payload.reason},
    )
    await op.session.commit()
    await account_security.mark_revoked(settings, revoked)
    return Response(status_code=204)


@router.post("/users/{user_id}/reactivate", status_code=204)
async def reactivate_user(
    user_id: uuid.UUID, payload: ReasonIn, request: Request, op: Admin
) -> Response:
    from app.services import account_security

    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="user_support")
    user = await _target_user(op, user_id)
    user.is_active = True
    account_security.audit(
        op.session,
        user.id,
        "account.reactivated",
        actor_user_id=op.user.id,
        request=request,
        detail={"reason": payload.reason},
    )
    await op.session.commit()
    return Response(status_code=204)
