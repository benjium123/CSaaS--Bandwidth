from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import OrgMembership, Role
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login


async def _register_member(client, session, org_id, email, role_name="agent", role_id=None):
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    if role_id is None:
        role_id = (
            await session.execute(sa.select(Role).where(Role.name == role_name))
        ).scalar_one().id
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role_id)
    )
    await session.commit()
    return token, user


async def test_roles_create_clone_from_agent(client, session):
    owner_token = await register_and_login(client, "roles-clone-owner@example.com")
    org = await create_org(client, owner_token, "Roles Clone")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    agent_role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()

    r = await client.post(
        "/api/v1/roles",
        json={"name": "Cloned Agent", "clone_from": str(agent_role.id)},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["permissions"] == list(agent_role.permissions or [])
    assert body["is_system"] is False
    assert body["member_count"] == 0


async def test_roles_create_explicit_permissions_beat_clone_from(client, session):
    """clone_from picks a starting point, but an explicitly-sent permissions[] (even
    empty) must win over the clone's permissions rather than being silently discarded -
    this is what lets the UI edit a starting point before saving it."""
    owner_token = await register_and_login(client, "roles-clone-override-owner@example.com")
    org = await create_org(client, owner_token, "Roles Clone Override")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    agent_role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    explicit_permissions = ["contacts:read"]
    assert explicit_permissions != list(agent_role.permissions or [])

    r = await client.post(
        "/api/v1/roles",
        json={
            "name": "Edited Starting Point",
            "clone_from": str(agent_role.id),
            "permissions": explicit_permissions,
        },
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["permissions"] == explicit_permissions


async def test_roles_edit_system_role_409(client, session):
    owner_token = await register_and_login(client, "roles-sys-owner@example.com")
    org = await create_org(client, owner_token, "Roles Sys")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    role = (await session.execute(sa.select(Role).where(Role.name == "admin"))).scalar_one()

    r = await client.patch(
        f"/api/v1/roles/{role.id}",
        json={"name": "Changed"},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "conflict"


async def test_roles_delete_in_use_409(client, session):
    owner_token = await register_and_login(client, "roles-inuse-owner@example.com")
    org = await create_org(client, owner_token, "Roles InUse")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    created = await client.post(
        "/api/v1/roles",
        json={"name": "Doomed", "permissions": ["contacts:read"]},
        headers=h_owner,
    )
    assert created.status_code == 201, created.text
    role_id = uuid.UUID(created.json()["id"])

    _token, user = await _register_member(
        client, session, org_id, "user.inuse@example.com", role_id=role_id
    )

    r = await client.delete(f"/api/v1/roles/{role_id}", headers=h_owner)
    assert r.status_code == 409, r.text
    assert "still assigned to 1" in r.json()["error"]["message"]


async def test_roles_escalation_blocked(client, session):
    owner_token = await register_and_login(client, "roles-esc-owner@example.com")
    org = await create_org(client, owner_token, "Roles Esc")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    admin_token, _admin = await _register_member(client, session, org_id, "admin.esc@example.com", "admin")
    h_admin = auth_headers(admin_token, org["id"])

    billing = await client.post(
        "/api/v1/roles",
        json={"name": "Bad Billing", "permissions": ["org:billing"]},
        headers=h_admin,
    )
    assert billing.status_code == 422, billing.text

    writer_role = await client.post(
        "/api/v1/roles",
        json={"name": "Role Writer", "permissions": ["roles:read", "roles:write"]},
        headers=h_owner,
    )
    assert writer_role.status_code == 201, writer_role.text
    writer_role_id = uuid.UUID(writer_role.json()["id"])

    writer_token, writer_user = await _register_member(
        client, session, org_id, "writer.esc@example.com", role_id=writer_role_id
    )

    supervise = await client.post(
        "/api/v1/roles",
        json={"name": "Bad Supervise", "permissions": ["calls:supervise"]},
        headers=auth_headers(writer_token, org["id"]),
    )
    assert supervise.status_code == 403, supervise.text


async def test_roles_wildcard_rejected(client, session):
    owner_token = await register_and_login(client, "roles-wild-owner@example.com")
    org = await create_org(client, owner_token, "Roles Wild")
    r = await client.post(
        "/api/v1/roles",
        json={"name": "Wild", "permissions": ["*"]},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 422, r.text


async def test_roles_unknown_key_422(client, session):
    owner_token = await register_and_login(client, "roles-unknown-owner@example.com")
    org = await create_org(client, owner_token, "Roles Unknown")
    r = await client.post(
        "/api/v1/roles",
        json={"name": "Unknown", "permissions": ["bogus:perm"]},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 422, r.text
    assert "Unknown permission keys" in r.json()["error"]["message"]


async def test_members_cannot_change_own_role(client, session):
    owner_token = await register_and_login(client, "roles-self-owner@example.com")
    org = await create_org(client, owner_token, "Roles Self")
    org_id = uuid.UUID(org["id"])

    admin_token, admin = await _register_member(client, session, org_id, "admin.self@example.com", "admin")

    r = await client.patch(
        f"/api/v1/orgs/current/members/{admin.id}",
        json={"role_name": "agent"},
        headers=auth_headers(admin_token, org["id"]),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "permission_denied"


async def test_roles_list_includes_member_count(client, session):
    owner_token = await register_and_login(client, "roles-count-owner@example.com")
    org = await create_org(client, owner_token, "Roles Count")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    created = await client.post(
        "/api/v1/roles",
        json={"name": "Counted", "permissions": ["contacts:read"]},
        headers=h_owner,
    )
    role_id = uuid.UUID(created.json()["id"])

    for i in range(2):
        _token, user = await _register_member(
            client, session, org_id, f"member.count{i}@example.com", role_id=role_id
        )

    listed = await client.get("/api/v1/roles", headers=h_owner)
    assert listed.status_code == 200, listed.text
    row = next(role for role in listed.json() if role["id"] == str(role_id))
    assert row["member_count"] == 2
