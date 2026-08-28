"""Consent ledger, DNC list, scrub, and the legally-required auto-replies.

Everything here reads current state by **deriving it from the append-only ledger**, never
from a denormalized flag. The ledger is the audit trail a TCPA complaint is answered with,
so it must be the same data the gate actually enforces on — if state lived in a flag column
the two could disagree, and the flag is the one that would be wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.keywords import KeywordHit, classify_keyword
from app.errors import ConflictError
from app.models import ComplianceSettings, ConsentEvent, DncEntry, Org

log = structlog.get_logger("compliance")

AUTO_REPLY_EXEMPTION = "compliance_auto_reply"
FEDERAL_DNC_UNCHECKED = "federal_dnc:unchecked"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------
async def get_settings(session: AsyncSession, org_id: uuid.UUID) -> ComplianceSettings:
    """Lazily create one settings row per org, with the federal defaults."""
    row = (
        await session.execute(sa.select(ComplianceSettings).limit(1))
    ).scalar_one_or_none()
    if row is not None:
        return row

    row = ComplianceSettings(id=uuid.uuid4(), org_id=org_id)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:  # pragma: no cover - concurrent first-read
        await session.rollback()
        from app.db.base import set_org_context

        set_org_context(session, org_id)
        row = (await session.execute(sa.select(ComplianceSettings).limit(1))).scalar_one()
    return row


# --------------------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------------------
async def latest_consent(
    session: AsyncSession, contact_e164: str, channel: str = "sms"
) -> ConsentEvent | None:
    """The most recent opt_out/opt_in for this contact on this channel.

    Note the key: (org via session scope, contact_e164, channel). There is NO our_e164
    anywhere - that absence is what makes STOP suppress the whole number pool.
    """
    stmt = (
        sa.select(ConsentEvent)
        .where(
            ConsentEvent.contact_e164 == contact_e164,
            ConsentEvent.channel == channel,
            ConsentEvent.event.in_(("opt_out", "opt_in")),
        )
        # seq decides same-timestamp pairs (the random-UUID id was a coin flip there);
        # id remains only as a stable last resort for pre-seq rows backfilled with 0.
        .order_by(
            ConsentEvent.created_at.desc(), ConsentEvent.seq.desc(), ConsentEvent.id.desc()
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def is_opted_out(
    session: AsyncSession, contact_e164: str, channel: str = "sms"
) -> bool:
    latest = await latest_consent(session, contact_e164, channel)
    return latest is not None and latest.event == "opt_out"


async def record_consent(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    contact_e164: str,
    event: str,
    source: str,
    channel: str = "sms",
    keyword_matched: str | None = None,
    message_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> ConsentEvent | None:
    """Append one ledger row. Returns None when this message was already processed.

    The unique constraint on message_id is the idempotency mechanism: a replayed inbound
    webhook cannot record a second opt-out or trigger a second auto-reply.
    """
    row = ConsentEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        contact_e164=contact_e164,
        channel=channel,
        event=event,
        source=source,
        keyword_matched=keyword_matched,
        message_id=message_id,
        actor_user_id=actor_user_id,
        details=details or {},
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        from app.db.base import set_org_context

        set_org_context(session, org_id)
        return None
    return row


async def manual_opt_in(
    session: AsyncSession,
    org_id: uuid.UUID,
    contact_e164: str,
    actor_user_id: uuid.UUID | None = None,
) -> ConsentEvent:
    """An operator resubscribing someone.

    **Refused when the standing opt-out came from a keyword.** Only the consumer's own
    START reverses their own STOP; an operator "fixing" a STOP is precisely the shape of a
    TCPA claim.
    """
    latest = await latest_consent(session, contact_e164)
    if latest is not None and latest.event == "opt_out" and latest.source == "keyword":
        raise ConflictError(
            "This contact opted out by texting a STOP keyword. Only they can resubscribe "
            "by replying START."
        )
    row = await record_consent(
        session,
        org_id,
        contact_e164=contact_e164,
        event="opt_in",
        source="manual",
        actor_user_id=actor_user_id,
    )
    assert row is not None
    return row


# --------------------------------------------------------------------------------------
# DNC
# --------------------------------------------------------------------------------------
async def is_dnc(session: AsyncSession, e164: str) -> bool:
    row = (
        await session.execute(sa.select(DncEntry).where(DncEntry.e164 == e164).limit(1))
    ).scalar_one_or_none()
    return row is not None


async def add_dnc(
    session: AsyncSession,
    org_id: uuid.UUID,
    e164: str,
    *,
    source: str = "manual",
    reason: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> DncEntry:
    existing = (
        await session.execute(sa.select(DncEntry).where(DncEntry.e164 == e164).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    entry = DncEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        e164=e164,
        source=source,
        reason=reason,
        added_by_user_id=actor_user_id,
    )
    session.add(entry)
    # The mutable working table is a convenience; the append-only ledger is the audit trail.
    await record_consent(
        session,
        org_id,
        contact_e164=e164,
        event="dnc_add",
        source="api" if source == "manual" else source,
        actor_user_id=actor_user_id,
        details={"reason": reason} if reason else {},
    )
    await session.flush()
    return entry


async def remove_dnc(
    session: AsyncSession,
    org_id: uuid.UUID,
    e164: str,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    entry = (
        await session.execute(sa.select(DncEntry).where(DncEntry.e164 == e164).limit(1))
    ).scalar_one_or_none()
    if entry is None:
        return False
    await session.delete(entry)
    await record_consent(
        session,
        org_id,
        contact_e164=e164,
        event="dnc_remove",
        source="api",
        actor_user_id=actor_user_id,
    )
    await session.flush()
    return True


@dataclass(frozen=True)
class ScrubResult:
    e164: str
    ok: bool
    reasons: list[str] = field(default_factory=list)
    #: ALWAYS False. See the note in scrub().
    federal_checked: bool = False


async def scrub(
    session: AsyncSession, org_id: uuid.UUID, numbers: list[str]
) -> list[ScrubResult]:
    """Check numbers against opt-out state and the internal DNC list.

    **This does NOT check the federal DNC registry.** We hold no SAN subscription and no
    OSS library exists, so every result carries ``federal_checked=False`` and the reason
    ``federal_dnc:unchecked``. There is deliberately no settings flag that could flip that
    to True - a deployment must never be able to believe it is scrubbing when it is not.
    A real integration lands behind a FederalDncChecker slot in this signature, later.
    """
    results: list[ScrubResult] = []
    for raw in numbers:
        e164 = (raw or "").strip()
        reasons = [FEDERAL_DNC_UNCHECKED]
        ok = True
        if not e164:
            results.append(ScrubResult(raw, False, ["invalid"] + reasons))
            continue
        if await is_opted_out(session, e164):
            ok = False
            reasons.insert(0, "opted_out")
        if await is_dnc(session, e164):
            ok = False
            reasons.insert(0, "dnc")
        results.append(ScrubResult(e164, ok, reasons))
    return results


# --------------------------------------------------------------------------------------
# Auto-replies
# --------------------------------------------------------------------------------------
async def _org_name(session: AsyncSession, org_id: uuid.UUID) -> str:
    from app.db.base import ALLOW_UNSCOPED_KEY

    org = (
        await session.execute(
            sa.select(Org).where(Org.id == org_id).execution_options(
                **{ALLOW_UNSCOPED_KEY: True}
            )
        )
    ).scalar_one_or_none()
    return org.name if org else "us"


def _interpolate(text: str, org_name: str, help_contact: str) -> str:
    return text.replace("{org}", org_name).replace("{help_contact}", help_contact or "support")


async def auto_reply_body(
    session: AsyncSession, org_id: uuid.UUID, hit: KeywordHit
) -> str | None:
    settings = await get_settings(session, org_id)
    org_name = await _org_name(session, org_id)
    template = {
        "opt_out": settings.optout_text,
        "opt_in": settings.optin_text,
        "help": settings.help_text,
    }.get(hit.kind)
    if not template:
        return None
    return _interpolate(template, org_name, settings.help_contact)


async def handle_inbound_keyword(
    session: AsyncSession, org_id: uuid.UUID, message, carrier=None  # noqa: ANN001
) -> None:
    """Classify one inbound message and, if it is a keyword, act on it.

    Order matters: the ledger row goes in FIRST, so if the auto-reply fails the contact is
    still opted out. Suppression must never depend on our ability to send.
    """
    hit = classify_keyword(message.body)
    if hit is None:
        return

    # HELP is ledgered as its own event type: it must be answered (even for someone
    # already opted out) but it must NOT change consent state.
    event = {"opt_out": "opt_out", "opt_in": "opt_in", "help": "help_request"}[hit.kind]

    recorded = await record_consent(
        session,
        org_id,
        contact_e164=message.from_e164,
        event=event,
        source="keyword",
        keyword_matched=hit.matched,
        message_id=message.id,
    )
    if recorded is None:
        # Already processed - this is a webhook replay. No second ledger row, no second
        # auto-reply. The unique constraint on message_id did the work.
        return
    await session.commit()

    body = await auto_reply_body(session, org_id, hit)
    if not body:
        return
    if carrier is None:
        # No carrier reached us (e.g. none configured). The opt-out already landed, which
        # is the part that matters legally; the confirmation is best-effort.
        log.warning("auto_reply_skipped_no_carrier", keyword=hit.matched)
        return

    # Sent from the number they texted - never a sticky-sender lookup. A confirmation
    # must come from the number the human sees in their thread.
    from app.services import messaging as messaging_svc

    try:
        await messaging_svc.send_message(
            session,
            org_id,
            carrier,
            to_e164=message.from_e164,
            from_e164=message.to_e164,
            body=body,
            exemption=AUTO_REPLY_EXEMPTION,
        )
    except Exception:
        # An auto-reply failure must never break ingestion. The opt-out already landed.
        log.exception("auto_reply_failed", keyword=hit.matched, contact=message.from_e164)
