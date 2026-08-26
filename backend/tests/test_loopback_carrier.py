from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ConfigurationError
from app.models import Message, MessageEvent
from tests.conftest import auth_headers, make_org_with_number

OUR = "+12145550100"
THEIRS = "+19725550199"


def test_production_guard_refuses_loopback():
    """A fake carrier must be impossible to run in production."""
    with pytest.raises(ConfigurationError) as exc:
        Settings(
            app_env="production",
            jwt_secret="x",
            session_secret="y",
            credential_encryption_key="bad-key",
            public_base_url="https://real.example.org",
            loopback_carrier_enabled=True,
            _env_file=None,
        )
    assert "LOOPBACK_CARRIER_ENABLED must be false" in str(exc.value)


def test_ambiguous_carrier_guard():
    with pytest.raises(ConfigurationError) as exc:
        Settings(
            jwt_secret="x",
            session_secret="y",
            loopback_carrier_enabled=True,
            bandwidth_enabled=True,
            _env_file=None,
        )
    assert "which carrier should send" in str(exc.value)


async def test_full_simulated_conversation(app_with_loopback, session):
    """The demo path, and proof it does NOT bypass the real pipeline.

    Everything downstream of the carrier - state machine, idempotency ledger, thread
    upsert, contact linkage, unread derivation - runs for real. Only the PSTN is fake.
    """
    client, carrier, _ = app_with_loopback
    token, org, _ = await make_org_with_number(client, "lb@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    sent = await client.post(
        "/api/v1/messages", json={"to": THEIRS, "body": "hello there"}, headers=h
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["status"] == "accepted"
    message_id = sent.json()["id"]

    # Deterministic: no sleeps, no flakiness.
    await carrier.drain()

    delivered = await client.get(f"/api/v1/messages/{message_id}", headers=h)
    assert delivered.json()["status"] == "delivered"

    # The echo reply landed in the SAME thread.
    thread_id = sent.json()["thread_id"]
    msgs = (await client.get(f"/api/v1/messages?thread_id={thread_id}", headers=h)).json()
    inbound = [m for m in msgs if m["direction"] == "inbound"]
    assert len(inbound) == 1
    assert inbound[0]["body"] == "echo: hello there"

    # It went through the LEDGER - proof the simulator used ingest_event, not a shortcut.
    events = (
        await session.execute(
            sa.select(MessageEvent).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    kinds = {e.event_type for e in events}
    assert {"message-sending", "message-delivered", "message-received"} <= kinds
    assert all(e.carrier == "loopback" for e in events)

    # And the inbox reflects it.
    inbox = (await client.get("/api/v1/inbox/threads", headers=h)).json()
    assert inbox["items"][0]["unread"] == 1
    assert inbox["items"][0]["last_message"]["body"] == "echo: hello there"


async def test_loopback_is_idempotent_on_replay(app_with_loopback, session):
    """Draining the same simulated events twice must not duplicate anything."""
    client, carrier, _ = app_with_loopback
    token, org, _ = await make_org_with_number(client, "lb2@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    sent = await client.post("/api/v1/messages", json={"to": THEIRS, "body": "hi"}, headers=h)
    provider_id = None
    await carrier.drain()

    before = (
        await session.execute(
            sa.select(sa.func.count(Message.id)).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()

    # Re-dispatch the identical events.
    events = carrier._events_for(  # noqa: SLF001 - deliberately exercising the dedupe path
        sent.json()["id"], type("M", (), {"to": THEIRS, "from_": OUR, "text": "hi"})()
    )
    assert provider_id is None
    await carrier._dispatch(events[:2])  # noqa: SLF001

    after = (
        await session.execute(
            sa.select(sa.func.count(Message.id)).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert after == before
