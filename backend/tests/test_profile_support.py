"""Profile (name + 911 address) and the Help menu (contacts + support requests).

/me/profile, /me/emergency-address, /support/* for members, plus the ops console's Support
tab (/api/v1/ops/console/support*). Carrier traffic is faked: services/e911.create_address
and services/e911.enable are replaced with in-process stand-ins, and
services/profile.assigned_numbers is patched so a person "holds" numbers without seeding
OrgNumber rows.
"""

from __future__ import annotations

import types

from app.models import EmergencyAddress
from app.services import e911, mailer, profile
from tests.conftest import auth_headers, create_org, register_and_login
from tests.test_ops_console import _operator, ops, ops_settings  # noqa: F401


async def test_profile_defaults(client, session):
    mailer.outbox.clear()
    email = "ana@example.com"
    token = await register_and_login(client, email)
    org = await create_org(client, token, "Ana Org")
    h = auth_headers(token, org["id"])

    r = await client.get("/api/v1/me/profile", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_name"] == email.split("@")[0]
    assert body["email"] == email
    assert body["emergency_address"] is None
    assert body["numbers"] == []


async def test_update_profile_name(client, session):
    mailer.outbox.clear()
    token = await register_and_login(client, "ana-agent@example.com")
    org = await create_org(client, token, "Ana Agent Org")
    h = auth_headers(token, org["id"])

    r = await client.put("/api/v1/me/profile", json={"full_name": "Ana Agent"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Ana Agent"

    r = await client.put("/api/v1/me/profile", json={"full_name": "  "}, headers=h)
    assert r.status_code == 422, r.text


async def test_set_emergency_address_applies_to_numbers(client, session, monkeypatch):
    mailer.outbox.clear()
    enabled = []

    async def fake_create_address(session, settings, org_id, fields):
        address = EmergencyAddress(**fields, telnyx_address_id="addr_test", org_id=org_id)
        session.add(address)
        await session.flush()
        return address

    async def fake_enable(session, settings, number, address):
        enabled.append((number.e164, address.id))
        number.provisioning = {
            **(number.provisioning or {}),
            "e911": {"address_id": str(address.id), "status": "active"},
        }

    monkeypatch.setattr(e911, "create_address", fake_create_address)
    monkeypatch.setattr(e911, "enable", fake_enable)

    numbers = [
        types.SimpleNamespace(e164="+14695550001", provisioning={}),
        types.SimpleNamespace(e164="+14695550002", provisioning={}),
    ]

    async def fake_assigned_numbers(session, org_id, user_id):
        return numbers

    monkeypatch.setattr(profile, "assigned_numbers", fake_assigned_numbers)

    street = "123 Main Street"
    token = await register_and_login(client, "ana-911@example.com")
    org = await create_org(client, token, "Ana 911 Org")
    h = auth_headers(token, org["id"])

    r = await client.put(
        "/api/v1/me/emergency-address",
        json={
            "name": "Ana Agent",
            "street_address": street,
            "locality": "Springfield",
            "administrative_area": "IL",
            "postal_code": "62704",
            "country_code": "US",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["emergency_address"] is not None
    assert street in body["emergency_address"]["label"]
    assert body["applied"] == 2
    assert {e164 for e164, _ in enabled} == {"+14695550001", "+14695550002"}
    assert len(body["numbers"]) == 2
    assert all(number["mine"] is True for number in body["numbers"])

    r = await client.get("/api/v1/me/profile", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["emergency_address"] == body["emergency_address"]


async def test_support_contacts_default(client, session):
    mailer.outbox.clear()
    token = await register_and_login(client, "help-default@example.com")
    org = await create_org(client, token, "Help Default Org")
    h = auth_headers(token, org["id"])

    r = await client.get("/api/v1/support/contacts", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] is None
    assert body["status_url"] == "/status"


async def test_support_request_create_and_list(client, session):
    mailer.outbox.clear()
    token = await register_and_login(client, "help-seeker@example.com")
    org = await create_org(client, token, "Help Seeker Org")
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/support/requests",
        json={"subject": "Help", "body": "My calls drop"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "open"
    assert created["subject"] == "Help"

    r = await client.get("/api/v1/support/requests", headers=h)
    assert r.status_code == 200, r.text
    assert created["id"] in [item["id"] for item in r.json()["requests"]]

    r = await client.post(
        "/api/v1/support/requests",
        json={"subject": "", "body": "no subject"},
        headers=h,
    )
    assert r.status_code == 422, r.text


async def test_ops_support_list_and_reply(ops, session):
    mailer.outbox.clear()
    member_email = "member-reply@example.com"
    member_token = await register_and_login(ops, member_email)
    org = await create_org(ops, member_token, "Reply Org")
    mh = auth_headers(member_token, org["id"])

    r = await ops.post(
        "/api/v1/support/requests",
        json={"subject": "Need help", "body": "My calls drop"},
        headers=mh,
    )
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]

    op_token = await _operator(ops, session, email="ops-reply@example.com")
    oh = auth_headers(op_token)

    r = await ops.get("/api/v1/ops/console/support", headers=oh)
    assert r.status_code == 200, r.text
    listed = {item["id"]: item for item in r.json()["requests"]}
    assert request_id in listed
    assert listed[request_id]["org_name"] == "Reply Org"

    mailer.outbox.clear()
    r = await ops.post(
        f"/api/v1/ops/console/support/{request_id}/reply",
        json={"reply": "Fixed", "close": True},
        headers=oh,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed"
    assert body["emailed"] is True
    assert mailer.outbox, "expected the reply to be emailed"
    assert any(member_email in str(message) for message in mailer.outbox)


async def test_ops_support_contacts(ops, session):
    mailer.outbox.clear()
    op_token = await _operator(ops, session, email="ops-contacts@example.com")
    oh = auth_headers(op_token)

    r = await ops.put(
        "/api/v1/ops/console/support-contacts",
        json={"email": "help@x.test", "knowledge_base_url": "ftp://bad"},
        headers=oh,
    )
    assert r.status_code == 422, r.text

    r = await ops.put(
        "/api/v1/ops/console/support-contacts",
        json={"email": "help@x.test", "knowledge_base_url": "https://kb.x.test"},
        headers=oh,
    )
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["email"] == "help@x.test"
    assert saved["knowledge_base_url"] == "https://kb.x.test"

    member_token = await register_and_login(ops, "member-contacts@example.com")
    org = await create_org(ops, member_token, "Contacts Org")
    mh = auth_headers(member_token, org["id"])

    r = await ops.get("/api/v1/support/contacts", headers=mh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "help@x.test"
    assert body["knowledge_base_url"] == "https://kb.x.test"
