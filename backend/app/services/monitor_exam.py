# ruff: noqa: E501 - long literal canary messages
"""P43: proving the AI monitor works - the exam and the canary.

Exam: a library of real-world-style scam and legitimate texts and calls
(``monitor_exam_data/*.jsonl``, plus every operator decision in ``monitor_labels``) is run
through EXACTLY the functions live traffic uses (``monitor_text.judge_text`` +
``monitor_rules``, ``monitor_calls.judge_call``). The result is the catch rate (scams
held/blocked/flagged) and the false-alarm rate (legitimate traffic stopped). Run weekly by
the sweeper and on demand (``scripts/monitor_exam.py``) before any prompt or model change.

Canary: every hour a small fixed set (two scams, one legit text, one scam call) goes
through the same functions. If a scam gets through, or the AI can't be reached, a
``monitor_health`` security alert opens. This is how you know the monitor works TODAY,
not just that it worked once.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import MonitorHealth, MonitorLabel, SecurityAlert
from app.services import ai_guard, monitor_calls, monitor_rules, monitor_text

log = structlog.get_logger("monitor_exam")

DATA_DIR = Path(__file__).with_name("monitor_exam_data")
CATCH_RATE_MIN = 0.95
FALSE_ALARM_MAX = 0.03

CANARY_TEXTS = [
    {
        "id": "canary-scam-bank",
        "label": "scam",
        "business": {
            "business_name": "Canary Plumbing",
            "what_they_do": "Plumbing appointment reminders",
        },
        "body": "Wells Fargo: your account is suspended. Verify your card number and PIN now at wf-verify-login.top or it will be closed today.",
    },
    {
        "id": "canary-scam-giftcard",
        "label": "scam",
        "business": {
            "business_name": "Canary Plumbing",
            "what_they_do": "Plumbing appointment reminders",
        },
        "body": "This is the sheriff's office. You missed jury duty and a warrant is out. Pay the fine with Target gift cards within 2 hours to avoid arrest.",
    },
    {
        "id": "canary-legit",
        "label": "legit",
        "business": {
            "business_name": "Canary Plumbing",
            "what_they_do": "Plumbing appointment reminders",
        },
        "body": "Canary Plumbing: your technician arrives tomorrow 9-11am. Reply C to confirm.",
    },
]
CANARY_CALLS = [
    {
        "id": "canary-call-scam",
        "label": "scam",
        "business": {
            "business_name": "Canary Plumbing",
            "what_they_do": "Plumbing appointment reminders",
        },
        "transcript": "agent: This is the Social Security Administration. Your number was used in a crime and will be suspended.\nuser: What do I do?\nagent: Withdraw your savings and deposit it in a Bitcoin ATM so we can protect it. Do not tell your bank.",
    }
]


@dataclass
class ExamResult:
    total: int = 0
    scams: int = 0
    caught: int = 0
    legit: int = 0
    false_alarms: int = 0
    unavailable: int = 0
    misses: list[dict] = field(default_factory=list)
    false_alarm_cases: list[dict] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def catch_rate(self) -> float:
        return self.caught / self.scams if self.scams else 1.0

    @property
    def false_alarm_rate(self) -> float:
        return self.false_alarms / self.legit if self.legit else 0.0

    @property
    def passed(self) -> bool:
        # `scams`/`legit` must be non-zero, and that is not belt-and-braces. `catch_rate`
        # returns 1.0 over zero scams and `false_alarm_rate` returns 0.0 over zero legit
        # cases, so an exam that loaded NOTHING - a data file renamed, a filter that matched
        # nothing, a bucket wired up but never populated - scores a perfect pass. An exam
        # that cannot fail is not an exam.
        return (
            self.unavailable == 0
            and self.scams > 0
            and self.legit > 0
            and self.catch_rate >= CATCH_RATE_MIN
            and self.false_alarm_rate <= FALSE_ALARM_MAX
        )

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "scams": self.scams,
            "caught": self.caught,
            "catch_rate": round(self.catch_rate, 4),
            "legit": self.legit,
            "false_alarms": self.false_alarms,
            "false_alarm_rate": round(self.false_alarm_rate, 4),
            "unavailable": self.unavailable,
            "misses": self.misses[:20],
            "false_alarm_cases": self.false_alarm_cases[:20],
            "tokens": {"in": self.tokens_in, "out": self.tokens_out},
            "passed": self.passed,
        }


def load_cases(kind: str) -> list[dict]:
    path = DATA_DIR / f"{kind}.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def labelled_cases(session: AsyncSession, kind: str, limit: int = 200) -> list[dict]:
    rows = (
        (
            await session.execute(
                sa.select(MonitorLabel)
                .where(MonitorLabel.kind == ("text" if kind == "texts" else "call"))
                .order_by(MonitorLabel.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    field_name = "body" if kind == "texts" else "transcript"
    return [
        {
            "id": f"label-{row.id}",
            "label": row.label,
            "business": (row.context or {}).get("business") or {},
            field_name: row.content,
        }
        for row in rows
    ]


async def judge_text_case(settings: Settings, case: dict) -> tuple[str, tuple[int, int]]:
    """'stopped' | 'allowed' - the same order as live traffic: rules block first, then AI."""
    rules = monitor_rules.evaluate(case["body"])
    if rules.action == "block":
        return "stopped", (0, 0)
    screening, _confidence, tokens = await monitor_text.judge_text(
        settings, case.get("business") or {}, case["body"], rules
    )
    return ("allowed" if screening.action == "allow" else "stopped"), tokens


async def judge_call_case(settings: Settings, case: dict) -> tuple[str, tuple[int, int]]:
    result = await monitor_calls.judge_call(
        settings, case.get("business") or {}, case["transcript"], {"direction": "outbound"}
    )
    return ("allowed" if result["verdict"] == "ok" else "stopped"), result["tokens"]


def load_cohort_cases() -> list[dict]:
    """The campaign cases, translated into the shape `run` scores.

    `cohorts.jsonl` is written in the reviewer's own vocabulary (`expect: consistent |
    inconsistent`) because `scripts/cohort_exam.py` reads it too and that is the vocabulary
    an operator reading the file needs. The mapping is exact: a campaign the monitor should
    act on is a scam, and one it should leave alone is legitimate.
    """
    path = DATA_DIR / "cohorts.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        out.append(
            {
                **case,
                "label": "scam" if case["expect"] == "inconsistent" else "legit",
            }
        )
    return out


async def judge_cohort_case(settings: Settings, case: dict) -> tuple[str, tuple[int, int]]:
    """One campaign through `review_one` - the same call the sweep makes on real traffic.

    The cohort is built in memory rather than inserted and re-clustered: what is under test
    here is the JUDGEMENT, and clustering has its own suite. Only `inconsistent` counts as
    stopped, matching `CohortReview.actionable` - the monitor does not act on `unclear`, so
    scoring `unclear` as a catch would credit the exam for a scam that goes out.
    """
    from app.services import monitor_cohorts

    cohort = monitor_cohorts.Cohort(
        fingerprint=case["id"],
        sample_body=case["bodies"][0],
        samples=list(case["bodies"][1:]),
        message_ids=[None] * int(case["size"]),
        recipients={f"+1000000{n:04d}" for n in range(int(case["recipients"]))},
    )
    m = monitor_cohorts.CohortMetrics(
        first_contact_ratio=float(case["first_contact_ratio"]),
        reply_rate=float(case["reply_rate"]),
        undelivered_rate=float(case["undelivered_rate"]),
        spread=int(case["spread"]),
    )
    review = await monitor_cohorts.review_one(settings, cohort, m, case["business"])
    return ("stopped" if review.actionable else "allowed"), review.tokens


async def run(
    settings: Settings,
    texts: list[dict],
    calls: list[dict],
    *,
    cohorts: list[dict] | None = None,
    stop_on_unavailable: bool = False,
) -> ExamResult:
    result = ExamResult()
    for kind, cases, judge in (
        ("text", texts, judge_text_case),
        ("call", calls, judge_call_case),
        ("cohort", cohorts or [], judge_cohort_case),
    ):
        for case in cases:
            result.total += 1
            is_scam = case["label"] == "scam"
            if is_scam:
                result.scams += 1
            else:
                result.legit += 1
            try:
                outcome, tokens = await judge(settings, case)
            except ai_guard.AIUnavailable:
                result.unavailable += 1
                if stop_on_unavailable:
                    return result  # one outage answer is enough - don't wait on the rest
                continue
            result.tokens_in += tokens[0]
            result.tokens_out += tokens[1]
            if is_scam and outcome == "stopped":
                result.caught += 1
            elif is_scam:
                result.misses.append({"kind": kind, "id": case["id"]})
            elif outcome == "stopped":
                result.false_alarms += 1
                result.false_alarm_cases.append({"kind": kind, "id": case["id"]})
    return result


async def _record(session: AsyncSession, kind: str, result: ExamResult) -> MonitorHealth:
    row = MonitorHealth(id=uuid.uuid4(), kind=kind, passed=result.passed, detail=result.as_dict())
    session.add(row)
    if not result.passed:
        # One open alert at a time per kind, so a long outage doesn't flood the queue.
        open_alert = (
            await session.execute(
                sa.select(SecurityAlert.id).where(
                    SecurityAlert.kind == "monitor_health",
                    SecurityAlert.status == "open",
                )
            )
        ).first()
        if open_alert is None:
            session.add(
                SecurityAlert(
                    id=uuid.uuid4(),
                    kind="monitor_health",
                    status="open",
                    detail={"check": kind, **result.as_dict()},
                )
            )
        log.error(
            "monitor_health_failed",
            check=kind,
            **{
                k: v
                for k, v in result.as_dict().items()
                if k in ("catch_rate", "false_alarm_rate", "unavailable")
            },
        )
    await session.commit()
    return row


async def canary_tick(session: AsyncSession, settings: Settings) -> MonitorHealth:
    """Hourly: the fixed canary set through the live judgement functions."""
    # Strict by construction: with 3 scams and 1 legit case, one miss or one false alarm
    # already falls outside CATCH_RATE_MIN / FALSE_ALARM_MAX.
    # Never hold a database transaction open while waiting on the AI.
    await session.commit()
    result = await run(settings, CANARY_TEXTS, CANARY_CALLS, stop_on_unavailable=True)
    return await _record(session, "canary", result)


async def cohort_exam_tick(session: AsyncSession, settings: Settings) -> MonitorHealth:
    """Weekly: the campaign reviewer, scored in its OWN bucket.

    Kept out of the text/call exam's numbers deliberately. They measure different questions -
    "is this message a scam?" against "is this campaign the kind of thing this business would
    send?" - and a blended rate hides which half regressed. The campaign question is the
    harder one: the same words are legitimate from one business and fraudulent from another,
    so a regression here looks like nothing at all in a combined score.
    """
    cases = load_cohort_cases()
    await session.commit()
    result = await run(settings, [], [], cohorts=cases, stop_on_unavailable=True)
    return await _record(session, "cohort_exam", result)


async def exam_tick(session: AsyncSession, settings: Settings) -> MonitorHealth:
    """Weekly: the whole library, plus the latest operator-labelled cases.

    Runs the cohort exam alongside it and records it as a SEPARATE monitor_health row, so the
    two rates stay independent while the schedule stays single. A failure in either opens the
    alert; neither can mask the other, and neither can pass by being empty.
    """
    texts = load_cases("texts") + await labelled_cases(session, "texts")
    calls = load_cases("calls") + await labelled_cases(session, "calls")
    # The exam takes minutes: end the read transaction first so it holds no locks (on
    # SQLite a reader blocks every writer; on Postgres it would stall migrations).
    await session.commit()
    result = await run(settings, texts, calls, stop_on_unavailable=True)
    row = await _record(session, "exam", result)
    # After the primary row is recorded, so a failure in the campaign reviewer can never cost
    # us the text exam's result.
    await cohort_exam_tick(session, settings)
    return row


async def due(session: AsyncSession, kind: str, every: timedelta) -> bool:
    last = (
        await session.execute(
            sa.select(MonitorHealth.created_at)
            .where(MonitorHealth.kind == kind)
            .order_by(MonitorHealth.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= every
