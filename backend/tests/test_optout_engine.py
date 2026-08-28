"""Opt-out end to end — the phase's headline behaviour.

The claim being proved: **STOP to one number suppresses the whole pool.** That is gotcha #1
from the parity research, and it is the single most common way a compliance implementation
is quietly wrong.
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from app.compliance import service as compliance_svc
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import ComplianceBlock, ConsentEvent, Message
from tests.conftest import (
    auth_headers,
    fixture_bytes,
    make_org_with_number,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
NUM_A = "+12145550100"
NUM_B = "+12145550111"
CONTACT = "+19725550199"


async def _unscoped(session, model):
    return list(
        (
            await session.execute(
                sa.select(model).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )


async def _inbound(client, body: str, msg_id: str = "kw-1"):
    payload = json.loads(fixture_bytes("message-received.json"))
    payload[0]["message"]["id"] = msg_id
    payload[0]["message"]["text"] = body
    r = await client.post(
        HOOK, content=json.dumps(payload).encode(), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text
    return r


async def test_stop_to_one_number_suppresses_the_whole_pool(app_with_carrier, session):
    """GOTCHA #1. The whole point of keying consent on (org, contact) with no number."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo1@example.com", "Org A", NUM_A)
    h = auth_headers(token, org["id"])
    await client.post("/api/v1/numbers", json={"e164": NUM_B}, headers=h)

    # The contact texts STOP to number A.
    await _inbound(client, "STOP")

    # Sending from number B - a DIFFERENT number in the same org - must be refused.
    blocked = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "new promo", "from": NUM_B},
        headers=h,
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "compliance_blocked"

    # And from A.
    blocked_a = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "new promo", "from": NUM_A},
        headers=h,
    )
    assert blocked_a.status_code == 422

    # The denial is audited.
    blocks = await _unscoped(session, ComplianceBlock)
    assert len(blocks) == 2
    assert {b.reason for b in blocks} == {"opted_out"}
    assert {b.from_e164 for b in blocks} == {NUM_A, NUM_B}


async def test_stop_confirmation_is_sent_despite_the_optout(app_with_carrier):
    """CTIA requires the confirmation. It is the one permitted post-opt-out send."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo2@example.com", "Org A", NUM_A)
    before = len(fake.sent)

    await _inbound(client, "STOP")

    assert len(fake.sent) == before + 1, "the STOP confirmation must go out"
    body = fake.sent[-1].text.lower()
    assert "unsubscrib" in body
    # It must come FROM the number they texted, not a sticky-sender pick.
    assert fake.sent[-1].from_ == NUM_A
    assert fake.sent[-1].to == CONTACT


async def test_footer_text_does_not_opt_anyone_out(app_with_carrier, session):
    """THE INCIDENT. A body CONTAINING the word must never suppress anyone."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo3@example.com", "Org A", NUM_A)
    h = auth_headers(token, org["id"])

    await _inbound(client, "Reply STOP to unsubscribe", msg_id="footer-1")

    assert await _unscoped(session, ConsentEvent) == []
    ok = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "still fine"}, headers=h
    )
    assert ok.status_code == 201
    assert ok.json()["status"] == "accepted"


async def test_start_resubscribes(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo4@example.com", "Org A", NUM_A)
    h = auth_headers(token, org["id"])

    await _inbound(client, "STOP", msg_id="s-1")
    assert (
        await client.post("/api/v1/messages", json={"to": CONTACT, "body": "x"}, headers=h)
    ).status_code == 422

    await _inbound(client, "START", msg_id="s-2")
    resumed = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "welcome back"}, headers=h
    )
    assert resumed.status_code == 201


