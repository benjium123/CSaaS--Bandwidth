"""P41 background work for trust & safety, driven by services/sweeper.py.

Hourly:
  - annual re-verification: approved -> reverification_due; once every owner has redone the
    ID + selfie check and sanctions/ban-list re-screens clean, back to approved; after the
    grace period with no re-verification, -> needs_info (telephony stops)
  - reconcile Stripe Identity sessions whose webhook never arrived
  - write the AI reviewer summary for newly submitted applications
  - delete spent WebAuthn challenges
Daily:
  - refresh the sanctions lists and the Tor exit list, then re-screen approved businesses
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    KycCheck,
    KycPerson,
    KycProfile,
    KycStepUp,
    SecurityAlert,
    WebauthnChallenge,
)
from app.services import audit as audit_svc
from app.services import kyc as kyc_svc
from app.services import kyc_checks, kyc_step_up, login_risk, sanctions, stripe_client

log = structlog.get_logger(__name__)

RECONCILE_AFTER = timedelta(minutes=10)
RECONCILE_BATCH = 50
AI_SUMMARY_BATCH = 5


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _profiles(session: AsyncSession, *statuses: str) -> list[KycProfile]:
    # JUSTIFIED allow_unscoped: a platform tick walks every org's application.
    return list(
        (
            await session.execute(
                sa.select(KycProfile)
                .where(KycProfile.status.in_(statuses))
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


async def reverification_tick(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    counts = {"reverification_started": 0, "reverified": 0, "reverification_lapsed": 0}

    for profile in await _profiles(session, "approved"):
        due = _aware(profile.next_reverification_at)
        if due is None or due > now:
            continue
        set_org_context(session, profile.org_id)
        kyc_svc.transition(profile, "reverification_due")
        profile.info_request = (
            "Annual re-verification: each owner needs to repeat the ID and selfie check within "
            f"{settings.kyc_reverify_grace_days} days to keep calling and texting."
        )
        audit_svc.record(
            session,
            profile.org_id,
            action="kyc.reverification_started",
            target_type="kyc_profile",
            target_id=str(profile.id),
        )
        counts["reverification_started"] += 1
    await session.commit()

    for profile in await _profiles(session, "reverification_due"):
        set_org_context(session, profile.org_id)
        started = _aware(profile.next_reverification_at) or now
        if await kyc_svc._is_individual(session, profile.org_id):
            if now >= started + timedelta(days=settings.kyc_reverify_grace_days):
                kyc_svc.transition(profile, "needs_info")
                profile.info_request = (
                    "Re-verification is overdue and needs a manual review by a superadmin."
                )
                audit_svc.record(
                    session,
                    profile.org_id,
                    action="kyc.reverification_lapsed",
                    target_type="kyc_profile",
                    target_id=str(profile.id),
                )
                counts["reverification_lapsed"] += 1
            else:
                profile.info_request = (
                    "Re-verification is in progress and needs a manual review by a superadmin."
                )
            continue
        owners = [
            p
            for p in await kyc_checks.persons_for(session, profile.org_id)
            if p.role in ("owner", "beneficial_owner")
        ]
        redone = owners and all(
            p.status == "verified" and (_aware(p.verified_at) or started) >= started for p in owners
        )
        if redone:
            sanctions_row = kyc_checks.check_sanctions(session, settings, profile, owners)
            ban_row = await kyc_checks.check_ban_list(session, profile, owners)
            if sanctions_row.result == "pass" and ban_row.result == "pass":
                kyc_svc.transition(profile, "approved")
                profile.info_request = None
                profile.next_reverification_at = now + timedelta(days=settings.kyc_reverify_days)
                action = "kyc.reverified"
                counts["reverified"] += 1
            else:
                kyc_svc.transition(profile, "needs_info")
                profile.info_request = "Re-verification needs a manual review."
                action = "kyc.reverification_flagged"
            audit_svc.record(
                session,
                profile.org_id,
                action=action,
                target_type="kyc_profile",
                target_id=str(profile.id),
            )
        elif now >= started + timedelta(days=settings.kyc_reverify_grace_days):
            kyc_svc.transition(profile, "needs_info")
            profile.info_request = (
                "Re-verification was not completed in time. Calling and texting are paused "
                "until each owner repeats the ID and selfie check."
            )
            audit_svc.record(
                session,
                profile.org_id,
                action="kyc.reverification_lapsed",
                target_type="kyc_profile",
                target_id=str(profile.id),
            )
            counts["reverification_lapsed"] += 1
    await session.commit()
    return counts


async def reconcile_identity_sessions(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> int:
    if not stripe_client.is_configured(settings):
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - RECONCILE_AFTER
    fixed = 0
    persons = (
        (
            await session.execute(
                sa.select(KycPerson)
                .where(
                    KycPerson.status.in_(("pending", "processing")),
                    KycPerson.stripe_verification_session_id.is_not(None),
                    KycPerson.updated_at <= cutoff,
                )
                .limit(RECONCILE_BATCH)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    for person in persons:
        try:
            outcome = await stripe_client.retrieve_verification_outcome(
                settings, person.stripe_verification_session_id
            )
        except Exception:  # noqa: BLE001
            log.warning("kyc_reconcile_failed", person_id=str(person.id), exc_info=True)
            continue
        set_org_context(session, person.org_id)
        before = person.status
        await kyc_svc.apply_person_outcome(session, person, outcome)
        fixed += int(person.status != before)
    step_ups = (
        (
            await session.execute(
                sa.select(KycStepUp)
                .where(
                    KycStepUp.status.in_(("pending", "processing")),
                    KycStepUp.stripe_verification_session_id.is_not(None),
                    KycStepUp.updated_at <= cutoff,
                )
                .limit(RECONCILE_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for row in step_ups:
        try:
            outcome = await stripe_client.retrieve_verification_outcome(
                settings, row.stripe_verification_session_id
            )
        except Exception:  # noqa: BLE001
            continue
        before = row.status
        await kyc_step_up.apply_outcome(session, row.stripe_verification_session_id, outcome)
        fixed += int(row.status != before)
    await session.commit()
    return fixed


async def ai_summary_tick(session: AsyncSession, settings: Settings, client=None) -> int:
    if kyc_checks._pick_provider(settings) is None:
        return 0
    written = 0
    for profile in (await _profiles(session, "submitted", "in_review"))[: AI_SUMMARY_BATCH * 4]:
        set_org_context(session, profile.org_id)
        has_summary = (
            await session.execute(
                sa.select(KycCheck.id).where(
                    KycCheck.org_id == profile.org_id,
                    KycCheck.kind == "ai_summary",
                    KycCheck.created_at >= (profile.submitted_at or profile.created_at),
                )
            )
        ).first()
        if has_summary is not None:
            continue
        row = await kyc_checks.generate_ai_summary(session, settings, profile, client=client)
        if row is not None:
            await session.flush()
            await kyc_svc.refresh_risk(session, settings, profile)
            written += 1
        await session.commit()
        if written >= AI_SUMMARY_BATCH:
            break
    return written


async def cleanup_challenges(session: AsyncSession, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    result = await session.execute(
        sa.delete(WebauthnChallenge).where(WebauthnChallenge.expires_at < now - timedelta(days=1))
    )
    await session.commit()
    return result.rowcount or 0


async def daily_lists_tick(session: AsyncSession, settings: Settings, client=None) -> dict:
    counts = await sanctions.refresh(settings, client=client)
    try:
        counts["tor_exits"] = await login_risk.refresh_tor_exit_list(settings, client=client)
    except Exception:  # noqa: BLE001
        log.warning("tor_exit_refresh_failed", exc_info=True)
    hits = 0
    for profile in await _profiles(session, "approved", "reverification_due"):
        set_org_context(session, profile.org_id)
        persons = await kyc_checks.persons_for(session, profile.org_id)
        row = kyc_checks.check_sanctions(session, settings, profile, persons)
        if row.result in ("fail", "warn"):
            hits += 1
            session.add(
                SecurityAlert(
                    id=uuid.uuid4(),
                    kind="sanctions_hit",
                    org_id=profile.org_id,
                    status="open",
                    detail={
                        "legal_name": profile.legal_name,
                        "result": row.result,
                        "exact": (row.detail or {}).get("exact"),
                        "partial": (row.detail or {}).get("partial"),
                    },
                )
            )
    await session.commit()
    counts["sanctions_hits"] = hits
    return counts
