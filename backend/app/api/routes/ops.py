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
from app.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
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


@router.get("/number-purchases")
async def number_purchase_queue(op: Reviewer) -> list[dict]:
    from app.models import NumberPurchase
    from app.services import number_purchases

    # JUSTIFIED: named operators reconcile provisioning across customer workspaces.
    rows = (
        await op.session.execute(
            sa.select(NumberPurchase)
            .order_by(NumberPurchase.created_at.desc())
            .limit(100)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars()
    return [
        {
            **number_purchases.public(row),
            "org_id": str(row.org_id),
            "subscription_id": row.subscription_id,
            "subscription_status": row.subscription_status,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


async def _purchase_org(op: OperatorContext, purchase_id: uuid.UUID) -> uuid.UUID:
    from app.models import NumberPurchase

    org_id = (
        await op.session.execute(
            sa.select(NumberPurchase.org_id)
            .where(NumberPurchase.id == purchase_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if org_id is None:
        raise NotFoundError("Purchase not found")
    set_org_context(op.session, org_id)
    return org_id


def _audit_purchase(op: OperatorContext, org_id, purchase_id, action: str, detail: dict) -> None:
    from app.services import audit as audit_svc

    audit_svc.record(
        op.session,
        org_id,
        action=action,
        target_type="number_purchase",
        target_id=str(purchase_id),
        actor_user_id=op.user.id,
        detail={"operator_user_id": str(op.user.id), **detail},
    )


@router.post("/number-purchases/{purchase_id}/retry")
async def retry_number_purchase(purchase_id: uuid.UUID, request: Request, op: Admin) -> dict:
    """Finish provisioning a paid purchase that stalled. Looks each missing number up on
    the Telnyx account before ordering, so a timed-out order is never placed twice."""
    from app.services import number_purchases

    org_id = await _purchase_org(op, purchase_id)
    purchase, failures = await number_purchases.retry(op.session, request, purchase_id)
    _audit_purchase(op, org_id, purchase_id, "number_purchase.retried", {"failures": failures})
    await op.session.commit()
    return {**number_purchases.public(purchase), "failures": failures}


@router.post("/number-purchases/{purchase_id}/refund")
async def refund_number_purchase(purchase_id: uuid.UUID, request: Request, op: Admin) -> dict:
    """Stop billing for and refund the numbers of a paid purchase that were never
    provisioned. Provisioned numbers are kept and stay billed."""
    from app.services import number_purchases

    org_id = await _purchase_org(op, purchase_id)
    purchase = await number_purchases.refund_unprovisioned(
        op.session, request.app.state.settings, purchase_id
    )
    _audit_purchase(
        op, org_id, purchase_id, "number_purchase.refunded", {"detail": purchase.detail}
    )
    await op.session.commit()
    return number_purchases.public(purchase)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)
    manual_override: bool = False


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
        sa.select(KycProfile, Org.name, Org.account_type)
        .join(Org, Org.id == KycProfile.org_id)
        .where(KycProfile.status.in_(statuses))
        .order_by(KycProfile.submitted_at.asc().nulls_last(), KycProfile.created_at.asc())
        .limit(200)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    if risk:
        stmt = stmt.where(KycProfile.risk_tier == risk)
    rows = (await op.session.execute(stmt)).all()
    # P43: the AI's recommendation next to each application, so the queue can be cleared
    # from the list. One query for the latest decision pack of every listed business.
    packs: dict[uuid.UUID, dict] = {}
    org_ids = [p.org_id for p, _name, _account_type in rows]
    if org_ids:
        pack_rows = (
            (
                await op.session.execute(
                    sa.select(KycCheck)
                    .where(KycCheck.org_id.in_(org_ids), KycCheck.kind == "ai_decision")
                    .order_by(KycCheck.created_at.desc())
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            )
            .scalars()
            .all()
        )
        for check in pack_rows:
            detail = check.detail or {}
            if check.org_id not in packs and detail.get("recommendation"):
                packs[check.org_id] = detail
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
                "account_type": account_type or "business",
                "status": p.status,
                "risk_tier": p.risk_tier,
                "risk_reasons": p.risk_reasons or [],
                "video_call_required": p.video_call_required,
                "video_call_done": p.video_call_done_at is not None,
                "use_case_change_pending": p.use_case_pending is not None,
                "submitted_at": _iso(p.submitted_at),
                "ai_recommendation": packs.get(p.org_id, {}).get("recommendation"),
                "ai_confidence": packs.get(p.org_id, {}).get("confidence"),
            }
            for p, name, account_type in rows
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
        "account_type": org.account_type or "business",
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
                "identity_provider": getattr(p, "identity_provider", None),
                "provider_session_id": p.provider_session_id,
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


@router.get("/applications/{org_id}/persons/{person_id}/evidence")
async def identity_evidence(
    org_id: uuid.UUID, person_id: uuid.UUID, request: Request, response: Response, op: Reviewer
) -> dict:
    await kyc_svc.load_for_operator(op.session, org_id)
    persons = await kyc_checks.persons_for(op.session, org_id)
    person = next((p for p in persons if p.id == person_id), None)
    if person is None or person.identity_provider != "didit" or not person.provider_session_id:
        raise NotFoundError("No Didit session is available for this person")
    from app.services import audit, didit_client

    evidence = await didit_client.retrieve_session(
        request.app.state.settings, person.provider_session_id
    )
    audit.record(
        op.session,
        org_id,
        action="kyc.identity_evidence_viewed",
        target_type="kyc_person",
        target_id=str(person.id),
        actor_user_id=op.user.id,
    )
    await op.session.commit()
    response.headers["Cache-Control"] = "no-store, private"
    return evidence


@router.get("/applications/{org_id}/persons/{person_id}/evidence-media")
async def identity_evidence_media(
    org_id: uuid.UUID,
    person_id: uuid.UUID,
    request: Request,
    op: Reviewer,
    path: str = Query(max_length=500),
) -> Response:
    from urllib.parse import urlparse

    import httpx

    evidence = await identity_evidence(org_id, person_id, request, Response(), op)
    value = evidence
    try:
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, ValueError, TypeError):
        raise NotFoundError("Evidence not found") from None
    if not isinstance(value, str):
        raise NotFoundError("Evidence not found")
    url = urlparse(value)
    host = url.hostname or ""
    # Only Didit's own evidence storage; never forward our API key to media hosts.
    allowed = host.endswith(".didit.me") or (
        host.startswith("service-didit-") and host.endswith(".amazonaws.com")
    )
    if url.scheme != "https" or not allowed or url.port not in (None, 443):
        raise ValidationFailedError("This evidence must be opened through its source link")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", value) as remote:
            if remote.status_code != 200:
                raise NotFoundError("Evidence link expired. Reload the verification results.")
            content_type = remote.headers.get("content-type", "application/octet-stream")
            if not content_type.startswith(
                ("image/jpeg", "image/png", "image/webp", "video/mp4", "application/pdf")
            ):
                raise ValidationFailedError("Unsupported evidence format")
            data = bytearray()
            async for chunk in remote.aiter_bytes():
                data.extend(chunk)
                if len(data) > 30 * 1024 * 1024:
                    raise ValidationFailedError("Open this large file through its source link")
    return Response(
        bytes(data),
        media_type=content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


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


INDIVIDUAL_DECISION_EMAILS = {
    "approved": (
        "Your identity is verified",
        "Good news - your identity passed verification. Calling and phone numbers are now "
        "available in your workspace. Texting is not available on individual accounts.",
    ),
    "needs_info": (
        "We need a little more information to verify your identity",
        "Our reviewer needs something else before approving your account:\n\n{message}\n\n"
        "Open Settings > Identity verification to respond.",
    ),
    "rejected": (
        "Your identity could not be verified",
        "We were unable to verify your identity, so calling is not available.\n\n"
        "Reason: {message}\n\nReply to this email if you believe this is a mistake.",
    ),
}


async def _email_decision(
    request: Request, op: OperatorContext, org_id: uuid.UUID, status: str, message: str = ""
) -> None:
    org = await op.session.get(Org, org_id)
    individual = bool(org is not None and org.account_type == "individual")
    template = (INDIVIDUAL_DECISION_EMAILS if individual else DECISION_EMAILS).get(status)
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
    if await kyc_svc._is_individual(op.session, org_id) and op.operator.role != "admin":
        raise PermissionDeniedError(
            "An individual account can only be approved by an admin operator"
        )
    # Overriding sanctions, identity or document checks is a deliberate act with a reason,
    # never a side effect of which operator happened to click Approve.
    if payload.manual_override and not payload.note.strip():
        raise ValidationFailedError(
            "Write why you are approving despite the failed checks.",
            code="override_reason_required",
        )
    await kyc_svc.approve(
        op.session,
        request.app.state.settings,
        profile,
        op.user.id,
        payload.note,
        manual_override=payload.manual_override,
    )
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


class AccountActionIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    identifiers: list[str] = Field(default_factory=list, max_length=100)
    confirmation: str = Field(default="", max_length=320)


@router.get("/customer-accounts")
async def all_customer_accounts(
    op: Reviewer,
    q: str = Query(default="", max_length=320),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    from app.services import customer_accounts

    return await customer_accounts.directory(op.session, q.strip(), offset, limit)


@router.get("/customer-accounts/{user_id}")
async def customer_account_detail(user_id: uuid.UUID, op: Admin) -> dict:
    from app.services import customer_accounts

    user = await customer_accounts.target(op.session, user_id, op.user.id)
    orgs, blockers = await customer_accounts.deletion_plan(op.session, user)
    values = await customer_accounts.identifiers(op.session, user)
    return {
        "id": str(user.id),
        "email": user.email,
        "identifiers": [{"key": v["key"], "kind": v["kind"], "label": v["label"]} for v in values],
        "delete_workspaces": [{"id": str(o.id), "name": o.name} for o in orgs],
        "blockers": blockers,
    }


@router.post("/customer-accounts/{user_id}/blacklist", status_code=204)
async def blacklist_customer_account(
    user_id: uuid.UUID, payload: AccountActionIn, request: Request, op: Admin
) -> Response:
    from app.services import account_security, customer_accounts

    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="ban")
    user = await customer_accounts.target(op.session, user_id, op.user.id)
    if not payload.identifiers or not payload.reason.strip():
        raise ValidationFailedError("Select identifiers and enter a reason")
    count = await customer_accounts.apply_bans(
        op.session, user, op.user.id, payload.identifiers, payload.reason
    )
    user.is_active = False
    revoked = await account_security.revoke_sessions(
        op.session, request.app.state.settings, user.id, revoked_by=op.user.id
    )
    op.session.add(
        SecurityAlert(
            kind="account_blacklisted",
            status="reviewed",
            reviewed_by=op.user.id,
            detail={"target_user_id": str(user.id), "reason": payload.reason, "identifiers": count},
        )
    )
    await op.session.commit()
    await account_security.mark_revoked(request.app.state.settings, revoked)
    return Response(status_code=204)


@router.post("/customer-accounts/{user_id}/delete", status_code=204)
async def delete_customer_account(
    user_id: uuid.UUID, payload: AccountActionIn, request: Request, op: Admin
) -> Response:
    from app.services import customer_accounts

    await check_step_up(request, op.session, op.user, kind="recent_2fa", action="user_support")
    user = await customer_accounts.target(op.session, user_id, op.user.id)
    if not payload.reason.strip():
        raise ValidationFailedError("Enter a reason")
    await customer_accounts.delete_account(
        op.session,
        request,
        user,
        op.user.id,
        payload.confirmation,
        payload.identifiers,
        payload.reason,
    )
    return Response(status_code=204)
