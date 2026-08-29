"""P13 DR-6: audit log - wired actions record actor/action/target, an API-key actor is
recorded distinctly from a human one, and the read endpoint filters + paginates."""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.models import Contact, ContactList, ContactListRow
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

#: Overrides conftest's `settings` fixture for every test in this module - `engine`/
#: `client` both depend on it by name, so webhook-endpoint routes here can actually
#: encrypt a secret (create_endpoint 503s without CREDENTIAL_ENCRYPTION_KEY).
FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def settings():
    return make_settings(credential_encryption_key=FERNET_KEY)


async def _org(client, email: str, name: str):
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, org


async def _ready_list(session, org_id: uuid.UUID, e164: str) -> ContactList:
    """A `ready` ContactList with one `accepted` row, built directly - bypassing the
    upload/import pipeline (covered by test_list_import.py) keeps this focused on audit."""
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(),
        org_id=org_id,
        name="L",
        source_filename="l.csv",
        status="ready",
        total_rows=1,
        accepted_count=1,
    )
    session.add(lst)
    await session.flush()
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name=e164)
    session.add(contact)
    await session.flush()
    session.add(
        ContactListRow(
            id=uuid.uuid4(),
            org_id=org_id,
            list_id=lst.id,
            row_number=1,
            raw={"phone": e164},
            e164=e164,
            contact_id=contact.id,
            status="accepted",
            fields={},
        )
    )
    await session.commit()
    return lst


async def test_apikey_created_action_records_actor_and_target(client):
    token, org = await _org(client, "aud1@example.com", "Org AUD1")
    h = auth_headers(token, org["id"])

    created = await client.post(
        "/api/v1/api-keys", json={"name": "k", "scopes": ["contacts:read"]}, headers=h
    )
    key_id = created.json()["id"]

    listed = await client.get("/api/v1/audit", headers=h)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    entry = next(i for i in items if i["action"] == "apikey.created")
    assert entry["target_type"] == "api_key"
    assert entry["target_id"] == key_id
    assert entry["actor_user_id"] is not None
    assert entry["actor_api_key_id"] is None
    assert entry["detail"]["prefix"]
    assert "key_hash" not in entry["detail"]


async def test_webhook_endpoint_crud_actions_are_audited(client):
    token, org = await _org(client, "aud2@example.com", "Org AUD2")
    h = auth_headers(token, org["id"])

    created = await client.post(
        "/api/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "event_types": ["message.received"]},
        headers=h,
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]

    updated = await client.patch(
        f"/api/v1/webhook-endpoints/{endpoint_id}", json={"status": "disabled"}, headers=h
    )
    assert updated.status_code == 200, updated.text

    deleted = await client.delete(f"/api/v1/webhook-endpoints/{endpoint_id}", headers=h)
    assert deleted.status_code == 204, deleted.text

    listed = await client.get("/api/v1/audit", headers=h)
    actions = [i["action"] for i in listed.json()["items"]]
    assert "webhook_endpoint.created" in actions
    assert "webhook_endpoint.updated" in actions
    assert "webhook_endpoint.deleted" in actions


async def test_campaign_lifecycle_actions_are_audited(client, session):
    token, org = await _org(client, "aud3@example.com", "Org AUD3")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    lst = await _ready_list(session, org_id, "+19725550100")

    created = await client.post(
        "/api/v1/outbound/campaigns",
        json={"name": "C1", "channel": "sms", "list_id": str(lst.id), "body": "hi there"},
        headers=h,
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]

    started = await client.post(f"/api/v1/outbound/campaigns/{campaign_id}/start", headers=h)
    assert started.status_code == 200, started.text
    paused = await client.post(f"/api/v1/outbound/campaigns/{campaign_id}/pause", headers=h)
    assert paused.status_code == 200, paused.text
    cancelled = await client.post(f"/api/v1/outbound/campaigns/{campaign_id}/cancel", headers=h)
    assert cancelled.status_code == 200, cancelled.text

    listed = await client.get("/api/v1/audit", headers=h)
    entries = {i["action"]: i for i in listed.json()["items"]}
    assert entries["campaign.started"]["target_id"] == campaign_id
    assert entries["campaign.paused"]["target_id"] == campaign_id
    assert entries["campaign.cancelled"]["target_id"] == campaign_id


async def test_api_key_actor_is_recorded_distinctly_from_a_human_actor(client):
    token, org = await _org(client, "aud4@example.com", "Org AUD4")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "writer", "scopes": ["contacts:write", "org:update"]},
        headers=h,
    )
    full_key = created.json()["key"]
    key_id = created.json()["id"]

    r = await client.post(
        "/api/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "event_types": ["message.received"]},
        headers={"Authorization": f"Bearer {full_key}"},
    )
    assert r.status_code == 201, r.text

    listed = await client.get("/api/v1/audit", headers=h)
    entry = next(
        i for i in listed.json()["items"] if i["action"] == "webhook_endpoint.created"
    )
    assert entry["actor_api_key_id"] == key_id
    assert entry["actor_user_id"] is None


async def test_audit_read_filters_by_action_and_target_type(client):
    token, org = await _org(client, "aud5@example.com", "Org AUD5")
    h = auth_headers(token, org["id"])
    await client.post(
        "/api/v1/api-keys", json={"name": "k1", "scopes": ["contacts:read"]}, headers=h
    )
    await client.post(
        "/api/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "event_types": ["message.received"]},
        headers=h,
    )

    only_keys = await client.get(
        "/api/v1/audit", params={"target_type": "api_key"}, headers=h
    )
    assert all(i["target_type"] == "api_key" for i in only_keys.json()["items"])

    only_created = await client.get(
        "/api/v1/audit", params={"action": "apikey.created"}, headers=h
    )
    assert all(i["action"] == "apikey.created" for i in only_created.json()["items"])
    assert len(only_created.json()["items"]) == 1


async def test_audit_read_paginates_with_a_cursor(client):
    token, org = await _org(client, "aud6@example.com", "Org AUD6")
    h = auth_headers(token, org["id"])
    for i in range(5):
        r = await client.post(
            "/api/v1/api-keys", json={"name": f"k{i}", "scopes": ["contacts:read"]}, headers=h
        )
        assert r.status_code == 201, r.text

    page1 = await client.get("/api/v1/audit", params={"limit": 2}, headers=h)
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None

    page2 = await client.get(
        "/api/v1/audit", params={"limit": 2, "cursor": body1["next_cursor"]}, headers=h
    )
    body2 = page2.json()
    assert len(body2["items"]) == 2

    ids1 = {i["id"] for i in body1["items"]}
    ids2 = {i["id"] for i in body2["items"]}
    assert ids1.isdisjoint(ids2)


async def test_audit_is_scoped_per_org(client):
    token_a, org_a = await _org(client, "aud7a@example.com", "Org AUD7A")
    token_b, org_b = await _org(client, "aud7b@example.com", "Org AUD7B")
    h_a = auth_headers(token_a, org_a["id"])
    h_b = auth_headers(token_b, org_b["id"])

    await client.post(
        "/api/v1/api-keys", json={"name": "k", "scopes": ["contacts:read"]}, headers=h_a
    )

    b_listed = await client.get("/api/v1/audit", headers=h_b)
    assert b_listed.json()["items"] == []
