"""Template cohorts: the unit of analysis for a scammer hiding inside real traffic.

THE PROBLEM. A verified customer sends hundreds of genuine texts and slips 3-4 scams in
among them. At that prevalence (~1%) asking "is THIS message a scam?" is the wrong
question: a classifier with even a 2% false-positive rate produces more false flags than
true ones over 400 messages, and that noise IS the operator's workload. Improving the
classifier mostly buys more noise.

THE SHIFT. A scam campaign is a TEMPLATE sent to MANY STRANGERS. So group the day's
outbound by near-duplicate body and score the groups. A plumber has three or four cohorts,
each going to people they have messaged before. The scam is a NEW cohort going to numbers
this workspace has never contacted. Four scams among four hundred messages stop being four
anomalies in four hundred and become one cohort out of five — which is a question the AI can
answer confidently, and which costs ~5 AI calls a day instead of ~400.

WHY NORMALISE FIRST. Real templates are personalised: "Hi Dave, your parcel..." and
"Hi Sara, your parcel..." must land in ONE cohort, or the whole idea collapses back to
per-message analysis. The existing `monitor_text.body_hash` is an exact digest and cannot do
this - it is a cache key, not a clustering key. So names, numbers, links and emails are
replaced by placeholders first, and Jaccard overlap tolerates what is left over.

WHAT THIS MODULE DOES NOT DO. It does not judge. It produces cohorts and the cheap
behavioural facts about them (how many recipients are strangers, whether anyone replied,
whether messages bounced). Judgement belongs to the AI, which gets a cohort plus the
workspace's own declared business and is asked whether the two are consistent - a relative
question it answers well - rather than "is this a scam", which it answers badly at 1%.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import Message, MessageThread

#: Two bodies are the same template when their normalised shingle sets overlap at least this
#: much (Jaccard). Generous on purpose: over-merging two genuine templates costs one extra AI
#: call, while under-merging splits a scam campaign into singletons and destroys the signal
#: entirely. Asymmetric costs, so err towards merging.
DEFAULT_SIMILARITY = 0.55
#: Word n-gram width. Shingles rather than single words so that word ORDER matters - two
#: messages with the same vocabulary but different structure are different templates.
#: Width 2 rather than 3: personalisation changes ONE word, and the narrower the window the
#: fewer shingles that word can contaminate.
SHINGLE = 2
#: A cohort smaller than this is not a campaign. Kept as a parameter because a scammer who
#: learns the threshold would send batches just under it.
MIN_COHORT = 2

_URL = re.compile(
    r"\bhttps?://\S+|\b[a-z0-9-]+\.(?:com|net|org|io|co|uk|link|xyz|top)\b/?\S*", re.I
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
#: NO word boundary on the tail: "3pm", "DHL-0" and "ref99231" all carry a varying number
#: glued to a letter, and `\b\d+\b` misses every one of them - which split one template into
#: seven cohorts the first time this ran.
_NUM = re.compile(r"\+?\d[\d\s().-]{4,}\d|\d+")
_MONEY = re.compile(r"[$£€]\s?\d[\d,.]*")
#: Weekdays, months and am/pm are merge fields in disguise. The single most common
#: legitimate template in this product is an appointment reminder, and without this
#: "confirmed for tuesday at 9am" and "confirmed for friday at 2pm" land 0.55 apart on a
#: short body and split into two cohorts - which would mean paying for two AI calls to ask
#: the same question about the same template, every day, for every business that books
#: appointments. Collapsing the day and the meridiem is cheaper and safer than loosening the
#: similarity threshold, which would start merging genuinely different templates.
_WHEN = re.compile(
    r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\b(mon|tue|wed|thu|fri|sat|sun)\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(am|pm)\b|\b(today|tomorrow|tonight|yesterday)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s<>]+")
_WS = re.compile(r"\s+")


def normalise(body: str) -> str:
    """Collapse the parts that vary per recipient so one template gives one fingerprint.

    Order matters: money before numbers (so "£50" does not become "<num>" and lose that it
    was a currency amount), and URLs before numbers (so a link's digits do not survive).
    """
    text = (body or "").lower()
    text = _URL.sub(" <url> ", text)
    text = _EMAIL.sub(" <email> ", text)
    text = _MONEY.sub(" <money> ", text)
    text = _NUM.sub(" <num> ", text)
    text = _WHEN.sub(" <when> ", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _shingles(text: str, width: int = SHINGLE) -> list[str]:
    words = text.split()
    if len(words) < width:
        return [" ".join(words)] if words else []
    return [" ".join(words[i : i + width]) for i in range(len(words) - width + 1)]


def shingle_set(body: str, *, width: int = SHINGLE) -> frozenset[str]:
    """The comparable shape of a body: normalised word n-grams, as a set."""
    return frozenset(_shingles(normalise(body), width))


def fingerprint(body: str, *, width: int = SHINGLE) -> str:
    """A stable id for a template, for storage and for showing an operator that two cohorts
    a week apart are the same campaign. Derived from the normalised shingle set, so
    personalisation does not change it."""
    shingles = sorted(shingle_set(body, width=width))
    if not shingles:
        return ""
    return hashlib.blake2b("\x1f".join(shingles).encode(), digest_size=8).hexdigest()


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of two shingle sets.

    WHY JACCARD AND NOT SIMHASH, since SimHash is the usual answer for near-duplicates.
    SimHash earns its keep at a scale where you need LSH banding to avoid comparing
    everything to everything. Here a workspace's day is a few thousand messages against a
    handful of templates, so the comparison is already cheap - and SimHash pays for that
    scalability with brittleness exactly where this problem lives: personalisation. One
    substituted word ("Hi Dave" / "Hi Sara") moves a fixed fraction of the hash bits, and
    the Hamming threshold that tolerates it also merges genuinely different templates. The
    first version of this module used SimHash with a 12-bit threshold and split ONE scam
    template into four cohorts, which is the failure that loses the signal completely.
    Jaccard degrades gracefully instead: one word out of nine is a visible, bounded,
    explainable drop in overlap, and an operator can be shown the number.
    """
    if not a or not b:
        return 1.0 if a == b else 0.0
    return len(a & b) / len(a | b)


@dataclass
class Cohort:
    """One template's worth of outbound traffic."""

    fingerprint: str
    #: The representative's shingle set — what later messages are compared against.
    shingles: frozenset[str] = frozenset()
    message_ids: list[uuid.UUID] = field(default_factory=list)
    recipients: set[str] = field(default_factory=set)
    #: The first body seen, kept verbatim for the operator and the AI. Never normalised -
    #: a decision pack must show what was actually sent.
    sample_body: str = ""
    first_at: datetime | None = None
    last_at: datetime | None = None

    @property
    def size(self) -> int:
        return len(self.message_ids)

    @property
    def recipient_count(self) -> int:
        return len(self.recipients)


def cluster(
    rows: Iterable[tuple[uuid.UUID, str, str, datetime]],
    *,
    min_similarity: float = DEFAULT_SIMILARITY,
) -> list[Cohort]:
    """Group `(message_id, body, to_e164, created_at)` rows into template cohorts.

    Greedy single-pass against cohort representatives. O(n * cohorts) rather than O(n^2),
    which matters because a busy workspace's day is thousands of messages and only a
    handful of templates. Cohorts come back largest first - the operator's attention and
    the AI budget should both go to the biggest campaign first.
    """
    cohorts: list[Cohort] = []
    for message_id, body, to_e164, created_at in rows:
        shingles = shingle_set(body or "")
        match = None
        best = min_similarity
        for cohort in cohorts:
            s = similarity(cohort.shingles, shingles)
            if s >= best:
                match, best = cohort, s
        if match is None:
            match = Cohort(
                fingerprint=fingerprint(body or ""), shingles=shingles, sample_body=body or ""
            )
            cohorts.append(match)
        match.message_ids.append(message_id)
        if to_e164:
            match.recipients.add(to_e164)
        if created_at is not None:
            if match.first_at is None or created_at < match.first_at:
                match.first_at = created_at
            if match.last_at is None or created_at > match.last_at:
                match.last_at = created_at
    cohorts.sort(key=lambda c: c.size, reverse=True)
    return cohorts


@dataclass
class CohortMetrics:
    """Cheap behavioural facts. None of these needs an AI call, and together they are a
    stronger separator than the message text - a blast to strangers looks nothing like a
    plumber confirming appointments, whatever the words say."""

    #: Recipients this workspace had never messaged before this cohort. A blast is ~1.0;
    #: a business talking to its own customers is near 0.
    first_contact_ratio: float = 0.0
    #: Recipients who replied at all. Real traffic gets replies; a blast gets silence.
    reply_rate: float = 0.0
    #: Messages the carrier could not deliver. High on a purchased list full of dead
    #: numbers, low on a real customer base.
    undelivered_rate: float = 0.0
    #: Distinct area codes / country prefixes. A local trade serves a few; a list spans many.
    spread: int = 0
    strangers: int = 0
    replied: int = 0
    undelivered: int = 0


def _prefix(e164: str) -> str:
    """Coarse geography without a phonenumbers round-trip per recipient: +1 keeps the NPA,
    everything else keeps the country code and two digits. Only used as a spread count."""
    digits = re.sub(r"\D", "", e164 or "")
    if not digits:
        return ""
    if digits.startswith("1") and len(digits) >= 4:
        return "1-" + digits[1:4]
    return digits[:4]


async def metrics(
    session: AsyncSession,
    org_id: uuid.UUID,
    cohort: Cohort,
    *,
    now: datetime | None = None,
) -> CohortMetrics:
    """Behavioural facts for one cohort. Three aggregate queries, no per-recipient loop."""
    set_org_context(session, org_id)
    out = CohortMetrics(spread=len({_prefix(r) for r in cohort.recipients if r}))
    recipients = [r for r in cohort.recipients if r]
    if not recipients:
        return out
    started = cohort.first_at or (now or datetime.now(timezone.utc))

    # FIRST CONTACT. A stranger is a recipient with no OUTBOUND message from this workspace
    # before this cohort began. Counted from messages rather than from threads because a
    # thread can be created by an inbound message, and someone who texted US first is not a
    # stranger - they are a lead.
    known = set(
        (
            await session.execute(
                sa.select(Message.to_e164)
                .where(
                    Message.org_id == org_id,
                    Message.direction == "outbound",
                    Message.to_e164.in_(recipients),
                    Message.created_at < started,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    out.strangers = len([r for r in recipients if r not in known])
    out.first_contact_ratio = out.strangers / len(recipients)

    # REPLIES. `first_response_at` is the agent's reply, not the contact's, so count
    # inbound messages instead.
    replied = set(
        (
            await session.execute(
                sa.select(MessageThread.contact_e164)
                .join(Message, Message.thread_id == MessageThread.id)
                .where(
                    MessageThread.org_id == org_id,
                    MessageThread.contact_e164.in_(recipients),
                    Message.direction == "inbound",
                    Message.created_at >= started,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    out.replied = len(replied)
    out.reply_rate = out.replied / len(recipients)

    # UNDELIVERED. Dead numbers are the signature of a purchased list.
    out.undelivered = int(
        (
            await session.execute(
                sa.select(sa.func.count(Message.id)).where(
                    Message.org_id == org_id,
                    Message.id.in_(cohort.message_ids),
                    Message.status.in_(("failed", "undelivered")),
                )
            )
        ).scalar_one()
        or 0
    )
    if cohort.size:
        out.undelivered_rate = out.undelivered / cohort.size
    return out


async def build(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    since: datetime | None = None,
    hours: int = 24,
    min_similarity: float = DEFAULT_SIMILARITY,
    min_size: int = MIN_COHORT,
    limit: int = 5000,
) -> list[tuple[Cohort, CohortMetrics]]:
    """Cohorts for one workspace's recent outbound, largest first, with their metrics.

    `min_size` drops singletons: a one-off message to one person is a conversation, not a
    campaign, and paying for an AI call on it is the per-message trap this module exists to
    escape. Singletons still reach the existing per-message screen - this is an additional
    lens, not a replacement for it.
    """
    now = datetime.now(timezone.utc)
    start = since or (now - timedelta(hours=hours))
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(Message.id, Message.body, Message.to_e164, Message.created_at)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.created_at >= start,
            )
            .order_by(Message.created_at)
            .limit(limit)
        )
    ).all()
    cohorts = [c for c in cluster(rows, min_similarity=min_similarity) if c.size >= min_size]
    out: list[tuple[Cohort, CohortMetrics]] = []
    for cohort in cohorts:
        out.append((cohort, await metrics(session, org_id, cohort, now=now)))
    return out


def looks_like_a_campaign(cohort: Cohort, m: CohortMetrics) -> bool:
    """A cheap pre-filter for which cohorts are worth an AI call at all.

    Deliberately NOT a scam verdict - plenty of legitimate traffic is a campaign, and an
    appointment-reminder blast to known customers trips none of this. The point is to spend
    the AI budget on cohorts that are going to STRANGERS, because that is the shape a scam
    has to have: you cannot defraud your own customer list twice.
    """
    if cohort.recipient_count < MIN_COHORT:
        return False
    if m.first_contact_ratio >= 0.8 and m.reply_rate <= 0.05:
        return True
    if m.undelivered_rate >= 0.2:
        return True
    return m.first_contact_ratio >= 0.5 and m.spread >= 5
