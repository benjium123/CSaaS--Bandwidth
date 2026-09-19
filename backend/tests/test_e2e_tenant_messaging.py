"""End-to-end tenancy + billing proofs over the SIMULATED loopback carrier.

What this file proves
- Two separate orgs, each with its own number, can only ever send FROM their own number:
  a cross-tenant ``from`` is refused as a validation error and writes nothing anywhere.
- An outbound SMS the carrier accepts is delivered and charged exactly once at the org's
  live per-segment price, and the inbound echo it produces is charged once at the inbound
  price - the two ledger entries and the resulting balance are asserted exactly.
- An inbound message is visible to the org that owns the receiving number and to no other
  org, both in the tenant-scoped data layer and through the API.
- The KYC gate both blocks an unverified org and lets an approved one through.

IMPORTANT: ``LoopbackCarrier`` is a SIMULATION. There is no real carrier, no PSTN and no
real money anywhere in this file. It proves the platform's own send -> delivery -> billing
-> tenancy path, NOT anything about Bandwidth, Twilio or the public telephone network.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import KycProfile
from tests.conftest import auth_headers, make_org_with_number, register_and_login
from tests.e2e_billing_helpers import (
    balance,
    enable_prepaid,
    messages_of,
    price_of,
    seed_rates,
    usage_entries,
)


async def test_two_orgs_send_from_their_own_numbers_only(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    ext = "+14695550103"

    token_a, org_a, number_a = await make_org_with_number(
        client, "tenant-a-1@example.com", "Org A", "+12145550101"
    )
    token_b, org_b, number_b = await make_org_with_number(
        client, "tenant-b-1@example.com", "Org B", "+19725550102"
    )
    await seed_rates(session, org_a["id"])
    await seed_rates(session, org_b["id"])
    await enable_prepaid(session, org_a["id"], balance_micros=1_000_000)
    await enable_prepaid(session, org_b["id"], balance_micros=1_000_000)

    ha = auth_headers(token_a, org_a["id"])
    hb = auth_headers(token_b, org_b["id"])

    resp_a = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "from org a"}, headers=ha
    )
    assert resp_a.status_code == 201, resp_a.text

    resp_b = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "from org b"}, headers=hb
    )
    assert resp_b.status_code == 201, resp_b.text

    out_a = [m for m in await messages_of(session, org_a["id"]) if m.direction == "outbound"]
    out_b = [m for m in await messages_of(session, org_b["id"]) if m.direction == "outbound"]
    assert len(out_a) == 1, [m.body for m in out_a]
    assert out_a[0].from_e164 == number_a["e164"]
    assert len(out_b) == 1, [m.body for m in out_b]
    assert out_b[0].from_e164 == number_b["e164"]

    count_a_before = len(await messages_of(session, org_a["id"]))
    count_b_before = len(await messages_of(session, org_b["id"]))

    cross = await client.post(
        "/api/v1/messages",
        json={"to": ext, "from": number_b["e164"], "body": "cross tenant attempt"},
        headers=ha,
    )
    assert cross.status_code == 422, cross.text
    assert cross.json()["error"]["code"] == "validation_failed"

    msgs_a = await messages_of(session, org_a["id"])
    msgs_b = await messages_of(session, org_b["id"])
    assert len(msgs_a) == count_a_before
    assert len(msgs_b) == count_b_before
    assert all(m.body != "cross tenant attempt" for m in msgs_a)
    assert all(m.body != "cross tenant attempt" for m in msgs_b)


async def test_outbound_sms_is_delivered_and_charged(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    ext = "+14695550105"

    token, org, _number = await make_org_with_number(
        client, "tenant-a-2@example.com", "Org A", "+12145550104"
    )
    await seed_rates(session, org["id"])
    await enable_prepaid(session, org["id"], balance_micros=1_000_000)
    h = auth_headers(token, org["id"])

    before = await balance(session, org["id"])

    sent = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "single segment"}, headers=h
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["status"] == "accepted"
    message_id = sent.json()["id"]

    await carrier.drain()

    delivered = await client.get(f"/api/v1/messages/{message_id}", headers=h)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["status"] == "delivered"

    per_sms = await price_of(session, org["id"], "sms_out")
    per_sms_in = await price_of(session, org["id"], "sms_in")

    entries = await usage_entries(session, org["id"])

    outbound = [e for e in entries if e.reference == f"sms:{message_id}"]
    assert len(outbound) == 1, [e.reference for e in entries]
    assert outbound[0].amount_micros == -per_sms

    inbound_msgs = [
        m for m in await messages_of(session, org["id"]) if m.direction == "inbound"
    ]
    assert len(inbound_msgs) == 1, [(m.body, m.direction) for m in inbound_msgs]
    inbound_ref = f"sms:{inbound_msgs[0].id}"

    inbound = [e for e in entries if e.reference == inbound_ref]
    assert len(inbound) == 1, [e.reference for e in entries]
    assert inbound[0].amount_micros == -per_sms_in

    after = await balance(session, org["id"])
    assert after == before - per_sms - per_sms_in


async def test_inbound_echo_lands_in_org_a_and_not_org_b(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    ext = "+14695550108"

    token_a, org_a, _number_a = await make_org_with_number(
        client, "tenant-a-3@example.com", "Org A", "+12145550106"
    )
    token_b, org_b, _number_b = await make_org_with_number(
        client, "tenant-b-3@example.com", "Org B", "+19725550107"
    )
    await seed_rates(session, org_a["id"])
    await seed_rates(session, org_b["id"])
    await enable_prepaid(session, org_a["id"], balance_micros=1_000_000)
    await enable_prepaid(session, org_b["id"], balance_micros=1_000_000)

    ha = auth_headers(token_a, org_a["id"])
    hb = auth_headers(token_b, org_b["id"])

    sent = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "ping"}, headers=ha
    )
    assert sent.status_code == 201, sent.text
    thread_id = sent.json()["thread_id"]

    await carrier.drain()

    inbound_a = [
        m for m in await messages_of(session, org_a["id"]) if m.direction == "inbound"
    ]
    assert len(inbound_a) == 1, [(m.body, m.direction) for m in inbound_a]
    assert inbound_a[0].body == "echo: ping"
    assert str(inbound_a[0].thread_id) == str(thread_id)

    assert await messages_of(session, org_b["id"]) == []

    via_thread = await client.get(f"/api/v1/messages?thread_id={thread_id}", headers=ha)
    assert via_thread.status_code == 200, via_thread.text
    thread_messages = via_thread.json()
    assert any(
        m["direction"] == "inbound" and m["body"] == "echo: ping"
        for m in thread_messages
    ), thread_messages

    inbox_b = await client.get("/api/v1/inbox/threads", headers=hb)
    assert inbox_b.status_code == 200, inbox_b.text
    b_items = inbox_b.json()["items"]
    assert not any(
        (item.get("last_message") or {}).get("body") == "echo: ping" for item in b_items
    ), b_items


@pytest.fixture
async def app_with_loopback_kyc(engine):
    from app import config
    from app.main import create_app
    from app.providers.loopback import LoopbackCarrier
    from app.providers.registry import CarrierRegistry
    from tests.conftest import make_settings

    settings = make_settings(kyc_enforced=True)
    config.set_active_settings(settings)
    application = create_app(settings)
    carrier = LoopbackCarrier(auto=False)
    application.state.carriers = CarrierRegistry({carrier.name: carrier}, primary=carrier.name)
    application.state.carrier = carrier
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, carrier, application


def _org_id_from_me(payload: dict) -> uuid.UUID:
    """GET /api/v1/auth/me returns MeOut: memberships is a list of MembershipOut, each
    carrying org_id. Assert the exact shape rather than guessing at field names."""
    memberships = payload["memberships"]
    assert len(memberships) == 1, memberships
    return uuid.UUID(str(memberships[0]["org_id"]))


async def test_kyc_gate_blocks_unverified_org_and_approved_org_can_send(
    app_with_loopback_kyc, session
):
    """The ONLY test in the suite that exercises the verification gate for real.

    The shared conftest sets KYC_ENFORCED off for every test (production defaults it on),
    so without this test nothing in the suite would ever prove the gate exists at all.
    """
    client, _carrier, _app = app_with_loopback_kyc
    ext = "+14695550110"

    # Registration with KYC enforced already created a workspace AND a draft KycProfile,
    # so we must not call make_org_with_number: a second workspace is refused with 409
    # kyc_pending_elsewhere while the first is unverified.
    token = await register_and_login(client, "tenant-kyc-1@example.com")
    me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200, me.text
    org_uuid = _org_id_from_me(me.json())
    org_id = str(org_uuid)
    h = auth_headers(token, org_id)

    async def _profile() -> KycProfile:
        set_org_context(session, org_uuid)
        return (
            await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_uuid))
        ).scalar_one()

    # Adding a number is gated by KYC too (kind "number"), so approve the existing draft
    # profile first - update it, never insert a second row for the org.
    profile = await _profile()
    profile.status = "approved"
    await session.commit()

    added = await client.post(
        "/api/v1/numbers", json={"e164": "+12145550109"}, headers=h
    )
    assert added.status_code == 201, added.text

    await seed_rates(session, org_uuid)
    await enable_prepaid(session, org_uuid, balance_micros=1_000_000)

    # Now put the org back into an unverified state to prove the gate actually bites.
    profile = await _profile()
    profile.status = "submitted"
    await session.commit()

    outbound_before = [
        m for m in await messages_of(session, org_uuid) if m.direction == "outbound"
    ]

    blocked = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "not verified"}, headers=h
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "account_not_verified"

    outbound_after = [
        m for m in await messages_of(session, org_uuid) if m.direction == "outbound"
    ]
    assert len(outbound_after) == len(outbound_before)
    assert all(m.body != "not verified" for m in outbound_after)

    # Back to approved: telephony is allowed for KYC_TELEPHONY_STATUSES == {"approved",
    # "reverification_due"}, so now the send goes through.
    profile = await _profile()
    profile.status = "approved"
    await session.commit()

    allowed = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "now verified"}, headers=h
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["status"] == "accepted"
