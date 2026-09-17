"""P43: the AI text guard - every outbound text is checked before it reaches a carrier.

Order, at the moment of sending (``messaging._dispatch_to_carrier``, which every send path
reaches - single sends, campaigns, scheduled and held texts, the AI SMS agent, API keys):

1. Compliance auto-replies (STOP/HELP confirmations) are exempt.
2. Cache: one verdict per workspace per normalised body, so a campaign costs one AI call.
3. Rules (``monitor_rules``): a clear scam combination is blocked outright.
4. DeepSeek Flash with the business's declared use case: allow / hold / block.
5. AI unreachable: new or flagged accounts HOLD (fail safe); established normal accounts
   send, and the text is re-checked in the background ("unchecked").

Held texts get a second, stricter look by the sweeper within about a minute: cleared ->
sent; confirmed -> rejected + risk signal. Nothing is held longer than
MONITOR_HOLD_MAX_HOURS.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import KycProfile, Message, Org, TextVerdict
from app.services import ai_guard, monitor_rules, monitor_score

log = structlog.get_logger("monitor_text")

VERDICT_TTL = timedelta(days=7)
#: A sweeper tick never spends longer than this on AI calls.
TICK_BUDGET_SECONDS = 60
UNCHECKED_TTL = timedelta(hours=1)

PUBLIC_BLOCKED = "Not sent - this message looks like a scam and was blocked by our safety checks."
PUBLIC_HELD = "Held for a quick safety review - it will send automatically if it's cleared."
PUBLIC_EXPIRED = "Not sent - this message could not be cleared by our safety review."

SYSTEM = """You check every outbound text message a business sends through a telecom
platform, before it is delivered, to stop scams, phishing and fraud reaching people.

Decide:
- "allow": normal business messaging - reminders, confirmations, delivery updates, one-time
  codes the business itself sends, marketing that fits the business, customer service.
- "hold": suspicious and needs a closer look - unusual links, requests for personal or
  financial details, urgency that doesn't fit the business, content unrelated to the
  declared business.
- "block": clearly a scam - impersonating a government agency, bank or delivery company,
  gift card / crypto / wire payment demands, fake prizes or refunds, threats, requests for
  passwords, one-time codes or remote access, fake "account locked" alerts.

Judge the message against what the business says it does: a dental office texting about tax
debt or crypto is not normal for it. Rule findings are hints, not verdicts.

