"""The compliance choke point.

P1 cut this seam, P2 pinned it with spy tests, P3 fills it in. Every outbound message in
the system passes through :func:`check_outbound` exactly once, before any row is created
and before the carrier is touched — so a deny costs nothing and leaves no trace in
``messages``.

Order is deliberate: **opt-out → DNC → quiet hours.** Deny beats defer. Someone who has
said STOP must not merely be delayed until 8 a.m.

The one exemption, ``compliance_auto_reply``, exists because CTIA requires us to confirm a
STOP and to answer HELP — including for a contact we have just suppressed. It is
keyword-only, only ``compliance/service.py`` passes it, and an exempted send is still
audited. It bypasses the checks, never the choke point.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import quiet_hours as qh
from app.compliance import service as compliance_svc
from app.models import ComplianceBlock, Contact, ContactPhone, Message

log = structlog.get_logger("compliance.gate")

AUTO_REPLY_EXEMPTION = "compliance_auto_reply"
CONVERSATION_WINDOW_HOURS = 24


@dataclass(frozen=True)
class OutboundDraft:
    to_e164: str
    from_e164: str
    body: str


@dataclass(frozen=True)
class ComplianceVerdict:
    allowed: bool
    reason: str | None = None
    #: Set only for quiet-hours deferral: the earliest instant the send becomes legal.
    defer_until: datetime | None = None


async def _audit_block(
    session: AsyncSession,
    org_id: uuid.UUID,
    draft: OutboundDraft,
    reason: str,
    exemption: str | None = None,
) -> None:
    """Record the denial, and COMMIT it.

    The gate runs before any other row exists, so committing here is safe — and it must be
    committed, because the caller is about to raise and unwind.
    """
    session.add(
        ComplianceBlock(
            id=uuid.uuid4(),
            org_id=org_id,
            contact_e164=draft.to_e164,
            from_e164=draft.from_e164,
            reason=reason[:32],
            body_excerpt=(draft.body or "")[:255],
            exemption=exemption,
        )
    )
    await session.commit()


async def _contact_timezone(session: AsyncSession, e164: str) -> str | None:
    row = (
        await session.execute(
            sa.select(Contact.timezone)
            .join(ContactPhone, ContactPhone.contact_id == Contact.id)
            .where(ContactPhone.e164 == e164)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _in_active_conversation(session: AsyncSession, e164: str, now: datetime) -> bool:
    """Did this contact message US recently?

    Quiet hours are a telemarketing rule. Blocking an agent's 9:05 p.m. reply to someone
    who texted at 9:00 p.m. would make the inbox unusable while adding no legal safety.
    Campaigns (P11) always hit the gate cold, so bulk traffic never rides this carve-out.
    """
    cutoff = now - timedelta(hours=CONVERSATION_WINDOW_HOURS)
    found = (
        await session.execute(
            sa.select(Message.id)
            .where(
                Message.direction == "inbound",
                Message.from_e164 == e164,
                Message.created_at >= cutoff.replace(tzinfo=None)
                if session.get_bind().dialect.name == "sqlite"
                else Message.created_at >= cutoff,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def check_outbound(
    session: AsyncSession,
    org_id: uuid.UUID,
    draft: OutboundDraft,
    *,
    exemption: str | None = None,
) -> ComplianceVerdict:
    """The single gate every outbound message passes through."""
    if exemption == AUTO_REPLY_EXEMPTION:
        # A STOP confirmation must reach someone who just opted out, and a HELP reply must
        # reach a DNC-listed contact. Audited, not silent.
        log.info("compliance_exempt", exemption=exemption, to=draft.to_e164)
        return ComplianceVerdict(True, reason="exempt:compliance_auto_reply")

    # 1. Opt-out. Keyed on (org, contact) with no number anywhere, so STOP to one number
    #    suppresses the entire pool.
    if await compliance_svc.is_opted_out(session, draft.to_e164):
        await _audit_block(session, org_id, draft, "opted_out")
        return ComplianceVerdict(False, "opted_out")

    # 2. Internal DNC. (Federal DNC is NOT consulted - we have no subscription and never
    #    pretend otherwise. See service.scrub.)
    if await compliance_svc.is_dnc(session, draft.to_e164):
        await _audit_block(session, org_id, draft, "dnc")
        return ComplianceVerdict(False, "dnc")

    # 3. Quiet hours, in the recipient's local time.
    settings = await compliance_svc.get_settings(session, org_id)
    if not settings.quiet_hours_enforced:
        return ComplianceVerdict(True)

    now = qh._now()
    if await _in_active_conversation(session, draft.to_e164, now):
        return ComplianceVerdict(True, reason="active_conversation")

    result = qh.evaluate(
        draft.to_e164,
        contact_timezone=await _contact_timezone(session, draft.to_e164),
        window_start=settings.window_start,
        window_end=settings.window_end,
        now=now,
    )
    if not result.allowed:
        # DEFER, not drop: the caller persists the message with hold_until and the sweeper
        # releases it - re-running this whole gate at that point.
        return ComplianceVerdict(False, "quiet_hours", defer_until=result.not_before)

    return ComplianceVerdict(True)


async def on_inbound(
    session: AsyncSession, org_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    """Called once per ingested inbound message, after it is persisted.

    Handles STOP / START / HELP. Idempotent by the ledger's unique constraint on
    ``message_id``, so a replayed webhook cannot opt someone out twice or double-reply.
    """
    message = await session.get(Message, message_id)
    if message is None:  # pragma: no cover - caller just wrote it
        return
    # The carrier rides on the session (see messaging.CARRIER_SESSION_KEY) precisely so
    # this signature stays exactly what the seam tests pinned in P1/P2.
    carrier = session.info.get("carrier")
    await compliance_svc.handle_inbound_keyword(session, org_id, message, carrier)
