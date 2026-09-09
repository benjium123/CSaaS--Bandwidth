from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.main import create_app
from app.models import PERMISSIONS, OrgMembership, Role
from app.repositories import users as users_repo
from tests.conftest import (
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)


@pytest.fixture
async def client_with_key(engine):
    key = Fernet.generate_key().decode()
    application = create_app(make_settings(credentials_master_key=key))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _add_agent(client, session, org_id: uuid.UUID, email: str) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def test_capabilities_owner_gets_full_permission_catalogue(client):
    token = await register_and_login(client, "cap-owner@example.com")
    org = await create_org(client, token, "Cap Owner")

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["permissions"]) == len(PERMISSIONS)
    assert "org:delete" in body["permissions"]
    assert "settings:write" in body["permissions"]


async def test_capabilities_agent_is_gated(client, session):
    owner_token = await register_and_login(client, "cap-agent-owner@example.com")
    org = await create_org(client, owner_token, "Cap Agent")
    agent_token = await _add_agent(
        client, session, uuid.UUID(org["id"]), "cap-agent@example.com"
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 200, r.text
    permissions = r.json()["permissions"]
    assert "inbox:read" in permissions
    assert "settings:write" not in permissions
    assert "members:read" not in permissions


async def test_capabilities_blank_org_summary(client):
    token = await register_and_login(client, "cap-blank@example.com")
    org = await create_org(client, token, "Cap Blank")

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    summary = r.json()["org"]
    assert summary["has_provider"] is False
    assert summary["has_number"] is False
    assert summary["member_count"] == 1
    assert summary["registration_state"] == "none"


async def test_capabilities_after_number_added(client):
    token, org, _number = await make_org_with_number(
        client, "cap-number@example.com", "Cap Number", "+12025550150"
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    summary = r.json()["org"]
    assert summary["has_number"] is True
    assert summary["registration_state"] == "unknown"


async def test_capabilities_has_provider_after_connect(client_with_key):
    token = await register_and_login(client_with_key, "cap-provider@example.com")
    org = await create_org(client_with_key, token, "Cap Provider")

    r = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "Primary",
            "credentials": {
                "api_key": "secret",
                "public_key": "secret",
                "messaging_profile_id": "profile",
                "voice_connection_id": "conn",
            },
        },
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text

    r = await client_with_key.get(
        "/api/v1/me/capabilities", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["org"]["has_provider"] is True


async def test_capabilities_requires_org_header(client):
    token = await register_and_login(client, "cap-noorg@example.com")
    r = await client.get(
        "/api/v1/me/capabilities", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 422, r.text


async def test_capabilities_requires_membership_for_org(client, session):
    owner_token = await register_and_login(client, "cap-membership-owner@example.com")
    org_a = await create_org(client, owner_token, "Cap Org A")
    org_b = await create_org(client, owner_token, "Cap Org B")

    outsider_token = await _add_agent(
        client, session, uuid.UUID(org_a["id"]), "cap-outsider@example.com"
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(outsider_token, org_b["id"])
    )
    assert r.status_code == 403, r.text
