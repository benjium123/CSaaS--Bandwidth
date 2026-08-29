#!/usr/bin/env python
"""Bounded async load test (P14 DR-6). Zero new products: asyncio + httpx (already a
runtime dependency - see pyproject.toml) drive concurrent workers against a running
deployment for a fixed duration, then report p50/p95/p99 latency and the error rate.

    python scripts/load_test.py --base-url https://host --token <jwt> --org-id <uuid> \
        --rps 20 --seconds 30 --mode read

Modes:
  read   - GETs /api/v1/inbox/threads and /api/v1/analytics/overview, alternating.
  send   - POSTs /api/v1/messages. REFUSES to run unless every number on the target org is
           on the loopback carrier (verified LIVE via GET /api/v1/numbers), or
           --i-know-this-is-loopback is passed explicitly. This must never be pointed at a
           real carrier by a slipped flag or a copy-pasted command - see DR-6.
  mixed  - mostly read traffic with an occasional send; same loopback gate as send.

Pass bar (docs/RUNBOOK.md): p95 < 250ms @ 20 rps sustained on the VPS, error rate 0.

--self-test needs no running server and no --base-url/--token/--org-id: it drives a tiny
in-process burst straight against the ASGI app via httpx.ASGITransport, against a
loopback-carrier test org built the same way tests/conftest.py builds one. This is what a
CI job or an implementer with nothing deployed can run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

READ_PATHS = ("/api/v1/inbox/threads", "/api/v1/analytics/overview")
LOAD_TEST_BODY = {"to": "+19725559999", "body": "csaas load_test - please ignore"}
#: Hard ceiling (Opus review): this is a bounded synthetic load tool, not a stress-test
#: cannon - a fat-fingered --rps must not be able to accidentally hammer a shared VPS.
RPS_CEILING = 200


@dataclass
class Result:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    total: int = 0

    def record(self, latency_ms: float, ok: bool) -> None:
        self.total += 1
        self.latencies_ms.append(latency_ms)
        if not ok:
            self.errors += 1

    def _pct(self, p: float) -> float:
        sorted_ms = sorted(self.latencies_ms)
        idx = min(len(sorted_ms) - 1, int(round(p * (len(sorted_ms) - 1))))
        return sorted_ms[idx]

    def report(self, label: str) -> str:
        if not self.latencies_ms:
            return f"{label}: no requests completed"
        error_rate = self.errors / self.total
        return (
            f"{label}: n={self.total} errors={self.errors} ({error_rate:.1%})  "
            f"p50={self._pct(0.50):.0f}ms p95={self._pct(0.95):.0f}ms "
            f"p99={self._pct(0.99):.0f}ms max={max(self.latencies_ms):.0f}ms"
        )


async def _timed(coro) -> tuple[float, bool]:  # noqa: ANN001
    started = time.perf_counter()
    try:
        resp = await coro
        ok = 200 <= resp.status_code < 300
    except Exception:
        ok = False
    return (time.perf_counter() - started) * 1000, ok


async def _verify_loopback_org(
    client: httpx.AsyncClient, headers: dict, *, allow_override: bool
) -> None:
    """DR-6's hard gate: send/mixed mode never runs against a real carrier."""
    if allow_override:
        return
    r = await client.get("/api/v1/numbers", headers=headers)
    r.raise_for_status()
    numbers = r.json()
    if not numbers:
        raise SystemExit(
            "REFUSING send/mixed mode: this org has no numbers to verify as loopback. "
            "Pass --i-know-this-is-loopback only if you are certain this is safe."
        )
    non_loopback = [n["e164"] for n in numbers if n.get("carrier") != "loopback"]
    if non_loopback:
        raise SystemExit(
            "REFUSING send/mixed mode: this org has non-loopback number(s) "
            f"({', '.join(non_loopback)}). load_test must never send through a real "
            "carrier - point it at a loopback-only org, or pass --i-know-this-is-loopback "
            "only if you are certain."
        )


async def _worker(
    client: httpx.AsyncClient,
    headers: dict,
    mode: str,
    stop_at: float,
    interval: float,
    result: Result,
) -> None:
    counter = 0
    while time.perf_counter() < stop_at:
        started = time.perf_counter()
        is_send = mode == "send" or (mode == "mixed" and counter % 5 == 4)
        if is_send:
            elapsed_ms, ok = await _timed(
                client.post("/api/v1/messages", json=LOAD_TEST_BODY, headers=headers)
            )
        else:
            path = READ_PATHS[counter % len(READ_PATHS)]
            elapsed_ms, ok = await _timed(client.get(path, headers=headers))
        result.record(elapsed_ms, ok)
        counter += 1
        sleep_for = interval - (time.perf_counter() - started)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


