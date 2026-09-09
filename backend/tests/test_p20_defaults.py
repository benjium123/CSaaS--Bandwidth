from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    CallFlow,
    ComplianceSettings,
    MessageTemplate,
    OrgMembership,
    OrgNumber,
    RingGroupDef,
    Role,
)
from app.repositories import users as users_repo
from app.services import flow_engine
from app.services import templates as tmpl
from tests.conftest import auth_headers, create_org, register_and_login


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


async def test_org_creation_seeds_defaults(client, session):
    token = await register_and_login(client, "seed-org@example.com")
    org = await create_org(client, token, "Seed Org")
    headers = auth_headers(token, org["id"])

    owner = await users_repo.get_by_email(session, "seed-org@example.com")
    assert owner is not None

    r = await client.get("/api/v1/compliance/settings", headers=headers)
    assert r.status_code == 200, r.text
    settings = r.json()
    assert settings["window_start"] == "08:00"
    assert settings["window_end"] == "21:00"
    assert settings["quiet_hours_enforced"] is True

    r = await client.get("/api/v1/templates", headers=headers)
    assert r.status_code == 200, r.text
    template_names = {t["name"] for t in r.json()}
    assert "Help reply" in template_names
    assert "Opt-out confirmation" in template_names

    r = await client.get("/api/v1/ring-groups", headers=headers)
    assert r.status_code == 200, r.text
    groups = r.json()
    everyone = next(group for group in groups if group["name"] == "Everyone")
    assert everyone["ring_timeout_seconds"] == 20
    assert everyone["member_user_ids"] == [str(owner.id)]

    r = await client.get("/api/v1/flows", headers=headers)
    assert r.status_code == 200, r.text
    flows = r.json()
    default = next(flow for flow in flows if flow["name"] == "Default")
    assert default["version"] == 1
    assert default["status"] == "active"
    assert default["definition"]["entry"] == "ring"
    assert default["definition"]["nodes"]["ring"]["ring_group_id"] == everyone["id"]


async def test_seeded_templates_pass_the_template_validator(client, session):
    token = await register_and_login(client, "seed-tmpl@example.com")
    org = await create_org(client, token, "Seed Tmpl")
    headers = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    templates = (await session.execute(sa.select(MessageTemplate))).scalars().all()
    assert len(templates) == 2

    for template in templates:
        for merge_token in tmpl.extract_tokens(template.body):
            assert merge_token.split(".")[0] in tmpl.ALLOWED_ROOTS

        r = await client.post(
            "/api/v1/templates",
            json={
                "name": f"{template.name} Copy",
                "body": template.body,
                "media_asset_ids": [],
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text


async def test_seed_defaults_is_idempotent(client, session):
    token = await register_and_login(client, "seed-idem@example.com")
    org = await create_org(client, token, "Seed Idem")
    headers = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])

    r = await client.post("/api/v1/orgs/current/seed-defaults", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == []
    assert sorted(body["existing"]) == [
        "call_flow:default",
        "compliance_settings",
        "ring_group:everyone",
        "template:help",
        "template:stop",
    ]

    set_org_context(session, org_id)
    settings_row = (await session.execute(sa.select(ComplianceSettings))).scalar_one()
    await session.delete(settings_row)
    await session.commit()

    r = await client.post("/api/v1/orgs/current/seed-defaults", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == ["compliance_settings"]
    assert sorted(body["existing"]) == [
        "call_flow:default",
        "ring_group:everyone",
        "template:help",
        "template:stop",
    ]

    set_org_context(session, org_id)
    assert (
        await session.execute(sa.select(sa.func.count(ComplianceSettings.id)))
    ).scalar_one() == 1
    assert (
        await session.execute(
            sa.select(sa.func.count(CallFlow.id)).where(CallFlow.name == "Default")
        )
    ).scalar_one() == 1
    assert (
        await session.execute(
            sa.select(sa.func.count(RingGroupDef.id)).where(RingGroupDef.name == "Everyone")
        )
    ).scalar_one() == 1
    assert (
        await session.execute(sa.select(sa.func.count(MessageTemplate.id)))
    ).scalar_one() == 2


async def test_seed_defaults_requires_settings_write(client, session):
    owner_token = await register_and_login(client, "seed-perm-owner@example.com")
    org = await create_org(client, owner_token, "Seed Perm")
    agent_token = await _add_agent(
        client, session, uuid.UUID(org["id"]), "seed-perm-agent@example.com"
    )

    r = await client.post(
        "/api/v1/orgs/current/seed-defaults",
        headers=auth_headers(agent_token, org["id"]),
    )
    assert r.status_code == 403, r.text


async def test_seeded_call_flow_validates(client, session):
    token = await register_and_login(client, "seed-flow@example.com")
    org = await create_org(client, token, "Seed Flow")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    flow = (
        await session.execute(sa.select(CallFlow).where(CallFlow.name == "Default"))
    ).scalar_one()
    assert flow_engine.validate_flow(flow.definition) == []

    numbers = (await session.execute(sa.select(OrgNumber))).scalars().all()
    assert all(number.call_flow_id is None for number in numbers)
