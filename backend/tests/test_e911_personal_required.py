"""Every person must register their own 911 address before calling or texting.

911/933 are never refused, and the gate lifts as soon as the person saves an address in
Settings, My profile (PUT /me/emergency-address).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.session import get_sessionmaker
from app.models import EmergencyAddress
from tests.conftest import auth_headers, make_org_with_number

LOCAL = "+12145550150"
CONTACT = "+19725550150"

ADDRESS = {
    "name": "Ana Agent",
    "street_address": "123 Main Street",
    "locality": "Springfield",
    "administrative_area": "IL",
    "postal_code": "62704",
    "country_code": "US",
}


async def _setup(app_with_carrier, email: str, *, required: bool = True):
    client, _fake, application = app_with_carrier
    application.state.settings.e911_personal_required = required
    token, org, _number = await make_org_with_number(client, email, "Org 911", LOCAL)
    return client, auth_headers(token, org["id"])


async def test_a_text_is_refused_until_the_person_adds_a_911_address(app_with_carrier):
    client, h = await _setup(app_with_carrier, "e911-text@example.com")

    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "e911_address_required"
    assert "911 address" in r.json()["error"]["message"]

    saved = await client.put("/api/v1/me/emergency-address", json=ADDRESS, headers=h)
    assert saved.status_code == 200, saved.text

    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201, r.text


async def test_the_address_is_saved_on_our_side_only(app_with_carrier):
    """No carrier key is configured here: saving must not need one (no carrier call)."""
    client, h = await _setup(app_with_carrier, "e911-local@example.com")

    saved = await client.put("/api/v1/me/emergency-address", json=ADDRESS, headers=h)
    assert saved.status_code == 200, saved.text

    async with get_sessionmaker()() as session:
        session.info["org_id"] = uuid.UUID(h["X-Org-Id"])
        row = (
            await session.execute(
                sa.select(EmergencyAddress).where(
                    EmergencyAddress.street_address == "123 Main Street"
                )
            )
        ).scalar_one()
        assert row.telnyx_address_id == ""  # not pushed to the carrier yet
        assert row.user_id is not None


async def test_an_ordinary_call_is_refused_without_a_911_address(app_with_carrier):
    client, h = await _setup(app_with_carrier, "e911-call@example.com")

    r = await client.post("/api/v1/calls", json={"to": CONTACT}, headers=h)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "e911_address_required"


async def test_dialing_911_is_never_refused_for_a_missing_address(app_with_carrier):
    client, h = await _setup(app_with_carrier, "e911-emergency@example.com")

    r = await client.post("/api/v1/calls", json={"to": "911"}, headers=h)
    if r.status_code >= 400:
        assert r.json()["error"]["code"] != "e911_address_required", r.text


async def test_the_switch_turns_the_gate_off(app_with_carrier):
    client, h = await _setup(app_with_carrier, "e911-off@example.com", required=False)

    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201, r.text
