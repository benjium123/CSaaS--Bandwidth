"""P13 DR-3: API key create/list/revoke/rotate, and auth against a real route."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import ApiKey
from tests.conftest import auth_headers, create_org, register_and_login


async def _org(client, email: str, name: str):
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, org


async def test_create_returns_full_key_once_and_stores_hash_only(client, session):
    token, org = await _org(client, "ak1@example.com", "Org AK1")
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/api-keys", json={"name": "CI key", "scopes": ["contacts:read"]}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("csk_")
    assert body["prefix"] in body["key"]
    assert "key_hash" not in body

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(ApiKey).where(ApiKey.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert row.key_hash != body["key"]
    assert len(row.key_hash) == 64  # sha256 hex digest
    assert row.prefix == body["prefix"]

    # Listing never leaks the hash or the key.
    listed = await client.get("/api/v1/api-keys", headers=h)
    assert listed.status_code == 200, listed.text
    for item in listed.json():
        assert "key" not in item
        assert "key_hash" not in item


async def test_wildcard_and_unknown_scope_are_rejected(client):
    token, org = await _org(client, "ak2@example.com", "Org AK2")
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/api-keys", json={"name": "x", "scopes": ["*"]}, headers=h)
    assert r.status_code == 422, r.text

    r = await client.post(
        "/api/v1/api-keys", json={"name": "x", "scopes": ["not:a:real:scope"]}, headers=h
    )
    assert r.status_code == 422, r.text


async def test_api_key_auth_works_against_a_real_route_with_the_right_scope(client):
    token, org = await _org(client, "ak3@example.com", "Org AK3")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys", json={"name": "reader", "scopes": ["contacts:read"]}, headers=h
    )
    full_key = created.json()["key"]

    r = await client.get("/api/v1/contacts", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 200, r.text


async def test_valid_key_missing_scope_is_403(client):
    token, org = await _org(client, "ak4@example.com", "Org AK4")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys", json={"name": "reader", "scopes": ["contacts:read"]}, headers=h
    )
    full_key = created.json()["key"]

    r = await client.post(
        "/api/v1/contacts",
        json={"display_name": "x", "phones": []},
        headers={"Authorization": f"Bearer {full_key}"},
    )
    assert r.status_code == 403, r.text


async def test_revoked_key_is_401(client):
    token, org = await _org(client, "ak5@example.com", "Org AK5")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys", json={"name": "reader", "scopes": ["contacts:read"]}, headers=h
    )
    key_id = created.json()["id"]
    full_key = created.json()["key"]

    revoked = await client.post(f"/api/v1/api-keys/{key_id}/revoke", headers=h)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"

    r = await client.get("/api/v1/contacts", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 401, r.text


async def test_expired_key_is_401(client, session):
    token, org = await _org(client, "ak6@example.com", "Org AK6")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys", json={"name": "reader", "scopes": ["contacts:read"]}, headers=h
    )
    key_id = created.json()["id"]
    full_key = created.json()["key"]

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(ApiKey).where(ApiKey.id == uuid.UUID(key_id)))
    ).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    r = await client.get("/api/v1/contacts", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 401, r.text


async def test_rotation_creates_new_and_revokes_old_atomically(client):
    token, org = await _org(client, "ak7@example.com", "Org AK7")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys", json={"name": "reader", "scopes": ["contacts:read"]}, headers=h
    )
    key_id = created.json()["id"]
    old_full_key = created.json()["key"]

    rotated_r = await client.post(f"/api/v1/api-keys/{key_id}/rotate", headers=h)
    assert rotated_r.status_code == 200, rotated_r.text
    rotated = rotated_r.json()
    assert rotated["id"] != key_id
    assert rotated["key"].startswith("csk_")
    new_full_key = rotated["key"]

    old_use = await client.get(
        "/api/v1/contacts", headers={"Authorization": f"Bearer {old_full_key}"}
    )
    assert old_use.status_code == 401, old_use.text

    new_use = await client.get(
        "/api/v1/contacts", headers={"Authorization": f"Bearer {new_full_key}"}
    )
    assert new_use.status_code == 200, new_use.text

    listed = await client.get("/api/v1/api-keys", headers=h)
    statuses = {row["id"]: row["status"] for row in listed.json()}
    assert statuses[key_id] == "revoked"
    assert statuses[rotated["id"]] == "active"


async def test_last_used_at_stamps_on_use(client, session):
    token, org = await _org(client, "ak8@example.com", "Org AK8")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/api-keys", json={"name": "writer", "scopes": ["contacts:write"]}, headers=h
    )
    key_id = created.json()["id"]
    full_key = created.json()["key"]
    assert created.json()["last_used_at"] is None

    r = await client.post(
        "/api/v1/contacts",
        json={"display_name": "Jane", "phones": []},
        headers={"Authorization": f"Bearer {full_key}"},
    )
    assert r.status_code == 201, r.text

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(ApiKey).where(ApiKey.id == uuid.UUID(key_id)))
    ).scalar_one()
    assert row.last_used_at is not None
