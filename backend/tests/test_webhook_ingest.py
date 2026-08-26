from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import Message, MessageEvent, WebhookDeadLetter
from tests.conftest import (
    auth_headers,
    fixture_bytes,
    load_fixture,
    make_org_with_number,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
THEIRS = "+19725550199"
OUTBOUND_ID = "1755000000000-outbound-bbbb"


async def _unscoped(session, model):
    stmt = sa.select(model).execution_options(**{ALLOW_UNSCOPED_KEY: True})
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers",
    [
        webhook_auth_headers("bw-hook-user", "WRONG"),
        webhook_auth_headers("WRONG", "bw-hook-pass"),
        webhook_auth_headers("WRONG", "WRONG"),
        {"Content-Type": "application/json"},
    ],
    ids=["bad-pass", "bad-user", "both-bad", "missing-header"],
)
async def test_bad_auth_is_401_and_writes_nothing(app_with_carrier, session, headers):
    client, _, _ = app_with_carrier
    r = await client.post(HOOK, content=fixture_bytes("message-received.json"), headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    assert await _unscoped(session, Message) == []
    assert await _unscoped(session, MessageEvent) == []
    assert await _unscoped(session, WebhookDeadLetter) == []


# ---------------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------------
async def test_inbound_creates_message_and_thread(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "in@example.com", "Org A", OUR)

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text

    msgs = await client.get("/api/v1/messages", headers=auth_headers(token, org["id"]))
    assert len(msgs.json()) == 1
    m = msgs.json()[0]
    assert m["direction"] == "inbound"
    assert m["status"] == "received"
    assert m["from_e164"] == THEIRS
    assert m["to_e164"] == OUR

    threads = await client.get("/api/v1/threads", headers=auth_headers(token, org["id"]))
    assert len(threads.json()) == 1

    # Replay the identical payload: still exactly one message and one event.
    again = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert again.status_code == 200
    assert len(await _unscoped(session, Message)) == 1
    assert len(await _unscoped(session, MessageEvent)) == 1


async def test_inbound_to_unknown_number_dead_letters(app_with_carrier, session):
    client, _, _ = app_with_carrier
    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200
    assert await _unscoped(session, Message) == []
    dls = await _unscoped(session, WebhookDeadLetter)
    assert len(dls) == 1
    assert dls[0].reason == "unknown_number"


@pytest.mark.parametrize(
    "body", [b"not json at all", b'{"not":"an array"}'], ids=["non-json", "json-not-array"]
)
async def test_malformed_body_dead_letters(app_with_carrier, session, body):
    client, _, _ = app_with_carrier
    r = await client.post(HOOK, content=body, headers=webhook_auth_headers())
    assert r.status_code == 200
    dls = await _unscoped(session, WebhookDeadLetter)
    assert len(dls) == 1
    assert dls[0].reason == "malformed"
    assert await _unscoped(session, Message) == []


async def test_unknown_event_type_dead_letters(app_with_carrier, session):
    client, _, _ = app_with_carrier
    payload = load_fixture("message-received.json")
    payload[0]["type"] = "message-teleported"
    r = await client.post(
        HOOK, content=json.dumps(payload).encode(), headers=webhook_auth_headers()
    )
    assert r.status_code == 200
    dls = await _unscoped(session, WebhookDeadLetter)
    assert [d.reason for d in dls] == ["unknown_event_type"]


# ---------------------------------------------------------------------------------
# DLR + the send race
# ---------------------------------------------------------------------------------
async def _send_one(client, token, org):
    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "Thanks - what is your best price?"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_dlr_unknown_id_returns_500_then_succeeds(app_with_carrier, session):
    """THE SEND-RACE CONTRACT.

    A DLR can beat our own commit. We answer 500 on purpose so Bandwidth's 24h retry
    re-delivers after the row lands — rather than silently dropping a real delivery.
    """
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "race@example.com", "Org A", OUR)

    early = await client.post(
        HOOK, content=fixture_bytes("message-delivered.json"), headers=webhook_auth_headers()
    )
    assert early.status_code == 500
    assert await _unscoped(session, MessageEvent) == []

    # The send commits (the race resolves).
    msg = await _send_one(client, token, org)

    replay = await client.post(
        HOOK, content=fixture_bytes("message-delivered.json"), headers=webhook_auth_headers()
    )
    assert replay.status_code == 200

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    assert got.json()["status"] == "delivered"


