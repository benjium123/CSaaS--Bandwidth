#!/usr/bin/env python
"""P1b gate demo: a human-readable live SMS round-trip against a deployed instance.

    export SMOKE_BASE_URL=https://your-host
    export SMOKE_TOKEN=<jwt>  SMOKE_ORG_ID=<uuid>
    export BANDWIDTH_TEST_RECIPIENT=+1XXXXXXXXXX
    python scripts/smoke_sms.py

Sends one message, then polls until the carrier reports a terminal status. Exits non-zero
if it does not reach `delivered`.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

BASE = os.environ.get("SMOKE_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("SMOKE_TOKEN", "")
ORG_ID = os.environ.get("SMOKE_ORG_ID", "")
RECIPIENT = os.environ.get("BANDWIDTH_TEST_RECIPIENT", "")
BODY = os.environ.get("SMOKE_BODY", "CSaaS P1b smoke test - please ignore")
TIMEOUT_S = int(os.environ.get("SMOKE_TIMEOUT", "120"))


def _require() -> None:
    missing = [
        n
        for n, v in [
            ("SMOKE_BASE_URL", BASE),
            ("SMOKE_TOKEN", TOKEN),
            ("SMOKE_ORG_ID", ORG_ID),
            ("BANDWIDTH_TEST_RECIPIENT", RECIPIENT),
        ]
        if not v
    ]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)


async def main() -> int:
    _require()
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Org-Id": ORG_ID}

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        health = await client.get("/healthz")
        print(f"healthz    : {health.status_code} {health.text}")

        print(f"sending    : {RECIPIENT!r} <- {BODY!r}")
        r = await client.post(
            "/api/v1/messages", json={"to": RECIPIENT, "body": BODY}, headers=headers
        )
        if r.status_code != 201:
            print(f"FAILED     : HTTP {r.status_code} {r.text}", file=sys.stderr)
            return 1

        msg = r.json()
        print(
            f"created    : id={msg['id']} status={msg['status']} "
            f"est_segments={msg['segment_count_est']}"
        )
        if msg["status"] == "rejected":
            print(f"REJECTED   : error_code={msg['error_code']}", file=sys.stderr)
            if msg["error_code"] == "4476":
                print(
                    "             -> 4476 = number is not attached to a 10DLC campaign.\n"
                    "                This is the Track R tripwire; see docs/BRAND_REGISTRATION.md",
                    file=sys.stderr,
                )
            return 1

        print("polling    : waiting for the carrier's delivery receipt...")
        waited = 0
        status = msg["status"]
        while waited < TIMEOUT_S:
            await asyncio.sleep(3)
            waited += 3
            got = await client.get(f"/api/v1/messages/{msg['id']}", headers=headers)
            body = got.json()
            if body["status"] != status:
                status = body["status"]
                print(f"  +{waited:>3}s    : {status}")
            if status in ("delivered", "failed"):
                break

        if status == "delivered":
            print(f"\nDELIVERED  : carrier_segments={body['segment_count_carrier']}")
            return 0
        print(f"\nENDED as {status} (error_code={body.get('error_code')})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
