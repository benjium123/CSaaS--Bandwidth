"""P43: run the CAMPAIGN reviewer against the live safety AI (DeepSeek Flash).

Why this exists separately from `monitor_exam.py`. That exam asks the per-message question:
"is this text a scam?". The cohort reviewer asks a different one - "is this CAMPAIGN the kind
of messaging this business would send?" - and it is a strictly harder question, because the
same words are legitimate from one business and fraudulent from another. A courier fee notice
is ordinary from a courier and disqualifying from a plumber, and nothing in the message body
tells you which. Every number quoted about the cohort layer so far came from a mock AI, which
proves the plumbing works and proves nothing about the judgement.

Each case is a whole cohort: the declared business, the sample bodies the reviewer would be
shown, and the behavioural facts a real build would have measured. One live AI call per case,
the same call `tick()` makes on real traffic.

    python scripts/cohort_exam.py                 # all cases
    python scripts/cohort_exam.py --country GB    # one country
    python scripts/cohort_exam.py --repeat 3      # stability: same case N times

Exit code 0 = passed (catch rate >= 95%, false alarms <= 3%), 1 = failed, 2 = AI unreachable.

The two rates are NOT symmetric and the thresholds say so. A missed scam costs a victim; a
false alarm costs an operator two minutes reading a report about a business that turns out to
be fine. But a monitor that cries wolf gets ignored, which converts every false alarm into a
future miss - so false alarms are capped tightly rather than tolerated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CASES = (
    Path(__file__).resolve().parents[1]
    / "app" / "services" / "monitor_exam_data" / "cohorts.jsonl"
)
CATCH_FLOOR = 0.95
FALSE_ALARM_CEILING = 0.03


def load_cases(path: Path = CASES) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            out.append(json.loads(line))
    return out


def as_cohort(case: dict):
    """Build the in-memory Cohort + CohortMetrics a real `build()` would have produced.

    Deliberately constructed rather than inserted into a database: this exam tests the
    JUDGEMENT, and a DB round-trip would only re-test the clustering that has its own suite.
    """
    from app.services.monitor_cohorts import Cohort, CohortMetrics

    bodies = case["bodies"]
    cohort = Cohort(
        fingerprint=case["id"],
        sample_body=bodies[0],
        samples=list(bodies[1:]),
        message_ids=[None] * int(case["size"]),
        recipients={f"+1000000{n:04d}" for n in range(int(case["recipients"]))},
    )
    m = CohortMetrics(
        first_contact_ratio=float(case["first_contact_ratio"]),
        reply_rate=float(case["reply_rate"]),
        undelivered_rate=float(case["undelivered_rate"]),
        spread=int(case["spread"]),
    )
    return cohort, m


async def run_case(settings, case: dict) -> dict:
    from app.services import ai_guard, monitor_cohorts

    cohort, m = as_cohort(case)
    try:
        review = await monitor_cohorts.review_one(settings, cohort, m, case["business"])
    except ai_guard.AIUnavailable as exc:
        return {"id": case["id"], "expect": case["expect"], "error": str(exc)}
    # "unclear" is counted as a MISS on a scam and as a PASS on legitimate traffic: the
    # monitor only acts on "inconsistent" (CohortReview.actionable), so an unclear verdict on
    # a scam lets it through, while an unclear verdict on a legitimate campaign costs nothing.
    flagged = review.verdict == "inconsistent"
    scam = case["expect"] == "inconsistent"
    return {
        "id": case["id"],
        "country": case.get("country"),
        "expect": case["expect"],
        "verdict": review.verdict,
        "confidence": review.confidence,
        "category": review.category,
        "impersonates": review.impersonates,
        "reason": review.reason,
        "correct": flagged == scam,
        "flagged": flagged,
        "prefilter": monitor_cohorts.looks_like_a_campaign(cohort, m),
        "tokens": list(review.tokens),
    }


async def main(country: str | None, repeat: int, verbose: bool) -> int:
    from app.config import load_settings
    from app.services import ai_guard

    settings = load_settings()
    if not ai_guard.is_available(settings):
        print("AI unavailable: set DEEPSEEK_API_KEY and AI_GUARD_ENABLED=true", file=sys.stderr)
        return 2

    cases = [c for c in load_cases() if not country or c.get("country") == country]
    cases = [c for c in cases for _ in range(repeat)]
    # Sequential on purpose: the live API is rate-limited per key and a 429 retried into a
    # second failure surfaces as AIUnavailable, which would read as a missed scam.
    results = []
    for case in cases:
        res = await run_case(settings, case)
        results.append(res)
        mark = "ok " if res.get("correct") else ("ERR" if res.get("error") else "MISS")
        print(f"  {mark}  {res['id']:<28} {res.get('verdict', res.get('error'))!s:<14}"
              f" conf={res.get('confidence', '-')}", flush=True)
        if verbose and not res.get("correct"):
            print(f"        reason: {res.get('reason')}", flush=True)

    errors = [r for r in results if r.get("error")]
    scams = [r for r in results if r["expect"] == "inconsistent" and not r.get("error")]
    legit = [r for r in results if r["expect"] == "consistent" and not r.get("error")]
    caught = [r for r in scams if r["flagged"]]
    false_alarms = [r for r in legit if r["flagged"]]
    # An error is NOT a pass. An exam that treats an unreachable AI as "nothing found" is the
    # same anti-pattern this whole audit kept finding.
    catch_rate = len(caught) / len(scams) if scams else 0.0
    false_rate = len(false_alarms) / len(legit) if legit else 0.0
    passed = (
        not errors
        and catch_rate >= CATCH_FLOOR
        and false_rate <= FALSE_ALARM_CEILING
        and bool(scams)
        and bool(legit)
    )

    summary = {
        "cases": len(results),
        "scams": len(scams),
        "legitimate": len(legit),
        "caught": len(caught),
        "catch_rate": round(catch_rate, 4),
        "false_alarms": len(false_alarms),
        "false_alarm_rate": round(false_rate, 4),
        "ai_errors": len(errors),
        "missed": [r["id"] for r in scams if not r["flagged"]],
        "wrongly_flagged": [r["id"] for r in false_alarms],
        "verdicts": dict(Counter(r.get("verdict", "error") for r in results)),
        "tokens_in": sum(r.get("tokens", [0, 0])[0] for r in results),
        "tokens_out": sum(r.get("tokens", [0, 0])[1] for r in results),
        "passed": passed,
    }
    print(json.dumps(summary, indent=2))
    print("PASSED" if passed else "FAILED", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", help="only cases for this country (US, GB)")
    parser.add_argument("--repeat", type=int, default=1, help="run each case N times")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print reasons for wrong answers"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.country, args.repeat, args.verbose)))