Return JSON: {"verdict": "allow"|"hold"|"block", "category": "none"|"phishing"|
"impersonation"|"payment_scam"|"prize_scam"|"off_business"|"harassment"|"other",
"confidence": 0-100, "reason": "one short sentence"}"""

SECOND_LOOK_SYSTEM = SYSTEM.replace(
    "You check every outbound text message",
    "You are the SECOND reviewer of an outbound text message that was held as suspicious. "
    "Only confirm (hold or block) if you are genuinely confident it is harmful; clear it "
    '(verdict "allow") if a reasonable business would send it. '
    "You check every outbound text message",
)


@dataclass
class Screening:
    action: str  # allow | hold | block
    reason: str = ""
    category: str = "none"
    source: str = "rules"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_KEEP_DIGITS = re.compile(
    # links and phone numbers: where a message sends people is what the verdict is about
    r"(?i)(https?://\S+|www\.\S+|[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?|\+?\d[\d\s().-]{6,}\d)"
)


def normalise(body: str) -> str:
    """Codes, amounts and dates don't change what a message is; links and phone numbers do,
    so their digits are kept (a clean verdict must never carry over to a different number
    or domain)."""
    text = (body or "").lower()
    parts: list[str] = []
    last = 0
    for match in _KEEP_DIGITS.finditer(text):
        parts.append(re.sub(r"\d", "#", text[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(re.sub(r"\d", "#", text[last:]))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def body_hash(body: str) -> str:
    return hashlib.sha256(normalise(body).encode()).hexdigest()


def trusted_hosts(settings: Settings) -> frozenset[str]:
    from urllib.parse import urlsplit

    hosts = set()
    for url in (settings.public_web_url, settings.public_base_url):
        host = (urlsplit(url or "").hostname or "").lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


async def _cached(session: AsyncSession, org_id: uuid.UUID, digest: str) -> TextVerdict | None:
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(TextVerdict).where(
                TextVerdict.org_id == org_id, TextVerdict.body_hash == digest
            )
        )
    ).scalar_one_or_none()
    if row is None or (_aware(row.expires_at) or _now()) <= _now():
        return None
    return row


async def _store(
    session: AsyncSession,
    org_id: uuid.UUID,
    digest: str,
    screening: Screening,
    *,
    confidence: int | None = None,
    tokens: tuple[int, int] = (0, 0),
    ttl: timedelta = VERDICT_TTL,
) -> None:
    from sqlalchemy.exc import IntegrityError

    values = {
        "verdict": screening.action,
        "category": screening.category[:32],
        "reason": screening.reason[:500],
        "source": screening.source,
        "confidence": confidence,
        "tokens_in": tokens[0],
        "tokens_out": tokens[1],
        "expires_at": _now() + ttl,
    }

    async def _existing() -> TextVerdict | None:
        set_org_context(session, org_id)
        return (
            await session.execute(
                sa.select(TextVerdict).where(
                    TextVerdict.org_id == org_id, TextVerdict.body_hash == digest
                )
            )
        ).scalar_one_or_none()

    row = await _existing()
    if row is None:
        # Written (flushed) NOW, inside a savepoint: two sends of the same new text racing
        # must not surface a unique-constraint error at the commit AFTER the carrier call.
        try:
            async with session.begin_nested():
                session.add(
                    TextVerdict(id=uuid.uuid4(), org_id=org_id, body_hash=digest, **values)
                )
                await session.flush()
            return
        except IntegrityError:
            row = await _existing()
            if row is None:
                return
    for key, value in values.items():
        setattr(row, key, value)
    await session.flush()


async def business_context(session: AsyncSession, org_id: uuid.UUID) -> dict:
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    org = await session.get(Org, org_id)
    set_org_context(session, org_id)
    use_case = (profile.use_case or {}) if profile else {}
    return {
        "business_name": (profile.legal_name if profile else None) or (org.name if org else None),
        "website": profile.website if profile else None,
        "what_they_do": use_case.get("description"),
        "vertical": use_case.get("vertical"),
        "who_they_contact": use_case.get("who_you_contact"),
    }


async def ask_ai(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    body: str,
    rules: monitor_rules.RuleResult,
    *,
    second_look: bool = False,
) -> tuple[Screening, int | None, tuple[int, int]]:
    context = await business_context(session, org_id)
    return await judge_text(settings, context, body, rules, second_look=second_look)


async def judge_text(
    settings: Settings,
    context: dict,
    body: str,
    rules: monitor_rules.RuleResult,
    *,
    second_look: bool = False,
) -> tuple[Screening, int | None, tuple[int, int]]:
    """The AI verdict on one text for a business - no database, so the exam and canary use
    exactly the same judgement as live traffic."""
    user = "\n\n".join(
        [
            ai_guard.data_block("business", context),
            ai_guard.data_block("message", body[:2000]),
            ai_guard.data_block(
                "rule_findings", {"action": rules.action, "reasons": rules.reasons}
            ),
            "Give your verdict on the message.",
        ]
    )
    judgement = await ai_guard.judge(
        settings,
        task="text_second_look" if second_look else "text_check",
        system=SECOND_LOOK_SYSTEM if second_look else SYSTEM,
        user=user,
        max_tokens=200,
        timeout=settings.monitor_text_ai_timeout_seconds,
    )
    verdict = judgement.data.get("verdict")
    if verdict not in ("allow", "hold", "block"):
        raise ai_guard.AIUnavailable("AI gave no valid verdict")
    try:
        confidence = int(judgement.data.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    return (
        Screening(
            action=verdict,
            reason=str(judgement.data.get("reason") or "")[:255],
            category=str(judgement.data.get("category") or "none")[:32],
            source="second_look" if second_look else "ai",
        ),
        confidence,
        (judgement.tokens_in, judgement.tokens_out),
    )


async def screen(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, message: Message
) -> Screening:
    if not settings.monitor_enforced:
        return Screening(action="allow", source="off")
    if message.moderation_state in ("exempt", "cleared", "allowed"):
        return Screening(action="allow", source="earlier")
    body = message.body or ""
    if not body.strip() and not message.media:
        return Screening(action="allow", source="empty")
    digest = body_hash(body)
    cached = await _cached(session, org_id, digest)
    if cached is not None and cached.source != "unchecked":
        return Screening(
            action=cached.verdict,
            reason=cached.reason or "",
            category=cached.category or "none",
            source="cache",
        )

    rules = monitor_rules.evaluate(body, trusted_hosts=trusted_hosts(settings))
    if rules.action == "block":
        screening = Screening(
            action="block", reason="; ".join(rules.reasons), category=rules.category
        )
        await _store(session, org_id, digest, screening)
        return screening

    try:
        screening, confidence, tokens = await ask_ai(session, settings, org_id, body, rules)
    except ai_guard.AIUnavailable:
        state = await monitor_score.get_state(session, org_id, create=False)
        flagged = state is not None and state.level != "normal"
        if (
            rules.action == "hold"
            or flagged
            or await monitor_score.is_new_account(session, settings, org_id)
        ):
            return Screening(
                action="hold",
                reason="; ".join(rules.reasons) or "Safety check unavailable - held to be safe",
                category=rules.category,
                source="fail_safe",
            )
        # Established, normal-level account: don't stop legitimate traffic for an outage,
        # but remember to check this body later.
        await _store(
            session,
            org_id,
            digest,
            Screening(
                action="allow", reason="Not checked yet (AI unavailable)", source="unchecked"
            ),
            ttl=UNCHECKED_TTL,
        )
        return Screening(action="allow", source="unchecked")
    await _store(session, org_id, digest, screening, confidence=confidence, tokens=tokens)
    return screening


async def apply_hold_or_block(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    message: Message,
    screening: Screening,
) -> Message:
    """Record a hold or block on the message (as data, like a carrier rejection)."""
    set_org_context(session, org_id)
    message = await session.get(Message, message.id)
    message.moderation_reason = (screening.reason or screening.category or "")[:255] or None
    if screening.action == "block":
        message.status = "rejected"
        message.moderation_state = "blocked"
        message.hold_until = None
        message.error_code = "blocked_scam"
        message.error_detail = (screening.reason or "Blocked by the safety check")[:255]
        message.failure_reason_public = PUBLIC_BLOCKED
        await monitor_score.add_signal(
            session,
            settings,
            org_id,
            "text_blocked",
            f"Blocked text: {screening.reason or screening.category}",
            detail={"category": screening.category, "source": screening.source},
            message_id=message.id,
        )
    else:
        message.moderation_state = "held"
        message.hold_until = None
        message.failure_reason_public = PUBLIC_HELD
    await session.commit()
    return message


async def second_look_tick(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 50,
) -> dict:
    """Clear or confirm held texts. A cleared text is handed back to the normal held-message
    release (messaging.release_held_messages), which re-runs compliance and sends it through
    its own carrier on the same sweeper pass."""
    counts = {"cleared": 0, "confirmed": 0, "expired": 0, "waiting": 0}
    # JUSTIFIED allow_unscoped: the sweeper walks held texts across workspaces.
    held = (
        (
            await session.execute(
                sa.select(Message)
                .where(Message.moderation_state == "held", Message.status == "queued")
                .order_by(Message.created_at)
                .limit(limit)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    started = time.monotonic()
    ai_down = False
    for message in held:
        org_id = message.org_id
        set_org_context(session, org_id)
        created = _aware(message.created_at) or _now()
        digest = body_hash(message.body or "")
        cached = await _cached(session, org_id, digest)
        decided: Screening | None = None
        # When the AI is down (or this pass has run long) the rest of the batch only gets
        # the cheap expiry check - the sweeper must not stall on a slow AI.
        out_of_time = time.monotonic() - started > TICK_BUDGET_SECONDS
        if cached is not None and cached.source in ("second_look", "operator"):
            decided = Screening(
                action=cached.verdict, reason=cached.reason or "", source=cached.source
            )
        elif ai_down or out_of_time:
            decided = None
        else:
            rules = monitor_rules.evaluate(
                message.body or "", trusted_hosts=trusted_hosts(settings)
            )
            try:
                decided, confidence, tokens = await ask_ai(
                    session, settings, org_id, message.body or "", rules, second_look=True
                )
                await _store(session, org_id, digest, decided, confidence=confidence, tokens=tokens)
            except ai_guard.AIUnavailable:
                decided = None
                ai_down = True
        if decided is None:
            if _now() - created > timedelta(hours=settings.monitor_hold_max_hours):
                message.status = "rejected"
                message.moderation_state = "blocked"
                message.error_code = "hold_expired"
                message.failure_reason_public = PUBLIC_EXPIRED
                counts["expired"] += 1
            else:
                counts["waiting"] += 1
            await session.commit()
            continue
        if decided.action == "allow":
            message.moderation_state = "cleared"
            message.failure_reason_public = None
            message.hold_until = _now()
            await session.commit()
            counts["cleared"] += 1
            continue
        message.status = "rejected"
        message.moderation_state = "blocked"
        message.error_code = "blocked_scam"
        message.error_detail = (decided.reason or "Confirmed by the safety review")[:255]
        message.failure_reason_public = PUBLIC_BLOCKED
        await monitor_score.add_signal(
            session,
            settings,
            org_id,
            "text_held_confirmed",
            f"Held text confirmed harmful: {decided.reason or decided.category}",
            detail={"category": decided.category},
            message_id=message.id,
        )
        await session.commit()
        counts["confirmed"] += 1
    return counts


async def unchecked_tick(session: AsyncSession, settings: Settings, limit: int = 50) -> int:
    """Re-check bodies that were sent unchecked during an AI outage."""
    # JUSTIFIED allow_unscoped: sweeper across workspaces.
    rows = (
        (
            await session.execute(
                sa.select(TextVerdict)
                .where(TextVerdict.source == "unchecked")
                .limit(limit)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    flagged = 0
    for verdict in rows:
        org_id = verdict.org_id
        set_org_context(session, org_id)
        sample = (
            (
                await session.execute(
                    sa.select(Message)
                    .where(Message.org_id == org_id, Message.direction == "outbound")
                    .order_by(Message.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        body = next(
            (m.body for m in sample if m.body and body_hash(m.body) == verdict.body_hash), None
        )
        if body is None:
            await session.delete(verdict)
            await session.commit()
            continue
        rules = monitor_rules.evaluate(body, trusted_hosts=trusted_hosts(settings))
        try:
            screening, confidence, tokens = await ask_ai(session, settings, org_id, body, rules)
        except ai_guard.AIUnavailable:
            break
        await _store(
            session, org_id, verdict.body_hash, screening, confidence=confidence, tokens=tokens
        )
        if screening.action != "allow":
            flagged += 1
            await monitor_score.add_signal(
                session,
                settings,
                org_id,
                "text_unchecked_flagged",
                f"A text sent during an AI outage was harmful: {screening.reason}",
                detail={"category": screening.category, "body_hash": verdict.body_hash},
            )
        await session.commit()
    return flagged
