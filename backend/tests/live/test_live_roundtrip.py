"""P1b — LIVE carrier round-trip. Skipped unless real credentials are present.

These run at HTTP level against a DEPLOYED instance, not the app factory: DLR webhooks need
a public callback URL that Bandwidth can actually reach.

Enable with:
    BANDWIDTH_LIVE_TEST=1
    SMOKE_BASE_URL=https://your-host
    SMOKE_TOKEN=<jwt>  SMOKE_ORG_ID=<uuid>  BANDWIDTH_TEST_RECIPIENT=+1...
Inbound half additionally needs BANDWIDTH_LIVE_INBOUND=1 and a human to reply.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.live_carrier

BASE = os.environ.get("SMOKE_BASE_URL", "")
TOKEN = os.environ.get("SMOKE_TOKEN", "")
ORG_ID = os.environ.get("SMOKE_ORG_ID", "")
RECIPIENT = os.environ.get("BANDWIDTH_TEST_RECIPIENT", "")

_ENABLED = os.environ.get("BANDWIDTH_LIVE_TEST") == "1" and all([BASE, TOKEN, ORG_ID, RECIPIENT])

pytestmark = [
    pytest.mark.live_carrier,
    pytest.mark.skipif(
        not _ENABLED,
        reason="live carrier test needs BANDWIDTH_LIVE_TEST=1 + SMOKE_* + a test recipient",
    ),
]


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "X-Org-Id": ORG_ID}


async def test_outbound_reaches_delivered():
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        r = await client.post(
            "/api/v1/messages",
            json={"to": RECIPIENT, "body": "CSaaS P1b live round-trip test"},
            headers=_headers(),
        )
        assert r.status_code == 201, r.text
        message_id = r.json()["id"]
        assert r.json()["status"] == "accepted", r.json()

        deadline = 120
        for _ in range(deadline // 3):
            await asyncio.sleep(3)
            got = await client.get(f"/api/v1/messages/{message_id}", headers=_headers())
            body = got.json()
            if body["status"] in ("delivered", "failed"):
                break

        assert body["status"] == "delivered", f"ended as {body['status']} ({body['error_code']})"
        # The carrier's count is truth; proves the DLR path populated it.
        assert body["segment_count_carrier"] is not None


@pytest.mark.skipif(
    os.environ.get("BANDWIDTH_LIVE_INBOUND") != "1",
    reason="needs a human to reply from the handset",
)
async def test_inbound_reply_lands_in_same_thread():
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        threads = await client.get("/api/v1/threads", headers=_headers())
        assert threads.status_code == 200
        thread = next(t for t in threads.json() if t["contact_e164"] == RECIPIENT)

        print(f"\n>>> Reply from {RECIPIENT} now. Waiting up to 180s...\n")
        for _ in range(60):
            await asyncio.sleep(3)
            msgs = await client.get(
                f"/api/v1/messages?thread_id={thread['id']}", headers=_headers()
            )
            inbound = [m for m in msgs.json() if m["direction"] == "inbound"]
            if inbound:
                assert inbound[-1]["status"] == "received"
                return
        pytest.fail("no inbound message arrived within 180s")