async def run(
    base_url: str,
    token: str,
    org_id: str,
    rps: int,
    seconds: int,
    mode: str,
    allow_override: bool,
) -> Result:
    headers = {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}
    result = Result()
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        if mode in ("send", "mixed"):
            await _verify_loopback_org(client, headers, allow_override=allow_override)
        # One worker per rps unit, each pacing itself to ~1 request/second - a simple,
        # good-enough synthetic generator, not a precision traffic shaper.
        stop_at = time.perf_counter() + seconds
        workers = [
            _worker(client, headers, mode, stop_at, 1.0, result) for _ in range(max(rps, 1))
        ]
        await asyncio.gather(*workers)
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bounded async load test (P14 DR-6)")
    p.add_argument("--base-url", default="")
    p.add_argument("--token", default="")
    p.add_argument("--org-id", default="")
    p.add_argument("--rps", type=int, default=20)
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--mode", choices=("send", "read", "mixed"), default="read")
    p.add_argument(
        "--i-know-this-is-loopback",
        dest="i_know_this_is_loopback",
        action="store_true",
        help=(
            "Skip the live loopback-carrier check for send/mixed mode. Use ONLY when you "
            "have already verified the target org sends exclusively through loopback."
        ),
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run an in-process burst against the ASGI app; needs no server and no flags above.",
    )
    return p.parse_args(argv)


# ------------------------------------------------------------------------------------
# --self-test: no network, no running server.
# ------------------------------------------------------------------------------------
async def _self_test() -> int:
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import auth_headers, create_org, make_settings, register_and_login  # noqa: E402

    from app.db.base import Base
    from app.db.session import dispose_engine, init_engine
    from app.main import create_app

    # Every real carrier explicitly OFF: make_settings() inherits the repo .env, which may
    # hold real Bandwidth/Telnyx credentials on a developer machine - LOOPBACK_CARRIER_ENABLED
    # contradicts any carrier that is live OR explicitly flagged on (config.py's ambiguous-
    # carrier guard), so every one needs an explicit False here regardless of what .env holds.
    settings = make_settings(
        loopback_carrier_enabled=True,
        bandwidth_enabled=False,
        telnyx_enabled=False,
        twilio_enabled=False,
        plivo_enabled=False,
        signalwire_enabled=False,
    )
    engine = init_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = await register_and_login(client, "loadtest@example.com")
            org = await create_org(client, token, "Load Test Org")
            headers = auth_headers(token, org["id"])
            r = await client.post(
                "/api/v1/numbers", json={"e164": "+12145550100"}, headers=headers
            )
            if r.status_code != 201:
                print(
                    f"load_test --self-test: FAILED setting up a number: {r.text}",
                    file=sys.stderr,
                )
                return 1

            read_result = Result()
            stop_at = time.perf_counter() + 1.0
            await asyncio.gather(
                *[_worker(client, headers, "read", stop_at, 0.1, read_result) for _ in range(3)]
            )

            await _verify_loopback_org(client, headers, allow_override=False)
            send_result = Result()
            elapsed_ms, ok = await _timed(
                client.post("/api/v1/messages", json=LOAD_TEST_BODY, headers=headers)
            )
            send_result.record(elapsed_ms, ok)

        if not read_result.latencies_ms or read_result.errors:
            print(
                "load_test --self-test: FAILED (read burst had errors or produced no data)",
                file=sys.stderr,
            )
            return 1
        if send_result.errors:
            print("load_test --self-test: FAILED (loopback send failed)", file=sys.stderr)
            return 1

        print(read_result.report("self-test read burst"))
        print(send_result.report("self-test loopback send"))
        print("load_test --self-test: OK")
        return 0
    finally:
        await dispose_engine()


async def _main() -> int:
    args = _parse_args(sys.argv[1:])
    if args.self_test:
        return await _self_test()

    missing = [
        name
        for name, val in (
            ("--base-url", args.base_url),
            ("--token", args.token),
            ("--org-id", args.org_id),
        )
        if not val
    ]
    if missing:
        print(f"Missing required arguments: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.rps > RPS_CEILING:
        print(
            f"REFUSING: --rps {args.rps} exceeds the ceiling of {RPS_CEILING}. This is a "
            "bounded synthetic load tool, not a stress-test cannon - if you genuinely need "
            "more, raise RPS_CEILING deliberately rather than passing a bigger number here.",
            file=sys.stderr,
        )
        return 2

    result = await run(
        args.base_url,
        args.token,
        args.org_id,
        args.rps,
        args.seconds,
        args.mode,
        args.i_know_this_is_loopback,
    )
    print(result.report(f"{args.mode} @ {args.rps} rps for {args.seconds}s"))
    return 0 if result.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
