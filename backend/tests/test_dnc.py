"""Internal DNC, scrub, and the honesty of the federal-DNC stub."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.compliance import service as svc
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import ConsentEvent
from tests.conftest import auth_headers, make_org_with_number

OUR = "+12145550100"
CONTACT = "+19725550199"
OTHER = "+19725550200"


async def test_dnc_blocks_a_send(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "d1@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    added = await client.post("/api/v1/compliance/dnc", json={"e164": CONTACT}, headers=h)
    assert added.status_code == 201

    blocked = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "compliance_blocked"
    assert fake.sent == []


async def test_dnc_add_and_remove_are_both_ledgered(app_with_carrier, session):
    """The working table is mutable; the append-only ledger stays the complete audit trail."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "d2@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await client.post(
        "/api/v1/compliance/dnc", json={"e164": CONTACT, "reason": "complained"}, headers=h
    )
    await client.delete(f"/api/v1/compliance/dnc/{CONTACT.replace('+', '%2B')}", headers=h)

    rows = list(
        (
            await session.execute(
                sa.select(ConsentEvent).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    events = [r.event for r in rows]
    assert "dnc_add" in events and "dnc_remove" in events


async def test_removing_dnc_unblocks(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "d3@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await client.post("/api/v1/compliance/dnc", json={"e164": CONTACT}, headers=h)
    await client.delete(f"/api/v1/compliance/dnc/{CONTACT.replace('+', '%2B')}", headers=h)

    ok = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert ok.status_code == 201


async def test_scrub_never_claims_federal_coverage(app_with_carrier):
    """The stub must be impossible to mistake for real scrubbing."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "d4@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await client.post("/api/v1/compliance/dnc", json={"e164": CONTACT}, headers=h)

    r = await client.post(
        "/api/v1/compliance/scrub", json={"numbers": [CONTACT, OTHER]}, headers=h
    )
    assert r.status_code == 200
    body = r.json()
    assert body["federal_dnc_checked"] is False

    by_number = {x["e164"]: x for x in body["results"]}
    assert by_number[CONTACT]["ok"] is False
    assert "dnc" in by_number[CONTACT]["reasons"]
    assert by_number[OTHER]["ok"] is True

    # EVERY result, even the clean one, admits the federal registry was not consulted.
    for result in body["results"]:
        assert result["federal_checked"] is False
        assert "federal_dnc:unchecked" in result["reasons"]


def test_no_setting_can_claim_federal_scrubbing():
    """There must be no configuration path that flips federal_checked to True."""
    from app.config import Settings

    s = Settings(jwt_secret="x", session_secret="y", _env_file=None)
    assert not any("federal" in f.lower() for f in s.model_fields), (
        "a federal-DNC settings flag would let a deployment believe it was scrubbing"
    )
    status = next(p for p in s.provider_statuses() if p.name == "federal_dnc")
    assert status.enabled is False
    assert "NOT scrubbed" in status.reason


async def test_scrub_reports_optout_too(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "d5@example.com", "Org A", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    await svc.record_consent(
        session, org_id, contact_e164=CONTACT, event="opt_out", source="manual"
    )
    await session.commit()

    r = await client.post(
        "/api/v1/compliance/scrub",
        json={"numbers": [CONTACT]},
        headers=auth_headers(token, org["id"]),
    )
    result = r.json()["results"][0]
    assert result["ok"] is False
    assert "opted_out" in result["reasons"]


async def test_dnc_is_org_scoped(app_with_carrier):
    client, _, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "d6@example.com", "Org A", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "d7@example.com", "Org B", "+12145550111"
    )
    await client.post(
        "/api/v1/compliance/dnc",
        json={"e164": CONTACT},
        headers=auth_headers(token_a, org_a["id"]),
    )

    listed = await client.get(
        "/api/v1/compliance/dnc", headers=auth_headers(token_b, org_b["id"])
    )
    assert listed.json() == []
    ok = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "hi"},
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert ok.status_code == 201


async def test_window_cannot_be_widened_past_the_federal_floor(app_with_carrier):
    """An org may narrow the sending window. It may never widen it."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "d8@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    r = await client.patch(
        "/api/v1/compliance/settings",
        json={"window_start": "06:00", "window_end": "23:00"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["window_start"] == "08:00", "start clamped up to the federal floor"
    assert r.json()["window_end"] == "21:00", "end clamped down to the federal floor"

    narrower = await client.patch(
        "/api/v1/compliance/settings",
        json={"window_start": "09:00", "window_end": "17:00"},
        headers=h,
    )
    assert narrower.json()["window_start"] == "09:00"
    assert narrower.json()["window_end"] == "17:00"