async def test_help_is_answered_even_when_opted_out(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo5@example.com", "Org A", NUM_A)

    await _inbound(client, "STOP", msg_id="h-1")
    count_after_stop = len(fake.sent)

    await _inbound(client, "HELP", msg_id="h-2")
    assert len(fake.sent) == count_after_stop + 1, "HELP must be answered after opt-out"
    assert "help" in fake.sent[-1].text.lower()


async def test_replayed_stop_does_not_double_reply(app_with_carrier, session):
    """The ledger's unique message_id makes keyword handling idempotent."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo6@example.com", "Org A", NUM_A)

    await _inbound(client, "STOP", msg_id="dup-1")
    after_first = len(fake.sent)
    await _inbound(client, "STOP", msg_id="dup-1")  # identical webhook, replayed

    assert len(fake.sent) == after_first, "a replay must not send a second confirmation"
    events = [e for e in await _unscoped(session, ConsentEvent) if e.event == "opt_out"]
    assert len(events) == 1


async def test_operator_cannot_undo_a_keyword_stop(app_with_carrier, session):
    """Only the consumer's own START reverses their STOP. An operator 'fixing' it is
    exactly the shape of a TCPA claim."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo7@example.com", "Org A", NUM_A)
    await _inbound(client, "STOP", msg_id="op-1")

    set_org_context(session, uuid.UUID(org["id"]))
    try:
        await compliance_svc.manual_opt_in(session, uuid.UUID(org["id"]), CONTACT)
        raise AssertionError("manual opt-in should have been refused")
    except Exception as exc:  # ConflictError
        assert "STOP keyword" in str(exc)


async def test_manual_optout_then_manual_optin_is_allowed(app_with_carrier, session):
    """A manual suppression may be undone manually - it was never the consumer's choice."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo8@example.com", "Org A", NUM_A)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    await compliance_svc.record_consent(
        session, org_id, contact_e164=CONTACT, event="opt_out", source="manual"
    )
    await session.commit()
    assert await compliance_svc.is_opted_out(session, CONTACT)

    await compliance_svc.manual_opt_in(session, org_id, CONTACT)
    await session.commit()
    assert not await compliance_svc.is_opted_out(session, CONTACT)


async def test_voice_channel_row_is_invisible_to_the_sms_gate(app_with_carrier, session):
    """P5 will hang recording consent off this same ledger via `channel`."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo9@example.com", "Org A", NUM_A)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    await compliance_svc.record_consent(
        session, org_id, contact_e164=CONTACT, event="opt_out",
        source="manual", channel="voice",
    )
    await session.commit()

    assert not await compliance_svc.is_opted_out(session, CONTACT, channel="sms")
    assert await compliance_svc.is_opted_out(session, CONTACT, channel="voice")


async def test_optout_is_scoped_to_the_org(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "ox1@example.com", "Org A", NUM_A)
    token_b, org_b, _ = await make_org_with_number(client, "ox2@example.com", "Org B", NUM_B)

    await _inbound(client, "STOP", msg_id="scope-1")  # lands on org A's number

    # Org B never heard from this contact; their send must not be blocked by org A's data.
    ok = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "hello from B"},
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert ok.status_code == 201


async def test_no_messages_row_is_created_on_deny(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "oo10@example.com", "Org A", NUM_A)
    h = auth_headers(token, org["id"])
    await _inbound(client, "STOP", msg_id="none-1")

    before = len(await _unscoped(session, Message))
    sent_before = len(fake.sent)
    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "x"}, headers=h)
    assert r.status_code == 422
    assert len(await _unscoped(session, Message)) == before, "a deny must cost nothing"
    assert len(fake.sent) == sent_before, "and must never reach the carrier"


async def test_same_timestamp_consent_pair_resolves_by_recording_order(
    app_with_carrier, session
):
    """The recorded coin-flip bug (PROGRESS 2026-08-26): created_at has finite resolution
    and the id is a random UUID, so an opt-out/opt-in pair landing in the same timestamp
    used to resolve at random. `seq` must make the LATER-recorded event win, always."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "seq1@example.com", "Org A", NUM_A)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    out = await compliance_svc.record_consent(
        session, org_id, contact_e164=CONTACT, event="opt_out", source="manual"
    )
    inn = await compliance_svc.record_consent(
        session, org_id, contact_e164=CONTACT, event="opt_in", source="manual"
    )
    # Force the exact degenerate case instead of hoping the clock produces it.
    inn.created_at = out.created_at
    await session.commit()

    assert inn.seq > out.seq
    for _ in range(5):
        latest = await compliance_svc.latest_consent(session, CONTACT)
        assert latest is not None and latest.event == "opt_in"
    assert not await compliance_svc.is_opted_out(session, CONTACT)
