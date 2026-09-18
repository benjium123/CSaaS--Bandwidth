"""The system 'agent' role may dial out - but only from a line it actually holds.

Operator decision, 18 Sept 2026: an employee handed a line is handed the whole line, so
`agent` gained `calls:place` (models/rbac.py, backfilled onto existing orgs by migration
0052). The risk in that change is not the permission, it is whether the permission
quietly widened REACH as well as capability. These tests exist to prove it did not.

Deliberately uses the SYSTEM agent role rather than a bespoke one. test_p15_departments_
api.py's `test_call_list_filter_and_place_guard` builds a custom role to isolate the P15
gate from RBAC; this file asserts the opposite thing - that the shipped role a real
employee is actually invited into behaves correctly end to end.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import OrgMembership, Role
from app.models.rbac import SYSTEM_ROLES
from app.repositories import users as users_repo
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier


@pytest.fixture()
async def app_with_voice_carrier(engine):
    """Local copy, for the same reason test_p15_departments_api.py keeps one: importing
    it would let ruff mistake the parameter for a shadowed module-level import."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application

A = "+12145550100"
B = "+12145550111"
FAR = "+19725550199"


def test_agent_role_template_can_place_calls() -> None:
    """The template itself. Cheap, and it fails loudly if someone trims the list."""
    assert "calls:place" in SYSTEM_ROLES["agent"]
    assert "calls:read" in SYSTEM_ROLES["agent"]
    # Capability, not reach: the agent must NOT become an inbox admin by this change.
    assert "inboxes:admin" not in SYSTEM_ROLES["agent"]
    # And the deliberate RBAC deny path this repo tests elsewhere stays intact.
    assert "members:read" not in SYSTEM_ROLES["agent"]


async def _agent_in_org(client, session, org_id: uuid.UUID, email: str) -> tuple[str, uuid.UUID]:
    """Register a user and put them in the org under the org's own seeded 'agent' role."""
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)

    set_org_context(session, org_id)
    role_id = (
        await session.execute(
            sa.select(Role.id).where(Role.org_id == org_id, Role.name == "agent")
        )
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role_id)
    )
    await session.commit()
    return token, user.id


async def _inbox_id_for(client, headers, e164: str) -> str:
    listed = await client.get("/api/v1/inboxes", headers=headers)
    assert listed.status_code == 200, listed.text
    return next(i["id"] for i in listed.json() if i["e164"] == e164)


@pytest.mark.anyio
async def test_agent_dials_only_from_a_line_it_holds(app_with_voice_carrier, session):
    client, fake, _app = app_with_voice_carrier
    owner_token = await register_and_login(client, "ap-owner@example.com")
    org = await create_org(client, owner_token, "Org AgentPlace")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, str(org_id))
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    await client.post("/api/v1/numbers", json={"e164": B}, headers=h_owner)

    agent_token, agent_user_id = await _agent_in_org(client, session, org_id, "ap-agent@example.com")
    h_agent = auth_headers(agent_token, str(org_id))

    # 1. Holding calls:place but NO grant is still nothing: the permission alone must not
    #    let anyone dial from a line they were never given.
    before = await client.post("/api/v1/calls", json={"to": FAR, "from": A}, headers=h_agent)
    assert before.status_code == 403, before.text
    assert fake.create_calls == []

    # 2. Given line A as a member, the agent can ring out from A.
    inbox_a = await _inbox_id_for(client, h_owner, A)
    granted = await client.put(
        f"/api/v1/inboxes/{inbox_a}/grants",
        json={"grants": [{"grantee_type": "user", "grantee_id": str(agent_user_id), "role": "member"}]},
        headers=h_owner,
    )
    assert granted.status_code == 200, granted.text

    placed = await client.post("/api/v1/calls", json={"to": FAR, "from": A}, headers=h_agent)
    assert placed.status_code == 201, placed.text

    # 3. THE POINT OF THE CHANGE: line B was never granted, so it is still refused. The
    #    permission widened what an agent may do, not which numbers they may do it from.
    other = await client.post("/api/v1/calls", json={"to": FAR, "from": B}, headers=h_agent)
    assert other.status_code == 403, other.text


@pytest.mark.anyio
async def test_viewer_grant_stays_read_only_for_calls(app_with_voice_carrier, session):
    """A viewer may watch a line, never speak from it - unchanged by calls:place."""
    client, fake, _app = app_with_voice_carrier
    owner_token = await register_and_login(client, "ap2-owner@example.com")
    org = await create_org(client, owner_token, "Org AgentPlace2")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, str(org_id))
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)

    agent_token, agent_user_id = await _agent_in_org(client, session, org_id, "ap2-agent@example.com")
    h_agent = auth_headers(agent_token, str(org_id))

    inbox_a = await _inbox_id_for(client, h_owner, A)
    granted = await client.put(
        f"/api/v1/inboxes/{inbox_a}/grants",
        json={"grants": [{"grantee_type": "user", "grantee_id": str(agent_user_id), "role": "viewer"}]},
        headers=h_owner,
    )
    assert granted.status_code == 200, granted.text

    refused = await client.post("/api/v1/calls", json={"to": FAR, "from": A}, headers=h_agent)
    assert refused.status_code == 403, refused.text
    assert fake.create_calls == []

    # ...but they can still SEE the line, or the grant would be meaningless.
    listed = await client.get("/api/v1/inboxes", headers=h_agent)
    assert listed.status_code == 200
    assert [i["my_role"] for i in listed.json() if i["e164"] == A] == ["viewer"]
