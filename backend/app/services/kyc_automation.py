"""P43: runs the whole verification pipeline without a human, in order.

1. AI-read every document that hasn't been reviewed yet.
2. Re-run the checks that depend on those reviews (registry fallback, documents roll-up).
3. Refresh the risk tier.
4. Write a fresh AI decision pack when anything changed.

Called after a document upload (reviews only, so the applicant gets fast feedback), after
submission, and from the sweeper for anything still pending. Safe to run repeatedly.
Nothing here ever approves or rejects.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import set_org_context
from app.models import KycDocument, KycProfile
from app.services import kyc_checks, kyc_decision, kyc_doc_reader

log = structlog.get_logger("kyc_automation")

REVIEW_STATUSES = ("submitted", "in_review", "needs_info", "reverification_due")
ERROR_RETRY_AFTER = timedelta(minutes=10)


async def _documents(session: AsyncSession, org_id: uuid.UUID) -> list[KycDocument]:
    return list(
        (
            await session.execute(
                sa.select(KycDocument)
                .where(KycDocument.org_id == org_id)
                .order_by(KycDocument.created_at)
            )
        )
        .scalars()
        .all()
    )


async def review_documents(
    session: AsyncSession,
    settings: Settings,
    object_store,
    profile: KycProfile,
    *,
    force: bool = False,
) -> int:
    persons = await kyc_checks.persons_for(session, profile.org_id)
    reviewed = 0
    now = datetime.now(timezone.utc)
    for document in await _documents(session, profile.org_id):
        if document.review_result not in (None, "error"):
            continue
        last = document.reviewed_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        # An AI outage is retried automatically, but not on every upload and sweep.
        if (
            document.review_result == "error"
            and not force
            and last
            and now - last < ERROR_RETRY_AFTER
        ):
            continue
        await kyc_doc_reader.review_document(
            session, settings, object_store, profile, document, persons
        )
        reviewed += 1
        await session.commit()
        set_org_context(session, profile.org_id)
    return reviewed


def _keep_if_changed(session: AsyncSession, previous, row) -> None:
    """The pipeline re-runs often; only record a check when its outcome actually changed."""
    if (
        previous is not None
        and previous.created_by is None
        and previous.result == row.result
        and previous.summary == row.summary
    ):
        session.expunge(row)


async def process(
    session: AsyncSession,
    settings: Settings,
    object_store,
    org_id: uuid.UUID,
    *,
    http_client: httpx.AsyncClient | None = None,
    reviews_only: bool = False,
    force_reviews: bool = False,
) -> dict:
    from app.services import kyc as kyc_svc

    set_org_context(session, org_id)
    profile = await kyc_svc.get_profile(session, org_id)
    if profile is None:
        return {}
    counts = {
        "documents_reviewed": await review_documents(
            session, settings, object_store, profile, force=force_reviews
        )
    }
    set_org_context(session, org_id)
    if reviews_only or profile.status not in REVIEW_STATUSES:
        return counts

    persons = await kyc_checks.persons_for(session, org_id)
    documents = await _documents(session, org_id)
    checks = await kyc_checks.latest_checks(session, org_id)
    _keep_if_changed(
        session,
        checks.get("documents"),
        kyc_checks.check_documents(session, profile, persons, documents),
    )
    registry = checks.get("registry")
    if (
        registry is None
        or registry.result in ("pending", "error")
        or ((registry.detail or {}).get("from_documents"))
    ):
        owns = http_client is None
        client = http_client or httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": "csaas-kyc/1.0"}
        )
        try:
            _keep_if_changed(
                session,
                registry,
                await kyc_checks.check_registry(session, settings, profile, persons, client),
            )
        except Exception:  # noqa: BLE001 - registry trouble must not stop the pack
            log.exception("kyc_registry_retry_failed", org_id=str(org_id))
        finally:
            if owns:
                await client.aclose()
    await session.flush()
    await kyc_svc.refresh_risk(session, settings, profile)
    await session.flush()
    pack = await kyc_decision.generate_if_stale(session, settings, profile)
    counts["decision_written"] = int(pack is not None)
    await session.commit()
    return counts


async def tick(
    session: AsyncSession,
    settings: Settings,
    object_store,
    *,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 10,
) -> dict:
    """Sweeper entry point: every open application gets its pipeline advanced."""
    from app.db.base import ALLOW_UNSCOPED_KEY

    # JUSTIFIED allow_unscoped: the sweeper walks every workspace's open application.
    org_ids = (
        (
            await session.execute(
                sa.select(KycProfile.org_id)
                .where(KycProfile.status.in_(("draft", *REVIEW_STATUSES)))
                .order_by(KycProfile.updated_at.desc())
                .limit(limit * 5)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    totals = {"documents_reviewed": 0, "decisions_written": 0}
    for org_id in org_ids[: limit * 5]:
        try:
            counts = await process(session, settings, object_store, org_id, http_client=http_client)
        except Exception:  # noqa: BLE001 - one broken application must not stop the rest
            log.exception("kyc_automation_failed", org_id=str(org_id))
            await session.rollback()
            continue
        totals["documents_reviewed"] += counts.get("documents_reviewed", 0)
        totals["decisions_written"] += counts.get("decision_written", 0)
    return totals
