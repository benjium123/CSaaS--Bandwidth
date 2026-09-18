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
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import Message, MessageThread

log = structlog.get_logger("monitor_cohorts")

#: Two bodies are the same template when their normalised shingle sets overlap at least this
#: much (Jaccard). MEASURED rather than guessed - the first value, 0.55, was above the floor
#: for real personalised traffic and silently split templates apart:
#:     same template, name swap, short body   0.43   <- the binding case
#:     same template, name swap, long body    0.71
#:     same template, day and time swap       1.00
#:     same template, two names swapped       0.50
#:     DIFFERENT: appointment vs parcel scam  0.00
#:     DIFFERENT: invoice vs bank scam        0.00
#:     DIFFERENT: two genuine templates       0.00
#: A wide empty gap between 0.43 and 0.00, so 0.30 sits in the middle with margin on both
#: sides. Note the cost asymmetry is NOT the naive one: over-merging used to be "one wasted
#: AI call", but a merged-in scam was never shown to the model at all until `bodies_for_review`
#: started sending the least-similar members too. Both directions lose the signal, so the
#: threshold belongs in the measured gap rather than at either edge.
DEFAULT_SIMILARITY = 0.30
#: Word n-gram width. Shingles rather than single words so that word ORDER matters - two
#: messages with the same vocabulary but different structure are different templates.
#: Width 2 rather than 3: personalisation changes ONE word, and the narrower the window the
#: fewer shingles that word can contaminate.
SHINGLE = 2
#: A cohort smaller than this is not a campaign. Kept as a parameter because a scammer who
#: learns the threshold would send batches just under it.
MIN_COHORT = 2
#: How many bodies from one cohort the AI is shown. One was a silent-miss bug: a scam that
#: merged into a legitimate cohort was never put in front of the model at all.
_MAX_SAMPLES = 3

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
#: ANCHORED, and the abbreviations are curated. The first version ended each month prefix with
#: `[a-z]*`, which ate every word starting with those letters: "marketing", "maybe", "decide",
#: "DECLINED", "junk", "separate" and "novel" all normalised to "<when>", so "Your payment was
#: declined" became "your payment was <when>" - deleting scam vocabulary from the very text
#: this module exists to characterise.
#: Bare "mar", "may", "wed", "sat" and "sun" are DELIBERATELY absent: each is an ordinary
#: English word. Losing a little clustering power on "Sat 3pm" is far cheaper than losing the
#: words "may", "sat" and "declined" from every message.
_WHEN = re.compile(
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b(?:january|february|march|april|june|july|august|september|october|november|december)\b"
    r"|\b(?:jan|feb|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b"
    r"|\b(?:mon|tue|thu|thur|fri)\b"
    r"|\b(?:am|pm)\b|\b(?:today|tomorrow|tonight|yesterday)\b",
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
    #: MORE than the seed, and the reason is a real hole that review by a second reader found:
    #: `review_one` used to send only `sample_body`, so anything that merged into a cohort was
    #: never shown to the model at all - the model answered about the seed. Over-merging was
    #: therefore not "one wasted call", it was a silent miss. These are the members LEAST like
    #: the seed, which is exactly where a merged-in scam would be.
    samples: list[str] = field(default_factory=list)
    #: Shingles EVERY member shares. The basis for the stable fingerprint; see cluster().
    shared: frozenset[str] = frozenset()
    first_at: datetime | None = None
    last_at: datetime | None = None

    @property
    def size(self) -> int:
        return len(self.message_ids)

    @property
    def recipient_count(self) -> int:
        return len(self.recipients)

    def bodies_for_review(self, limit: int = 3) -> list[str]:
        """The seed plus the least-similar members, de-duplicated, for the AI and the operator."""
        out = [self.sample_body] if self.sample_body else []
        for body in self.samples:
            if len(out) >= limit:
                break
            if body and body not in out:
                out.append(body)
        return out


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

    ORDER-DEPENDENT, deliberately and with a limit. Representatives are never updated after
    creation, so with A~B and B~C both above the threshold but A~C below it, the grouping
    depends on arrival order. That is acceptable for deciding what to look at; it is NOT
    acceptable as a durable key, which is why `fingerprint` is recomputed from the settled
    cohort's shared shingles below rather than taken from whichever message happened to
    arrive first.
    """
    cohorts: list[Cohort] = []
    seeds: dict[int, frozenset[str]] = {}
    for message_id, body, to_e164, created_at in rows:
        shingles = shingle_set(body or "")
        match = None
        best = min_similarity
        for cohort in cohorts:
            s = similarity(cohort.shingles, shingles)
            if s >= best:
                match, best = cohort, s
        if match is None:
            match = Cohort(shingles=shingles, sample_body=body or "", fingerprint="")
            cohorts.append(match)
            seeds[id(match)] = shingles
            match.shared = shingles
        else:
            # Keep the members LEAST like the seed. A scam that merged in is by definition the
            # least similar member, and it is what the AI must be shown.
            seed = seeds.get(id(match), match.shingles)
            if body and similarity(seed, shingles) < 0.95:
                match.samples.append(body)
                if len(match.samples) > _MAX_SAMPLES * 4:
                    match.samples = sorted(
                        match.samples, key=lambda b: similarity(seed, shingle_set(b))
                    )[:_MAX_SAMPLES]
            # The shingles every member shares - a stable identity for the campaign.
            match.shared = match.shared & shingles if match.shared else shingles
        match.message_ids.append(message_id)
        if to_e164:
            match.recipients.add(to_e164)
        if created_at is not None:
            if match.first_at is None or created_at < match.first_at:
                match.first_at = created_at
            if match.last_at is None or created_at > match.last_at:
                match.last_at = created_at

    for cohort in cohorts:
        seed = seeds.get(id(cohort), cohort.shingles)
        cohort.samples = sorted(
            set(cohort.samples), key=lambda b: similarity(seed, shingle_set(b))
        )[:_MAX_SAMPLES]
        # STABLE id: hashed from the shingles every member of the settled cohort shares, so
        # personalisation and arrival order cannot change it. Seeding it from the first
        # message's full body meant the same template run on Monday and Friday got different
        # ids whenever the first recipient's name differed - which broke every use of it
        # (dedupe, "seen this campaign before", the operator's evidence blob).
        basis = cohort.shared or cohort.shingles
        cohort.fingerprint = (
            hashlib.blake2b("\x1f".join(sorted(basis)).encode(), digest_size=8).hexdigest()
            if basis
            else ""
        )
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


@dataclass
class CohortSet:
    """What a build produced, INCLUDING what it could not see.

    A bare list cannot say "I only read the most recent 5000 of your 12000 messages", and a
    caller that cannot say that will happily print "no campaign contradicts this business"
    off the back of a partial read.
    """

    cohorts: list[tuple[Cohort, CohortMetrics]] = field(default_factory=list)
    #: Messages actually loaded.
    scanned: int = 0
    #: True when the row cap was hit, so older traffic in the window was not read.
    truncated: bool = False
    #: Cohorts dropped for being one-offs. Reported because they are a known blind spot.
    singletons: int = 0

    def __iter__(self):
        return iter(self.cohorts)

    def __len__(self) -> int:
        return len(self.cohorts)


async def build(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    since: datetime | None = None,
    hours: int = 24,
    min_similarity: float = DEFAULT_SIMILARITY,
    min_size: int = MIN_COHORT,
    limit: int = 5000,
) -> CohortSet:
    """Cohorts for one workspace's recent outbound, largest first, with their metrics.

    NEWEST FIRST, and the truncation is reported. This used to read `ORDER BY created_at` ASC
    with a 5000 cap, which on a 30-day operator review of a busy account returned the OLDEST
    5000 messages: a customer sending 400/day fills the cap on day 13, so a review triggered by
    something that happened yesterday covered days 1-13 and never loaded yesterday at all. The
    scam cohort was not scored low or filtered out, it was never read. Proven on 5200 messages:
    one cohort, 5000 messages, the recent scam absent.
    `truncated` exists so the caller cannot present a partial read as a clean bill of health.

    `min_size` drops singletons, and that is a REAL limit on what this lens can see: four
    bespoke one-off impersonations are four singletons and are invisible here. It catches
    campaigns - the same template sent repeatedly - which is what a scammer running volume has
    to do, but the per-message screen remains the only thing looking at one-offs.
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
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()
    clustered = cluster(rows, min_similarity=min_similarity)
    kept = [c for c in clustered if c.size >= min_size]
    out: list[tuple[Cohort, CohortMetrics]] = []
    for cohort in kept:
        out.append((cohort, await metrics(session, org_id, cohort, now=now)))
    return CohortSet(
        cohorts=out,
        scanned=len(rows),
        truncated=len(rows) >= limit,
        singletons=len(clustered) - len(kept),
    )


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
    # NOT `m.spread >= 5`. `_prefix` buckets +1 numbers by area code (300+ buckets, so five is
    # trivial for a US blast) but every UK mobile is 4470-4479 - at most ten buckets, and a list
    # inside one or two networks gives a spread of 1. A GB scam blast to 200 strangers could
    # never trip this branch, which is the same NANP assumption the audit found in quiet hours.
    # Recipient COUNT is country-neutral; spread stays as evidence an operator reads, not a gate.
    return m.first_contact_ratio >= 0.5 and cohort.recipient_count >= 10


# --------------------------------------------------------------------------------------
# The AI question, asked of a COHORT rather than of a message
# --------------------------------------------------------------------------------------
#: Weight for a confident "this campaign is not what this business does". Deliberately below
#: `call_scam` (40) and above `text_blocked` (25): one such cohort should not pause an account
#: on its own, two should reach "restricted", three should reach the pause threshold. A
#: scammer running a campaign a day is stopped within days; a single ambiguous campaign is
#: not, which is the trade the operator's review budget actually cares about.
#: The one signal kind this module writes. A constant because the dedupe query has to match
#: exactly what the writers write, and two string literals drift.
SIGNAL_KIND = "cohort_inconsistent"
WEIGHT_INCONSISTENT = 35
WEIGHT_INCONSISTENT_WEAK = 15
#: Below this the model is guessing, and a guess at 1% prevalence is noise.
CONFIDENT = 70

COHORT_SYSTEM = """You are reviewing a CAMPAIGN: a batch of messages one business sent,
all variations of a single template, to many recipients. You are told what the
business said it does when it signed up, the template itself, and how the batch behaved.

Your question is CONSISTENCY, not suspicion: is this campaign the kind of messaging this
business would send?

Judge carefully:
- Sending to people who have not been contacted before is NOT by itself wrongdoing.
  Legitimate marketing, recruitment and delivery notifications all do it.
- What matters is whether the CONTENT fits the declared business, and whether it is of a kind
  used to defraud: impersonating a bank, courier, tax office or government; demanding a fee to
  release a parcel, refund or account; or driving recipients to a link that collects
  credentials or card details.
- A plumber sending appointment reminders is consistent. The same plumber sending parcel-fee
  notices is not, however ordinary the words look on their own.

Return JSON only:
{"verdict": "consistent" | "unclear" | "inconsistent",
 "confidence": 0-100,
 "category": "scam" | "phishing" | "impersonation" | "unrelated_business" | "none",
 "impersonates": "who it pretends to be, or null",
 "reason": "one sentence an operator can act on"}"""


@dataclass(frozen=True)
class CohortReview:
    verdict: str
    confidence: int
    category: str
    reason: str
    impersonates: str | None = None
    tokens: tuple[int, int] = (0, 0)

    @property
    def actionable(self) -> bool:
        return self.verdict == "inconsistent"


async def review_one(settings, cohort: Cohort, m: CohortMetrics, business: dict) -> CohortReview:
    """One AI call for one cohort. No database, so an exam or a canary can drive exactly the
    judgement live traffic gets - the same property `judge_text` has."""
    from app.services import ai_guard

    user = "\n\n".join(
        [
            ai_guard.data_block("business", business),
            # EVERY sample, not just the seed: a scam that merged into a legitimate cohort
            # was otherwise never put in front of the model at all.
            ai_guard.data_block(
                "messages_from_this_campaign",
                [b[:1000] for b in cohort.bodies_for_review()],
            ),
            ai_guard.data_block(
                "how_the_batch_behaved",
                {
                    "messages": cohort.size,
                    "recipients": cohort.recipient_count,
                    "never_contacted_before": f"{m.first_contact_ratio:.0%}",
                    "recipients_who_replied": f"{m.reply_rate:.0%}",
                    "undelivered": f"{m.undelivered_rate:.0%}",
                    "distinct_area_codes": m.spread,
                },
            ),
            "Give your verdict on this campaign.",
        ]
    )
    judgement = await ai_guard.judge(
        settings,
        task="cohort_check",
        system=COHORT_SYSTEM,
        user=user,
        max_tokens=250,
        timeout=settings.monitor_text_ai_timeout_seconds,
    )
    verdict = judgement.data.get("verdict")
    if verdict not in ("consistent", "unclear", "inconsistent"):
        raise ai_guard.AIUnavailable("AI gave no valid cohort verdict")
    try:
        confidence = int(judgement.data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0
    impersonates = judgement.data.get("impersonates")
    return CohortReview(
        verdict=verdict,
        confidence=max(0, min(100, confidence)),
        category=str(judgement.data.get("category") or "none")[:32],
        reason=str(judgement.data.get("reason") or "")[:255],
        impersonates=str(impersonates)[:64] if impersonates else None,
        tokens=(judgement.tokens_in, judgement.tokens_out),
    )


async def signalled_fingerprints(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    since: datetime,
    limit: int = 500,
) -> set[str]:
    """Templates already recorded as inconsistent for this account inside the window.

    WHY THIS IS NECESSARY, not a nicety. The score is a rolling sum over
    `monitor_signal_window_days` with no decay, and the sweep looks back 24 hours every hour.
    Without this, ONE scam campaign is re-reviewed and re-signalled on every pass: 24 hours of
    overlap x 35 points crosses the pause threshold three times over, from a single campaign,
    by repetition alone. The operator then reads a queue showing twenty-four campaigns where
    there was one, which is exactly the false volume this whole design exists to remove.

    DELIBERATELY NOT narrowed by `state.cleared_before`. The score window is truncated there,
    but the dedupe must not be: when an operator rejects a recommendation ("I checked, this is
    their real business"), the signals stop counting - and if the next sweep were free to
    re-signal the same template, the recommendation would be back within the hour and the
    operator's decision would be undone by the arithmetic. Spanning the full window means a
    rejected template stays quiet for the window, which is what "I checked this" should buy.

    Costs one small query per account per pass. The rows are AI-confirmed findings, so there
    are a handful per account, not thousands; `limit` is a guard, not a working constraint.
    """
    from app.models import MonitorSignal

    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(MonitorSignal.detail)
                .where(
                    MonitorSignal.org_id == org_id,
                    MonitorSignal.kind == SIGNAL_KIND,
                    MonitorSignal.created_at >= since,
                )
                .order_by(MonitorSignal.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # Read in Python rather than with a JSON path predicate: `detail` is PortableJSON, which is
    # a real JSONB on Postgres and a TEXT column on SQLite, and a query that works on one
    # silently matches nothing on the other.
    return {
        d["fingerprint"]
        for d in rows
        if isinstance(d, dict) and d.get("fingerprint")
    }


async def shared_recipient_count(
    session: AsyncSession,
    org_id: uuid.UUID,
    recipients: Iterable[str],
    *,
    since: datetime,
    sample: int = 500,
) -> dict:
    """How many of this campaign's recipients other workspaces also cold-contacted. COUNTS ONLY.

    WHY THIS EXISTS. Everything else in this module looks at one account, and one account is
    exactly what a serious operation does not confine itself to: buy three workspaces, split the
    same victim list across them, and each account on its own looks like a modest business
    sending a modest campaign. The overlap is the only thing that betrays it, and it is
    structurally invisible to a per-account lens however good that lens is.

    WHAT IT DELIBERATELY DOES NOT RETURN. Never the other workspaces' identities, never their
    message bodies, never which of their numbers sent what - a count, and a count of distinct
    others at the worst-overlapping recipient. An operator learns "this list is shared" and can
    then look at each account through its own audited endpoint, which is where the cross-tenant
    boundary belongs. Handing one customer's traffic to another customer's case file would make
    this the platform's own tenancy leak, dressed up as a safety feature.

    WHY IT IS EVIDENCE AND NOT A SCORE. A shared list is not proof of anything: bought lead
    lists are legal and ordinary, and a consumer legitimately hears from several businesses.
    Weighting this into the risk score would score customers for their SUPPLIER's behaviour. So
    it lands in the report for a person to read, and nothing here calls `add_signal`.
    """
    numbers = [n for n in list(dict.fromkeys(recipients))[:sample] if n]
    if not numbers:
        return {"recipients_checked": 0, "also_contacted_elsewhere": 0, "other_workspaces": 0}

    # JUSTIFIED allow_unscoped: this question is cross-tenant BY DEFINITION - "did anyone else
    # message these numbers" cannot be asked inside one org's scope. Bounded to be safe to run
    # unscoped: it selects no bodies and no org ids, aggregates to counts in the database, and
    # the only rows that can influence the answer are ones whose recipient this org already
    # knows, because it messaged them itself.
    from app.db.base import ALLOW_UNSCOPED_KEY

    rows = (
        await session.execute(
            sa.select(
                Message.to_e164,
                sa.func.count(sa.distinct(Message.org_id)).label("others"),
            )
            .where(
                Message.to_e164.in_(numbers),
                Message.org_id != org_id,
                Message.direction == "outbound",
                Message.created_at >= since,
            )
            .group_by(Message.to_e164)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    return {
        "recipients_checked": len(numbers),
        "also_contacted_elsewhere": len(rows),
        # The worst single recipient, which is what says "list" rather than "coincidence": one
        # number reached by four other workspaces in a month is not two businesses sharing a
        # customer.
        "other_workspaces": max((int(n) for _, n in rows), default=0),
        "since": since.isoformat(),
    }


async def active_org_ids(
    session: AsyncSession, *, since: datetime, limit: int = 5000
) -> list[uuid.UUID]:
    """Workspaces with outbound texts in the window.

    The cap is 5000, not 500. The operator's stated book is 100-1000 customers, and a 500-row
    LIMIT with no ORDER BY would have swept an arbitrary, database-chosen half of them while
    the other half was never looked at - and reported nothing unusual, because from inside the
    sweep 500 orgs with no findings looks identical to 500 orgs that are clean. Ids only, one
    group-by, so 5000 rows is free; the cap exists to bound memory, not to sample. Hitting it
    is logged as a WARNING because at that point the sweep has stopped being complete.
    """
    # JUSTIFIED allow_unscoped: a platform-wide sweep that RESOLVES which orgs to look at, so
    # it cannot be org-scoped by construction. Returns ids only - no message content crosses a
    # tenant boundary here, and every later query is scoped to one org at a time.
    from app.db.base import ALLOW_UNSCOPED_KEY

    rows = (
        (
            await session.execute(
                sa.select(Message.org_id)
                .where(Message.direction == "outbound", Message.created_at >= since)
                .group_by(Message.org_id)
                .limit(limit)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    if len(rows) >= limit:
        log.warning("cohort_sweep_org_cap_reached", cap=limit, since=since.isoformat())
    return list(rows)


async def tick(
    session: AsyncSession,
    settings,
    *,
    hours: int = 24,
    now: datetime | None = None,
) -> dict:
    """Review each workspace's recent campaigns and record a signal for the bad ones.

    The load property this exists for: one AI call per CAMPAIGN that reaches the pre-filter,
    not one per message. A workspace sending 400 texts across five templates, of which one
    looks like a blast to strangers, costs ONE call - and that call carries the whole batch's
    behaviour, which is what lets the model answer confidently instead of guessing.

    An AI outage is not an enforcement decision: a cohort that cannot be reviewed is counted
    and left alone. The per-message screen still runs independently of this.
    """
    from app.services import ai_guard, monitor_score, monitor_text

    started = now or datetime.now(timezone.utc)
    since = started - timedelta(hours=hours)
    out = {"orgs": 0, "cohorts": 0, "reviewed": 0, "signals": 0, "unavailable": 0, "repeats": 0}
    if not settings.monitor_enforced or not ai_guard.is_available(settings):
        return out

    for org_id in await active_org_ids(session, since=since):
        out["orgs"] += 1
        built = await build(session, org_id, since=since)
        out["cohorts"] += len(built)
        worth_asking = [(c, m) for c, m in built if looks_like_a_campaign(c, m)]
        if not worth_asking:
            continue
        # Templates already on the record. Checked BEFORE the AI call, not after: re-reviewing a
        # campaign we already ruled on costs money and answers a question nobody asked.
        seen = await signalled_fingerprints(
            session,
            org_id,
            since=started - timedelta(days=settings.monitor_signal_window_days),
        )
        fresh = []
        pass_seen: set[str] = set()
        for c, m in worth_asking:
            if c.fingerprint in seen:
                out["repeats"] += 1
                # A collision INSIDE one pass is a different event from "we ruled on this
                # yesterday", and lumping both into `repeats` would hide it. Two distinct
                # campaigns converging on one id means the second is never signalled, which is
                # rare, harmless to the score, and exactly the kind of silence that turns into an
                # unexplained miss six months later. Logged loudly rather than fixed: merging the
                # two would need an id that is not derived from shared shingles, and the stable
                # id is worth more than this edge.
                if c.fingerprint in pass_seen:
                    out["collisions"] = out.get("collisions", 0) + 1
                    log.warning(
                        "cohort_fingerprint_collision",
                        org_id=str(org_id),
                        fingerprint=c.fingerprint,
                        messages=c.size,
                        sample=c.sample_body[:120],
                    )
                continue
            # Added as we go, so one pass cannot signal the same template twice. Two cohorts in
            # a single build CAN share a fingerprint: it is recomputed from the shingle
            # INTERSECTION after clustering, so two clusters that started apart can collapse to
            # the same id, and only this line keeps that from double-scoring.
            seen.add(c.fingerprint)
            pass_seen.add(c.fingerprint)
            fresh.append((c, m))
        if not fresh:
            continue
        business = await monitor_text.business_context(session, org_id)
        for cohort, m in fresh:
            try:
                review = await review_one(settings, cohort, m, business)
            except ai_guard.AIUnavailable:
                out["unavailable"] += 1
                continue
            out["reviewed"] += 1
            if not review.actionable:
                continue
            weight = (
                WEIGHT_INCONSISTENT
                if review.confidence >= CONFIDENT
                else WEIGHT_INCONSISTENT_WEAK
            )
            await monitor_score.add_signal(
                session,
                settings,
                org_id,
                "cohort_inconsistent",
                f"Campaign of {cohort.size} messages does not match this business: {review.reason}",
                # Everything an operator needs to decide WITHOUT opening the message log.
                detail={
                    "fingerprint": cohort.fingerprint,
                    "template": cohort.sample_body[:500],
                    "messages": cohort.size,
                    "recipients": cohort.recipient_count,
                    "first_contact_ratio": round(m.first_contact_ratio, 3),
                    "reply_rate": round(m.reply_rate, 3),
                    "undelivered_rate": round(m.undelivered_rate, 3),
                    "spread": m.spread,
                    "category": review.category,
                    "impersonates": review.impersonates,
                    "confidence": review.confidence,
                    "reason": review.reason,
                },
                weight=weight,
            )
            out["signals"] += 1
        await session.commit()
    return out
