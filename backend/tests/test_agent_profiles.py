"""P8: agent-profile CRUD (/api/v1/agent/profiles) - org-scoped, name-unique per org,
single-default enforced in the service layer, and gated on settings:read/settings:write
(there is no dedicated agent:* permission yet)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import OrgMembership, Role
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login


async def _make_org(client, email: str, name: str):
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, org


async def test_create_list_patch_delete_roundtrip(client):
    token, org = await _make_org(client, "p1@example.com", "Org P1")
    h = auth_headers(token, org["id"])

    created = await client.post(
        "/api/v1/agent/profiles",
        json={
            "name": "Main",
            "system_prompt": "You are a helpful assistant.",
            "greeting": "Hi, thanks for calling.",
            "voice_id": "voice-1",
            "llm_provider": "anthropic",
            "llm_model": "claude-haiku",
        },
        headers=h,
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["is_default"] is False

    listed = await client.get("/api/v1/agent/profiles", headers=h)
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [profile["id"]]

    patched = await client.patch(
        f"/api/v1/agent/profiles/{profile['id']}",
        json={"greeting": "Updated greeting"},
        headers=h,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["greeting"] == "Updated greeting"
    assert patched.json()["system_prompt"] == "You are a helpful assistant."  # untouched

    deleted = await client.delete(f"/api/v1/agent/profiles/{profile['id']}", headers=h)
    assert deleted.status_code == 204

    listed_after = await client.get("/api/v1/agent/profiles", headers=h)
    assert listed_after.json() == []


async def test_duplicate_name_in_same_org_is_409(client):
    token, org = await _make_org(client, "p2@example.com", "Org P2")
    h = auth_headers(token, org["id"])

    r1 = await client.post("/api/v1/agent/profiles", json={"name": "Main"}, headers=h)
    assert r1.status_code == 201, r1.text

    r2 = await client.post("/api/v1/agent/profiles", json={"name": "Main"}, headers=h)
    assert r2.status_code == 409, r2.text


async def test_same_name_in_different_orgs_is_allowed(client):
    token_a, org_a = await _make_org(client, "p3a@example.com", "Org P3A")
    token_b, org_b = await _make_org(client, "p3b@example.com", "Org P3B")

    r_a = await client.post(
        "/api/v1/agent/profiles", json={"name": "Main"}, headers=auth_headers(token_a, org_a["id"])
    )
    r_b = await client.post(
        "/api/v1/agent/profiles", json={"name": "Main"}, headers=auth_headers(token_b, org_b["id"])
    )
    assert r_a.status_code == 201
    assert r_b.status_code == 201


async def test_set_default_clears_previous_default(client):
    token, org = await _make_org(client, "p4@example.com", "Org P4")
    h = auth_headers(token, org["id"])

    a = (await client.post("/api/v1/agent/profiles", json={"name": "A"}, headers=h)).json()
    b = (await client.post("/api/v1/agent/profiles", json={"name": "B"}, headers=h)).json()

    r1 = await client.post(f"/api/v1/agent/profiles/{a['id']}/default", headers=h)
    assert r1.status_code == 200, r1.text
    assert r1.json()["is_default"] is True

    r2 = await client.post(f"/api/v1/agent/profiles/{b['id']}/default", headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["is_default"] is True

    listed_resp = await client.get("/api/v1/agent/profiles", headers=h)
    listed = {p["id"]: p["is_default"] for p in listed_resp.json()}
    assert listed[a["id"]] is False
    assert listed[b["id"]] is True


async def test_cross_org_profile_is_404_not_visible_or_mutable(client):
    """Org B must not be able to read (via list), update, delete, or set-default a
    profile that belongs to org A - even though it has settings:write on its OWN org
    and knows org A's profile id."""
    token_a, org_a = await _make_org(client, "idor-a@example.com", "Org IDOR A")
    token_b, org_b = await _make_org(client, "idor-b@example.com", "Org IDOR B")
    h_a = auth_headers(token_a, org_a["id"])
    h_b = auth_headers(token_b, org_b["id"])

    owned_by_a = (
        await client.post("/api/v1/agent/profiles", json={"name": "A-Only"}, headers=h_a)
    ).json()
    profile_id = owned_by_a["id"]

    # Not visible in org B's own list.
    listed_b = await client.get("/api/v1/agent/profiles", headers=h_b)
    assert listed_b.status_code == 200
    assert profile_id not in [p["id"] for p in listed_b.json()]

    patch_b = await client.patch(
        f"/api/v1/agent/profiles/{profile_id}", json={"name": "Hijacked"}, headers=h_b
    )
    assert patch_b.status_code == 404

    default_b = await client.post(f"/api/v1/agent/profiles/{profile_id}/default", headers=h_b)
    assert default_b.status_code == 404

    delete_b = await client.delete(f"/api/v1/agent/profiles/{profile_id}", headers=h_b)
    assert delete_b.status_code == 404

    # Org A's profile is untouched by all of the above.
    listed_a = await client.get("/api/v1/agent/profiles", headers=h_a)
    assert [p["name"] for p in listed_a.json()] == ["A-Only"]


