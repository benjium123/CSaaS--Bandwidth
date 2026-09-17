"""P43: run the AI monitor's exam against the live safety AI (DeepSeek Flash).

Run it before changing a prompt, the model or the rules, and whenever you want proof that the
monitor catches scams without stopping legitimate traffic.

    docker compose exec api python scripts/monitor_exam.py            # library only
    docker compose exec api python scripts/monitor_exam.py --labels   # + operator decisions
    docker compose exec api python scripts/monitor_exam.py --record   # also save the result

Exit code 0 = passed (catch rate >= 95%, false alarms <= 3%, AI reachable), 1 = failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main(labels: bool, record: bool) -> int:
    from app.config import load_settings
    from app.db.session import dispose_engine, get_sessionmaker, init_engine
    from app.services import monitor_exam

    settings = load_settings()
    texts = monitor_exam.load_cases("texts")
    calls = monitor_exam.load_cases("calls")
    if labels or record:
        init_engine(settings.database_url)
    if labels:
        async with get_sessionmaker()() as session:
            texts += await monitor_exam.labelled_cases(session, "texts")
            calls += await monitor_exam.labelled_cases(session, "calls")
    result = await monitor_exam.run(settings, texts, calls)
    report = result.as_dict()
    print(json.dumps(report, indent=2))
    if record:
        async with get_sessionmaker()() as session:
            await monitor_exam._record(session, "exam", result)
    if labels or record:
        await dispose_engine()
    print("PASSED" if result.passed else "FAILED", file=sys.stderr)
    return 0 if result.passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", action="store_true", help="include operator-labelled cases")
    parser.add_argument("--record", action="store_true", help="save the result as monitor health")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.labels, args.record)))