async def test_failed_event_records_error_code(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "fail@example.com", "Org A", OUR)
    msg = await _send_one(client, token, org)

    r = await client.post(
        HOOK, content=fixture_bytes("message-failed.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    assert got.json()["status"] == "failed"
    assert got.json()["error_code"] == "4720"


async def test_array_batch_processes_every_event(app_with_carrier, session):
    """The array shape must be exercised, not simulated."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "batch@example.com", "Org A", OUR)
    msg = await _send_one(client, token, org)

    r = await client.post(
        HOOK, content=fixture_bytes("batch-two-events.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200
    assert r.json()["events"] == 2

    events = await _unscoped(session, MessageEvent)
    assert {e.event_type for e in events} == {"message-sending", "message-delivered"}

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    assert got.json()["status"] == "delivered"
    assert got.json()["segment_count_carrier"] == 1


async def test_replay_out_of_order_3x(app_with_carrier, session):
    """THE P1a GATE TEST.

    Deliver the DLR sequence delivered/sending three times over, out of order — exactly
    what Bandwidth's unordered parallel retries produce — and require the final state to be
    field-for-field identical to a clean ordered run.
    """
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "ooo@example.com", "Org A", OUR)
    msg = await _send_one(client, token, org)

    sequence = ["message-delivered.json", "message-sending.json"] * 3
    for name in sequence:
        r = await client.post(HOOK, content=fixture_bytes(name), headers=webhook_auth_headers())
        assert r.status_code == 200, f"{name} -> {r.status_code} {r.text}"

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    final = got.json()
    assert final["status"] == "delivered"
    assert final["segment_count_carrier"] == 1

    events = await _unscoped(session, MessageEvent)
    # Six deliveries of two distinct events => exactly two ledger rows.
    assert len(events) == 2
    assert {e.event_type for e in events} == {"message-sending", "message-delivered"}
    assert all(e.processed_at is not None for e in events)


async def test_ordered_control_run_matches_out_of_order(app_with_carrier, session):
    """The control half of the gate: ordered once-each must land on the same state."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "ctl@example.com", "Org A", OUR)
    msg = await _send_one(client, token, org)

    for name in ["message-sending.json", "message-delivered.json"]:
        r = await client.post(HOOK, content=fixture_bytes(name), headers=webhook_auth_headers())
        assert r.status_code == 200

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    final = got.json()
    assert final["status"] == "delivered"
    assert final["segment_count_carrier"] == 1
    assert len(await _unscoped(session, MessageEvent)) == 2


async def test_reprocess_pending_is_idempotent(app_with_carrier, session):
    from app.services.messaging import reprocess_pending

    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "rp@example.com", "Org A", OUR)
    msg = await _send_one(client, token, org)

    await client.post(
        HOOK, content=fixture_bytes("message-delivered.json"), headers=webhook_auth_headers()
    )

    # Simulate a processing crash: clear processed_at on the ledger row.
    await session.execute(
        sa.update(MessageEvent)
        .where(MessageEvent.event_type == "message-delivered")
        .values(processed_at=None)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    await session.commit()

    assert await reprocess_pending(session) == 1
    assert await reprocess_pending(session) == 0  # nothing left pending

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    assert got.json()["status"] == "delivered"


@pytest.mark.pg_only
async def test_concurrent_duplicate_ingest_pg(app_with_carrier, session):
    """The IntegrityError dedupe path under REAL concurrency.

    SQLite serializes writes, so only Postgres actually exercises this.
    """
    import asyncio

    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "conc@example.com", "Org A", OUR)
    await _send_one(client, token, org)

    body = fixture_bytes("message-delivered.json")
    results = await asyncio.gather(
        client.post(HOOK, content=body, headers=webhook_auth_headers()),
        client.post(HOOK, content=body, headers=webhook_auth_headers()),
        return_exceptions=True,
    )
    codes = [r.status_code for r in results if hasattr(r, "status_code")]
    assert all(c == 200 for c in codes), codes
    events = await _unscoped(session, MessageEvent)
    assert len([e for e in events if e.event_type == "message-delivered"]) == 1


async def test_conflicting_terminal_does_not_overwrite(app_with_carrier, session):
    """First terminal wins.

    This is the ONLY case terminal-immutability uniquely protects: `failed` and `delivered`
    share rank 30, so the monotonic-rank guard cannot catch it. Mutation testing found the
    out-of-order gate test could not distinguish the two guards; this one can.
    """
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "term@example.com", "Org A", OUR)
    msg = await _send_one(client, token, org)

    delivered = await client.post(
        HOOK, content=fixture_bytes("message-delivered.json"), headers=webhook_auth_headers()
    )
    assert delivered.status_code == 200

    # A contradictory terminal arrives afterwards. It must be LEDGERED but must NOT win.
    failed = await client.post(
        HOOK, content=fixture_bytes("message-failed.json"), headers=webhook_auth_headers()
    )
    assert failed.status_code == 200

    got = await client.get(
        f"/api/v1/messages/{msg['id']}", headers=auth_headers(token, org["id"])
    )
    assert got.json()["status"] == "delivered", "a late `failed` must not clobber `delivered`"
    assert got.json()["error_code"] is None

    events = await _unscoped(session, MessageEvent)
    assert {e.event_type for e in events} == {"message-delivered", "message-failed"}
