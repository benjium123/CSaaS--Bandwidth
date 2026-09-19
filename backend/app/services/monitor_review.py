"""On-demand review of ONE account, run because an operator asked for it.

WHY THIS IS NOT JUST THE HOURLY SWEEP WITH A BUTTON. The sweep is tuned for cost across every
workspace: 24 hours of traffic, and only the cohorts that pass a cheap pre-filter get an AI
call. An operator asking "review this account thoroughly" has already decided this one account
is worth money, so the trade inverts — look back weeks instead of a day, review EVERY campaign
rather than only the suspicious-looking ones, and put the whole picture in one place.

IT TAKES NO ACTION. It records what it learns as signals, so the score reflects the review, and
it returns a report. It never changes the account's level, never pauses, never bans. That is
the operator's decision and this function deliberately cannot make it - see
`monitor_score.recompute`, where automatic restriction is off by default.

WHY A REPORT RATHER THAN A VERDICT. With a thousand customers the scarce resource is operator
attention, and attention is spent on READING, not on clicking. So the report is ordered to be
read top-down: what the monitor currently thinks, then the campaigns with the AI's reasoning,
then the raw behavioural facts underneath. Someone should be able to decide from the first
screen and drill down only when they disagree.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import (
    Call,
    KycProfile,
    Message,
    MonitorHealth,
    MonitorSignal,
    Org,
    OrgMonitoring,
)
from app.services import monitor_cohorts, monitor_score

log = structlog.get_logger("monitor_review")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def traffic_summary(session: AsyncSession, org_id: uuid.UUID, since: datetime) -> dict:
    """Volume facts, cheap. Context for everything else: 4 strangers out of 20 messages is a
    different account from 4 out of 4,000, and the raw score cannot tell you which."""
    set_org_context(session, org_id)
    texts = (
        await session.execute(
            sa.select(
                Message.direction,
                sa.func.count(Message.id),
                sa.func.count(sa.distinct(Message.to_e164)),
            )
            .where(Message.org_id == org_id, Message.created_at >= since)
            .group_by(Message.direction)
        )
    ).all()
    by_direction = {d: {"messages": n, "recipients": r} for d, n, r in texts}
    calls = (
        await session.execute(
            sa.select(Call.direction, sa.func.count(Call.id))
            .where(Call.org_id == org_id, Call.created_at >= since)
            .group_by(Call.direction)
        )
    ).all()
    return {
        "outbound_texts": by_direction.get("outbound", {}).get("messages", 0),
        "outbound_recipients": by_direction.get("outbound", {}).get("recipients", 0),
        "inbound_texts": by_direction.get("inbound", {}).get("messages", 0),
        "calls": dict(calls),
    }


async def recent_signals(
    session: AsyncSession, org_id: uuid.UUID, since: datetime, *, limit: int = 50
) -> list[dict]:
    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(MonitorSignal)
                .where(MonitorSignal.org_id == org_id, MonitorSignal.created_at >= since)
                .order_by(MonitorSignal.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "kind": s.kind,
            "weight": s.weight,
            "summary": s.summary,
            "at": s.created_at.isoformat() if s.created_at else None,
            "detail": s.detail,
        }
        for s in rows
    ]


async def review_account(
    session: AsyncSession,
    settings,
    org_id: uuid.UUID,
    *,
    days: int | None = None,
    thorough: bool = True,
    record_signals: bool = True,
    touch_state: bool = True,
) -> dict:
    """Assemble the whole picture for one account, and review its campaigns with the AI.

    `thorough=True` (what an operator's button means) reviews EVERY campaign, not only the ones
    that trip the cheap pre-filter, because the pre-filter exists to save money on a sweep the
    operator did not ask for. `thorough=False` matches the sweep's behaviour, which is useful
    for comparing "what would the sweep have seen" against "what is actually there".

    `touch_state=False` reads the monitoring row without creating one. It exists for
    `audit_sample`, which promises to leave no trace on an account it merely sampled: with the
    default, `get_state(create=True)` INSERTs an empty `OrgMonitoring` row for any account that
    has never been monitored, and that row's `created_at` is then a record of the moment the
    account was audited - "was I sampled?" answerable from the database, off a function whose
    docstring said it wrote nothing. The signal suppression (`record_signals=False`) never
    covered this, because the row is written by the READ.
    """
    from app.services import ai_guard, monitor_text

    window = days or settings.monitor_review_window_days
    since = _now() - timedelta(days=window)
    set_org_context(session, org_id)

    org = await session.get(Org, org_id)
    if org is None:
        from app.errors import NotFoundError

        raise NotFoundError("Organization not found")
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    state = await monitor_score.get_state(session, org_id, create=touch_state)

    report: dict = {
        "account": {
            "org_id": str(org_id),
            "name": org.name,
            "verification_status": profile.status if profile else None,
            "declared_business": await monitor_text.business_context(session, org_id),
        },
        # `state` is None only under `touch_state=False` for an account that has never been
        # monitored - which is exactly what "the monitor has no opinion" looks like, so it reads
        # as the defaults rather than as missing data.
        "monitor": {
            "level": state.level if state is not None else "normal",
            "score": state.score if state is not None else 0,
            # What the monitor WOULD do. Present precisely because it has not done it.
            "recommendation": (
                (state.case_file or {}).get(monitor_score.PENDING_ACTION)
                if state is not None
                else None
            ),
            "paused_reason": state.paused_reason if state is not None else None,
        },
        "window_days": window,
        "traffic": await traffic_summary(session, org_id, since),
        "campaigns": [],
        "signals": await recent_signals(session, org_id, since),
        "ai": {"available": ai_guard.is_available(settings), "calls": 0, "skipped": 0},
        "reviewed_at": _now().isoformat(),
        "actions_taken": [],  # always empty: this function does not act. See the docstring.
    }

    built = await monitor_cohorts.build(session, org_id, since=since)
    pairs = list(built)
    if not thorough:
        pairs = [(c, m) for c, m in pairs if monitor_cohorts.looks_like_a_campaign(c, m)]
    # What the review could and could not see. A report that cannot say "I only read the most
    # recent 5000 messages" or "half your one-off messages are outside this lens" invites the
    # reader to treat a partial look as a full one.
    report["coverage"] = {
        "messages_scanned": built.scanned,
        "truncated": built.truncated,
        "one_off_messages_not_grouped": built.singletons,
        "campaigns_found": len(built),
        "campaigns_examined": len(pairs),
    }

    business = report["account"]["declared_business"]
    budget = settings.monitor_review_max_ai_calls
    new_signals = 0
    # Templates already on this account's record. An operator pressing "review" twice must not
    # double the score off one campaign - see monitor_cohorts.signalled_fingerprints.
    seen = await monitor_cohorts.signalled_fingerprints(
        session,
        org_id,
        since=_now() - timedelta(days=settings.monitor_signal_window_days),
    )
    for cohort, m in pairs:
        entry = {
            "fingerprint": cohort.fingerprint,
            "template": cohort.sample_body[:500],
            "messages": cohort.size,
            "recipients": cohort.recipient_count,
            "first_seen": cohort.first_at.isoformat() if cohort.first_at else None,
            "last_seen": cohort.last_at.isoformat() if cohort.last_at else None,
            "never_contacted_before": round(m.first_contact_ratio, 3),
            "replied": round(m.reply_rate, 3),
            "undelivered": round(m.undelivered_rate, 3),
            "area_codes": m.spread,
            "flagged_by_prefilter": monitor_cohorts.looks_like_a_campaign(cohort, m),
            "verdict": None,
        }
        if thorough:
            # Cross-account overlap, on the operator's thorough review only. One extra query per
            # campaign is fine for a review a human asked for and waited on; it is not fine in
            # the hourly sweep across every workspace, which is why the sweep does not do it.
            # COUNTS ONLY - never which other workspaces. See shared_recipient_count.
            entry["shared_with_other_workspaces"] = await monitor_cohorts.shared_recipient_count(
                session, org_id, cohort.recipients, since=since
            )
            set_org_context(session, org_id)
        if report["ai"]["available"] and report["ai"]["calls"] < budget:
            try:
                review = await monitor_cohorts.review_one(settings, cohort, m, business)
            except ai_guard.AIUnavailable as exc:
                entry["verdict"] = {"error": str(exc)}
                report["ai"]["skipped"] += 1
            else:
                report["ai"]["calls"] += 1
                entry["verdict"] = {
                    "verdict": review.verdict,
                    "confidence": review.confidence,
                    "category": review.category,
                    "impersonates": review.impersonates,
                    "reason": review.reason,
                }
                # Recording keeps the score honest about what a review found. It still takes
                # no action - recompute only RECOMMENDS while MONITOR_AUTO_ACTION is off.
                already = cohort.fingerprint in seen
                entry["already_on_record"] = already
                if record_signals and review.actionable and not already:
                    weight = (
                        monitor_cohorts.WEIGHT_INCONSISTENT
                        if review.confidence >= monitor_cohorts.CONFIDENT
                        else monitor_cohorts.WEIGHT_INCONSISTENT_WEAK
                    )
                    await monitor_score.add_signal(
                        session,
                        settings,
                        org_id,
                        "cohort_inconsistent",
                        f"Operator review: {review.reason}",
                        detail={**entry, "verdict": entry["verdict"], "source": "manual_review"},
                        weight=weight,
                    )
                    seen.add(cohort.fingerprint)
                    new_signals += 1
        else:
            report["ai"]["skipped"] += 1
        report["campaigns"].append(entry)

    if new_signals:
        await session.commit()
        set_org_context(session, org_id)
        state = await monitor_score.get_state(session, org_id, create=True)
        report["monitor"]["score"] = state.score
        report["monitor"]["level"] = state.level
        report["monitor"]["recommendation"] = (state.case_file or {}).get(
            monitor_score.PENDING_ACTION
        )
    report["signals_added"] = new_signals

    # The one-line answer, so nobody has to add it up themselves.
    #
    # THE BUG THIS SHAPE EXISTS TO PREVENT. The first version said "No campaign contradicts this
    # business (N reviewed)" whenever nothing came back inconsistent - counting every campaign
    # FOUND as one reviewed. With the AI key missing, or the budget spent, nothing is reviewed at
    # all, and the operator's review button returned "No campaign contradicts this business (2
    # reviewed)" off two campaigns the model never saw. Proven by the peer's probe:
    # `ai: {'available': False, 'calls': 0, 'skipped': 2}` under exactly that headline. An
    # outage must never read as a clean bill of health - that is the same anti-pattern this
    # audit found eight times over: a check that can only return the reassuring answer.
    #
    # So the headline is computed from what was actually JUDGED, and every gap - no AI, budget
    # exhausted, truncated read - is stated in the sentence rather than left to the reader to
    # notice in a nested field.
    judged = [
        c
        for c in report["campaigns"]
        if isinstance(c["verdict"], dict) and c["verdict"].get("verdict")
    ]
    inconsistent = [c for c in judged if c["verdict"].get("verdict") == "inconsistent"]
    found = len(report["campaigns"])
    caveats = []
    # Only a caveat if there was something it could have judged. An account with no campaigns at
    # all is not "unreviewed because the AI was down" - there was nothing to ask about, and
    # saying otherwise trains an operator to ignore the word "unavailable".
    if found and not report["ai"]["available"]:
        caveats.append("the AI reviewer is unavailable")
    elif report["ai"]["skipped"]:
        skipped = report["ai"]["skipped"]
        caveats.append(
            f"{skipped} campaign{'s' if skipped != 1 else ''} could not be reviewed"
            + (f" (budget is {budget} AI calls)" if report["ai"]["calls"] >= budget else "")
        )
    if report["coverage"]["truncated"]:
        caveats.append(
            f"only the most recent {report['coverage']['messages_scanned']} messages were read"
        )

    if inconsistent:
        report["headline"] = (
            f"{len(inconsistent)} of {len(judged)} reviewed campaigns do not match this business"
        )
    elif judged:
        report["headline"] = f"No contradiction in {len(judged)} of {found} campaigns reviewed"
    else:
        # Nothing was judged. Say so, and do not imply anything about the account.
        report["headline"] = (
            f"NOT REVIEWED: {found} campaign{'s' if found != 1 else ''} found, none judged"
            if found
            else "No campaigns found in this window"
        )
    if caveats:
        report["headline"] += " - " + "; ".join(caveats)
    report["campaigns_reviewed"] = len(judged)
    # "Nothing stopped this review from seeing what is there." A quiet account with no campaigns
    # and no caveats is conclusive: `bool(judged) and not caveats` alone would report every quiet
    # customer as inconclusive forever, which is the mirror of the bug above - a word that always
    # says the same thing carries no information either way.
    report["conclusive"] = not caveats and (bool(judged) or not found)
    log.info(
        "monitor_review",
        org_id=str(org_id),
        campaigns=len(report["campaigns"]),
        judged=len(judged),
        inconsistent=len(inconsistent),
        conclusive=report["conclusive"],
        ai_calls=report["ai"]["calls"],
    )
    return report


# --------------------------------------------------------------------------------------
# Measuring what the monitor MISSES
# --------------------------------------------------------------------------------------
# Every other number this system produces measures what it CAUGHT: signals raised, campaigns
# flagged, exam cases stopped. None of them can move when the monitor stops seeing something,
# which is the failure that matters - a scammer whose template drifts below the pre-filter, or a
# quiet account nobody ever had a reason to look at, produces exactly the same reporting as a
# clean platform. "No findings" and "not looking" are indistinguishable from the inside.
#
# So this samples accounts the monitor currently believes are FINE, reviews them properly, and
# counts how many turn out not to be. That count is the miss rate, and it is the only figure here
# that can get worse while everything else looks healthy.
#
# TWO PROPERTIES THAT MAKE IT A MEASUREMENT RATHER THAN A SWEEP:
#   1. It records no signals. An audit that scored the accounts it sampled would change the thing
#      it measures - and, worse, the sample would stop being random the moment being sampled
#      became a reason to be flagged.
#   2. It is stratified by volume. A uniform sample of a 1000-account book is mostly the smallest
#      accounts, and a 30-message-a-month account cannot hide a campaign in the first place. The
#      miss rate that matters is the one on accounts with enough traffic to hide in.

#: Volume bands for the sample, as fractions of the sorted-by-volume account list. Three bands
#: rather than a continuous weighting because the point is coverage, not precision: an audit that
#: never looks at the busiest tenth of the book is the one that will be surprised.
_AUDIT_BANDS = 3

#: Confidence level for the upper bound reported beside the miss rate.
_AUDIT_ALPHA = 0.05


def miss_rate_upper_bound(missed: int, reviewed: int, *, alpha: float = _AUDIT_ALPHA) -> float:
    """The largest true miss rate consistent with this result.

    ONE-SIDED Clopper-Pearson, and the field name says so because the choice is invisible in the
    number itself. 0 of 9 gives 28.3% one-sided; the two-sided 95% interval's upper limit for the
    same data uses alpha/2 and gives 33.6%. One-sided is the right choice here - only the upper
    direction matters - but someone checking 28.3% against a two-sided calculator would get a
    different answer and conclude one of the two is broken.

    WHY THIS IS NOT OPTIONAL POLISH. `miss_rate: 0.0` off the default sample of nine is the most
    reassuring-looking field this system produces and it is nearly uninformative: zero misses in
    nine reviews is consistent with a true rate up to 28%. Everything else in this design refuses
    to make the reader do the arithmetic that decides whether to believe a number - reporting a
    rate without its precision does exactly that, and the direction of the error is always
    towards "the platform looks clean".

    Solved by bisection on the binomial tail rather than pulled from scipy: fifty iterations of
    exact integer arithmetic, no dependency, and `n` here is at most 50.
    """
    if reviewed <= 0:
        return 1.0
    if missed >= reviewed:
        return 1.0

    def tail(p: float) -> float:
        # P(X <= missed | n=reviewed, p). The upper limit is the p where this equals alpha.
        return sum(
            math.comb(reviewed, k) * p**k * (1.0 - p) ** (reviewed - k)
            for k in range(missed + 1)
        )

    lo, hi = float(missed) / reviewed, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if tail(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


async def audit_sample(
    session: AsyncSession,
    settings,
    *,
    sample_size: int = 9,
    days: int = 7,
    seed: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Review a stratified sample of accounts the monitor thinks are clean, and report the misses.

    Returns the miss rate and, for anything it turned up, enough for an operator to go and look
    with `POST /orgs/{id}/review`. It deliberately does NOT raise the finding as a signal: a
    human decides, and an automated audit quietly scoring accounts would be the same
    action-without-a-decision the rest of this design refuses.

    It writes exactly one row, a `monitor_health` record of the platform-level result, so the
    miss rate is a tracked series rather than a number that scrolls past in a log. Nothing is
    written against any SAMPLED account - not a signal, not an audit row. An audit row in a
    customer's own log would tell that customer they had been picked, which is the one thing a
    sampled audit must not emit.
    """
    from random import Random

    from app.db.base import ALLOW_UNSCOPED_KEY

    started = now or _now()
    since = started - timedelta(days=days)

    # JUSTIFIED allow_unscoped: choosing WHICH tenants to audit is a platform-level question and
    # cannot be asked inside one org's scope. Aggregates only - counts per org id, no content.
    volumes = (
        await session.execute(
            sa.select(Message.org_id, sa.func.count(Message.id))
            .where(Message.direction == "outbound", Message.created_at >= since)
            .group_by(Message.org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    # Accounts the monitor already has an opinion about are not part of the question. The miss
    # rate is about accounts it is SILENT on: anything above `normal`, or carrying a finding in
    # the window, is already in front of a human by another route.
    noisy = set(
        (
            await session.execute(
                sa.select(OrgMonitoring.org_id)
                .where(OrgMonitoring.level != "normal")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    flagged = set(
        (
            await session.execute(
                sa.select(MonitorSignal.org_id)
                .where(MonitorSignal.created_at >= since)
                .group_by(MonitorSignal.org_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    quiet = sorted(
        (
            (org_id, int(n))
            for org_id, n in volumes
            if org_id not in noisy and org_id not in flagged
        ),
        key=lambda row: row[1],
    )

    report: dict = {
        "window_days": days,
        "eligible_accounts": len(quiet),
        "sampled": 0,
        "reviewed": 0,
        "missed": 0,
        "miss_rate": None,
        "miss_rate_upper_95_one_sided": None,
        "miss_rate_text": "nothing reviewed",
        "findings": [],
        "inconclusive": 0,
        "sampled_at": started.isoformat(),
    }
    # NO early return when there is nothing to sample. It was tempting - there is no work to do -
    # but returning here skipped the tail, so the report came back with no `passed` key and no
    # health row at all. An audit that found nothing it was allowed to look at has not passed and
    # has not verified anything, and it has to say so through the same fields as every other run;
    # a shape that changes depending on the outcome is a shape callers cannot check.
    rng = Random(seed)
    picks: list[uuid.UUID] = []
    band_len = max(1, len(quiet) // _AUDIT_BANDS)
    # The REMAINDER is distributed, not floored away. `sample_size // _AUDIT_BANDS` gave 1 per
    # band for a requested 5, so an operator who asked for five accounts got three - with the
    # report saying `sampled: 3`, so nothing lied, but nothing explained it either.
    quotas = [
        sample_size // _AUDIT_BANDS + (1 if band < sample_size % _AUDIT_BANDS else 0)
        for band in range(_AUDIT_BANDS)
    ]
    for band in range(_AUDIT_BANDS):
        lo = band * band_len
        hi = len(quiet) if band == _AUDIT_BANDS - 1 else min(len(quiet), lo + band_len)
        window = quiet[lo:hi]
        if not window or not quotas[band]:
            continue
        picks += [org_id for org_id, _ in rng.sample(window, min(quotas[band], len(window)))]
    # A short band (or a duplicate) must not cost the whole request: top up from whatever is
    # left, cheapest-to-reason-about way to honour `sample_size` when the bands cannot.
    picks = list(dict.fromkeys(picks))
    if len(picks) < sample_size:
        spare = [org_id for org_id, _ in quiet if org_id not in set(picks)]
        rng.shuffle(spare)
        picks += spare[: sample_size - len(picks)]
    picks = picks[:sample_size]
    report["sampled"] = len(picks)

    for org_id in picks:
        try:
            one = await review_account(
                session,
                settings,
                org_id,
                days=days,
                thorough=True,
                # The whole point. See the module comment above.
                record_signals=False,
                # And no OrgMonitoring row either - finding 1. `record_signals=False` stops the
                # signal; only this stops the READ from writing.
                touch_state=False,
            )
        except Exception:  # noqa: BLE001 - one unreviewable account must not void the audit
            log.exception("monitor_audit_account_failed", org_id=str(org_id))
            report["inconclusive"] += 1
            # ROLLBACK, or the comment above is a lie. A SQLAlchemyError leaves the session in a
            # failed transaction, so every later `execute` raises PendingRollbackError and the
            # whole remaining sample lands in this same handler - one bad account voiding the
            # audit, which is the precise outcome this `except` exists to prevent. The report
            # stayed honest (reviewed 0, passed False) but it was honest about the wrong thing.
            await session.rollback()
            continue
        if not one["conclusive"]:
            # An account the audit could not actually judge is not evidence of a clean platform.
            report["inconclusive"] += 1
            if not one["campaigns_reviewed"]:
                continue
        report["reviewed"] += 1
        bad = [
            c
            for c in one["campaigns"]
            if isinstance(c["verdict"], dict) and c["verdict"].get("verdict") == "inconsistent"
        ]
        if bad:
            report["missed"] += 1
            report["findings"].append(
                {
                    "org_id": str(org_id),
                    "name": one["account"]["name"],
                    "headline": one["headline"],
                    "campaigns": [
                        {
                            "fingerprint": c["fingerprint"],
                            "messages": c["messages"],
                            "recipients": c["recipients"],
                            "reason": c["verdict"].get("reason"),
                        }
                        for c in bad
                    ],
                }
            )
    if report["reviewed"]:
        report["miss_rate"] = round(report["missed"] / report["reviewed"], 3)
        # Always beside the rate, never optional. See miss_rate_upper_bound.
        report["miss_rate_upper_95_one_sided"] = miss_rate_upper_bound(
            report["missed"], report["reviewed"]
        )
        # Spelled out so a UI can print this instead of a bare percentage, and so the sample size
        # travels with the number wherever it gets copied.
        report["miss_rate_text"] = (
            f"{report['missed']} of {report['reviewed']} reviewed"
            f" (true rate could be up to {report['miss_rate_upper_95_one_sided']:.0%},"
            f" one-sided 95%)"
        )
    # `passed` is a three-way question collapsed to a bool, so be explicit about which way the
    # unknown falls: an audit that could review NOTHING has not passed. Reviewing zero accounts
    # and calling it a pass is the whole failure this function was written to detect, and it
    # would be absurd for the detector to commit it itself.
    passed = bool(report["reviewed"]) and report["missed"] == 0
    session.add(
        MonitorHealth(
            id=uuid.uuid4(),
            kind="audit_sample",
            passed=passed,
            detail={k: v for k, v in report.items() if k != "findings"},
        )
    )
    report["passed"] = passed
    # `miss_rate` stays None when nothing could be reviewed. NOT 0.0: a rate of zero over zero
    # reviews reads as "we checked and found nothing", which is the exact claim the audit exists
    # to stop anyone making. The caller has to handle None, which is the point.
    log.info(
        "monitor_audit_sample",
        eligible=report["eligible_accounts"],
        sampled=report["sampled"],
        reviewed=report["reviewed"],
        missed=report["missed"],
        inconclusive=report["inconclusive"],
    )
    return report
