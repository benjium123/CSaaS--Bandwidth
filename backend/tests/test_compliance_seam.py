"""The compliance seam is pinned by tests so P3 FILLS IT IN rather than retrofitting
checks into N call sites later. If someone adds a send path that bypasses the gate, these
go red."""

from __future__ import annotations

import sqlalchemy as sa

from app.compliance import gate
from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import Message
from tests.conftest import (
    auth_headers,
    fixture_bytes,
    make_org_with_number,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
THEIRS = "+19725550199"


async def test_gate_called_exactly_once_per_send(app_with_carrier, monkeypatch):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c1@example.com", "Org A", OUR)

    calls = []
    original = gate.check_outbound

    async def spy(session, org_id, draft):
        calls.append(draft)
        return await original(session, org_id, draft)

    monkeypatch.setattr("app.services.messaging.gate.check_outbound", spy)

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hello"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201
    assert len(calls) == 1
    assert calls[0].to_e164 == THEIRS
    assert calls[0].from_e164 == OUR
    assert calls[0].body == "hello"


async def test_deny_blocks_before_any_row_or_carrier_call(app_with_carrier, session, monkeypatch):
    """A deny must cost NOTHING: no message row, no carrier call."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c2@example.com", "Org A", OUR)

    async def deny(session_, org_id, draft):
        return gate.ComplianceVerdict(allowed=False, reason="recipient has opted out")

    monkeypatch.setattr("app.services.messaging.gate.check_outbound", deny)

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hello"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "compliance_blocked"
    assert "opted out" in r.json()["error"]["message"]

    assert fake.sent == []
    rows = (
        await session.execute(
            sa.select(Message).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    assert list(rows) == []


async def test_on_inbound_invoked_once_per_ingested_inbound(app_with_carrier, monkeypatch):
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "c3@example.com", "Org A", OUR)

    calls = []

    async def spy(session, org_id, message_id):
        calls.append(message_id)

    monkeypatch.setattr("app.services.messaging.gate.on_inbound", spy)

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200
    assert len(calls) == 1

    # A replayed duplicate must NOT re-fire the hook.
    r2 = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r2.status_code == 200
    assert len(calls) == 1
