from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.errors import ValidationFailedError
from app.models import OrgMembership, Role
from app.models.rbac import SYSTEM_ROLES, validate_permissions
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login


def test_unknown_permission_key_rejected():
    with pytest.raises(ValidationFailedError):
        validate_permissions(["bogus:nope"])


def test_known_permissions_and_wildcard_accepted():
    assert validate_permissions(["inbox:read", "*"]) == ["inbox:read", "*"]


def test_agent_role_lacks_members_read():
    assert "members:read" not in SYSTEM_ROLES["agent"]
    assert "members:read" in SYSTEM_ROLES["admin"]


async def test_owner_wildcard_passes_every_org_endpoint(client):
    token = await register_and_login(client, "owner@example.com")
    org = await create_org(client, token, "Owner Org")
    h = auth_headers(token, org["id"])
    paths = (
        "/api/v1/orgs/current",
        "/api/v1/orgs/current/roles",
        "/api/v1/orgs/current/members",
    )
    for path in paths:
        r = await client.get(path, headers=h)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"


async def test_agent_denied_members_read(client, session):
    owner_token = await register_and_login(client, "boss@example.com")
    org = await create_org(client, owner_token, "Agency")
    org_id = uuid.UUID(org["id"])

    agent_token = await register_and_login(client, "agent@example.com")
    agent_user = await users_repo.get_by_email(session, "agent@example.com")

    from app.db.base import set_org_context

    set_org_context(session, org_id)
    agent_role = (
        await session.execute(sa.select(Role).where(Role.name == "agent"))
    ).scalar_one()
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=agent_user.id, role_id=agent_role.id
        )
    )
    await session.commit()

    h = auth_headers(agent_token, org["id"])
    denied = await client.get("/api/v1/orgs/current/members", headers=h)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"

    allowed = await client.get(
        "/api/v1/orgs/current/members", headers=auth_headers(owner_token, org["id"])
    )
    assert allowed.status_code == 200