async def test_unknown_profile_operations_404(client):
    token, org = await _make_org(client, "p5@example.com", "Org P5")
    h = auth_headers(token, org["id"])
    missing_id = uuid.uuid4()

    assert (
        await client.patch(f"/api/v1/agent/profiles/{missing_id}", json={"name": "X"}, headers=h)
    ).status_code == 404
    deleted = await client.delete(f"/api/v1/agent/profiles/{missing_id}", headers=h)
    assert deleted.status_code == 404
    assert (
        await client.post(f"/api/v1/agent/profiles/{missing_id}/default", headers=h)
    ).status_code == 404


# ==================================================================================
# RBAC: a member without settings:write cannot mutate (same pattern as test_rbac.py's
# agent-role member fixture - the "agent" system role deliberately lacks settings:write).
# ==================================================================================
async def _add_agent_role_member(client, session, owner_token, org, email) -> str:
    org_id = uuid.UUID(org["id"])
    member_token = await register_and_login(client, email)
    member_user = await users_repo.get_by_email(session, email)

    set_org_context(session, org_id)
    agent_role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=member_user.id, role_id=agent_role.id)
    )
    await session.commit()
    return member_token


async def test_member_without_settings_write_cannot_mutate_profiles(client, session):
    owner_token, org = await _make_org(client, "rbac-owner@example.com", "Org RBAC")
    owner_h = auth_headers(owner_token, org["id"])
    member_token = await _add_agent_role_member(
        client, session, owner_token, org, "rbac-member@example.com"
    )
    member_h = auth_headers(member_token, org["id"])

    existing = (
        await client.post("/api/v1/agent/profiles", json={"name": "Owned"}, headers=owner_h)
    ).json()

    denied_create = await client.post(
        "/api/v1/agent/profiles", json={"name": "NotAllowed"}, headers=member_h
    )
    assert denied_create.status_code == 403
    assert denied_create.json()["error"]["code"] == "permission_denied"

    denied_patch = await client.patch(
        f"/api/v1/agent/profiles/{existing['id']}", json={"name": "Hijacked"}, headers=member_h
    )
    assert denied_patch.status_code == 403

    denied_default = await client.post(
        f"/api/v1/agent/profiles/{existing['id']}/default", headers=member_h
    )
    assert denied_default.status_code == 403

    denied_delete = await client.delete(
        f"/api/v1/agent/profiles/{existing['id']}", headers=member_h
    )
    assert denied_delete.status_code == 403

    # The owner (wildcard permission) is unaffected.
    allowed = await client.get("/api/v1/agent/profiles", headers=owner_h)
    assert allowed.status_code == 200
    assert len(allowed.json()) == 1
