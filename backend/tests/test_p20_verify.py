"""P20 verification probes (Opus verifier, 2026-09-10).

These are NOT a re-run of test_p20_*.py. Each test here exists because the P20
verification brief named a claim that the shipped tests do not actually prove:

  * tenancy of the /me/capabilities EXISTENCE probes (has_provider / has_number) and
    of the numbers inbox_name join - test_p20_tenancy.py covers member_count, provider
    spend, provider numbers_count and the numbers list, but not these;
  * that seeding a SECOND org in a database that already holds a first org's defaults
    still creates that org's own rows (a broken existence check would silently skip);
  * that re-seeding an org whose defaults were CUSTOMISED leaves the customisation
    alone and duplicates nothing;
  * that every seeded artifact round-trips through the same write-time validators the
    UI routes use, with the same field values;
  * /me/capabilities for owner / admin / agent / API-key callers;
  * that mounting the /api/v1/me router does not shadow /api/v1/auth/me.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    CallFlow,
    ComplianceSettings,
    MessageTemplate,
    OrgMembership,
    RingGroupDef,
    Role,
)
from app.repositories import users as users_repo
from tests.conftest import (
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)

TELNYX_CREDENTIALS = {
    "api_key": "secret",
    "public_key": "secret",
    "messaging_profile_id": "profile",
    "voice_connection_id": "conn",
}


@pytest.fixture
async def client_with_key(engine):
    key = Fernet.generate_key().decode()
    application = create_app(make_settings(credentials_master_key=key))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _add_member(client, session, org_id: uuid.UUID, email: str, role_name: str) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = (
        await session.execute(sa.select(Role).where(Role.name == role_name))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


# ==================================================================================
# 1. Tenancy of the queries test_p20_tenancy.py does not reach
# ==================================================================================
async def test_capabilities_has_provider_is_org_scoped(client_with_key):
    """org A must not read org B's provider account through the existence probe."""
    token_a = await register_and_login(client_with_key, "p20v-hasprov-a@example.com")
    org_a = await create_org(client_with_key, token_a, "Verify HasProv A")
    token_b = await register_and_login(client_with_key, "p20v-hasprov-b@example.com")
    org_b = await create_org(client_with_key, token_b, "Verify HasProv B")

    r = await client_with_key.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "B", "credentials": TELNYX_CREDENTIALS},
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert r.status_code == 201, r.text

    r_a = await client_with_key.get(
        "/api/v1/me/capabilities", headers=auth_headers(token_a, org_a["id"])
    )
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["org"]["has_provider"] is False

    r_b = await client_with_key.get(
        "/api/v1/me/capabilities", headers=auth_headers(token_b, org_b["id"])
    )
    assert r_b.json()["org"]["has_provider"] is True


async def test_capabilities_has_number_and_registration_state_are_org_scoped(client):
    token_b, org_b, _ = await make_org_with_number(
        client, "p20v-hasnum-b@example.com", "Verify HasNum B", "+12025558801"
    )
    token_a = await register_and_login(client, "p20v-hasnum-a@example.com")
    org_a = await create_org(client, token_a, "Verify HasNum A")

    r_a = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token_a, org_a["id"])
    )
    assert r_a.status_code == 200, r_a.text
    summary_a = r_a.json()["org"]
    assert summary_a["has_number"] is False
    assert summary_a["registration_state"] == "none"

    r_b = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(token_b, org_b["id"])
    )
    assert r_b.json()["org"]["has_number"] is True


async def test_numbers_inbox_name_join_is_org_scoped(client):
    """Two orgs, each with an inbox; neither list may borrow the other's inbox name."""
    token_a, org_a, number_a = await make_org_with_number(
        client, "p20v-inbox-a@example.com", "Verify Inbox A", "+12025558811"
    )
    token_b, org_b, number_b = await make_org_with_number(
        client, "p20v-inbox-b@example.com", "Verify Inbox B", "+12025558812"
    )
    for token, org, name in (
        (token_a, org_a, "Alpha inbox"),
        (token_b, org_b, "Bravo inbox"),
    ):
        inboxes = await client.get("/api/v1/inboxes", headers=auth_headers(token, org["id"]))
        assert inboxes.status_code == 200, inboxes.text
        inbox_id = inboxes.json()[0]["id"]
        r = await client.patch(
            f"/api/v1/inboxes/{inbox_id}",
            json={"name": name},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 200, r.text

    r_a = await client.get("/api/v1/numbers", headers=auth_headers(token_a, org_a["id"]))
    assert r_a.status_code == 200, r_a.text
    assert [(n["e164"], n["inbox_name"]) for n in r_a.json()] == [
        (number_a["e164"], "Alpha inbox")
    ]

    r_b = await client.get("/api/v1/numbers", headers=auth_headers(token_b, org_b["id"]))
    assert [(n["e164"], n["inbox_name"]) for n in r_b.json()] == [
        (number_b["e164"], "Bravo inbox")
    ]


# ==================================================================================
# 2. Seeding: a second org, re-seeding, and customised rows
# ==================================================================================
async def test_second_org_gets_its_own_defaults(client, session):
    """A broken (unscoped) existence check would see org A's rows and skip org B."""
    token_a = await register_and_login(client, "p20v-seed2-a@example.com")
    org_a = await create_org(client, token_a, "Verify Seed A")
    token_b = await register_and_login(client, "p20v-seed2-b@example.com")
    org_b = await create_org(client, token_b, "Verify Seed B")

    for token, org in ((token_a, org_a), (token_b, org_b)):
        h = auth_headers(token, org["id"])
        templates = await client.get("/api/v1/templates", headers=h)
        assert templates.status_code == 200, templates.text
        assert sorted(t["name"] for t in templates.json()) == [
            "Help reply",
            "Opt-out confirmation",
        ]
        flows = await client.get("/api/v1/flows", headers=h)
        assert [f["name"] for f in flows.json()] == ["Default"]
        groups = await client.get("/api/v1/ring-groups", headers=h)
        assert [g["name"] for g in groups.json()] == ["Everyone"]
        compliance = await client.get("/api/v1/compliance/settings", headers=h)
        assert compliance.json()["window_start"] == "08:00"

    # And the rows really belong to their own org. GET /compliance/settings creates a
    # row on demand, so the API check above cannot tell "seeded" from "auto-created" -
    # read the table directly instead.
    for org in (org_a, org_b):
        org_id = uuid.UUID(org["id"])
        set_org_context(session, org_id)
        rows = (
            await session.execute(sa.select(MessageTemplate.org_id))
        ).scalars().all()
        assert rows == [org_id, org_id], f"org {org['name']} sees {rows}"
        for model in (ComplianceSettings, RingGroupDef, CallFlow):
            owners = (
                await session.execute(sa.select(model.org_id))
            ).scalars().all()
            assert owners == [org_id], f"{model.__name__} for {org['name']}: {owners}"


async def test_reseeding_preserves_customised_rows_and_duplicates_nothing(client, session):
    token = await register_and_login(client, "p20v-reseed@example.com")
    org = await create_org(client, token, "Verify Reseed")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    # Customise every seeded artifact the way a real operator would.
    r = await client.patch(
        "/api/v1/compliance/settings",
        json={"window_start": "09:30", "window_end": "20:00"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    set_org_context(session, org_id)
    help_row = (
        await session.execute(
            sa.select(MessageTemplate).where(MessageTemplate.name == "Help reply")
        )
    ).scalar_one()
    help_row.body = "Custom help body for {{org.name}}"
    group_row = (
        await session.execute(
            sa.select(RingGroupDef).where(RingGroupDef.name == "Everyone")
        )
    ).scalar_one()
    group_row.strategy = "sequential"
    group_row.ring_timeout_seconds = 45
    flow_row = (
        await session.execute(sa.select(CallFlow).where(CallFlow.name == "Default"))
    ).scalar_one()
    flow_row.definition = {
        "entry": "vm",
        "nodes": {"vm": {"type": "voicemail", "greeting": "Custom greeting."}},
    }
    await session.commit()

    # A custom artifact of the org's own, alongside the seeded ones.
    r = await client.post(
        "/api/v1/templates",
        json={"name": "My own template", "body": "Hi from {{org.name}}"},
        headers=h,
    )
    assert r.status_code == 201, r.text

    # Seed twice more.
    first = await client.post("/api/v1/orgs/current/seed-defaults", headers=h)
    assert first.status_code == 200, first.text
    second = await client.post("/api/v1/orgs/current/seed-defaults", headers=h)
    assert second.status_code == 200, second.text
    for body in (first.json(), second.json()):
        assert body["created"] == [], body
        assert sorted(body["existing"]) == [
            "call_flow:default",
            "compliance_settings",
            "ring_group:everyone",
            # P21: new orgs are seeded a routing policy too (Smart routing on,
            # cross-provider failover available).
            "routing_policy",
            "template:help",
            "template:stop",
        ], body

    # Nothing duplicated.
    set_org_context(session, org_id)
    for model in (ComplianceSettings, MessageTemplate, RingGroupDef, CallFlow):
        count = (
            await session.execute(sa.select(sa.func.count(model.id)))
        ).scalar_one()
        expected = 3 if model is MessageTemplate else 1
        assert count == expected, f"{model.__name__} count {count}, expected {expected}"

    # Existing values win - nothing was reset to the seed value.
    settings_now = await client.get("/api/v1/compliance/settings", headers=h)
    assert settings_now.json()["window_start"] == "09:30"
    assert settings_now.json()["window_end"] == "20:00"
    set_org_context(session, org_id)
    await session.refresh(help_row)
    await session.refresh(group_row)
    await session.refresh(flow_row)
    assert help_row.body == "Custom help body for {{org.name}}"
    assert group_row.strategy == "sequential"
    assert group_row.ring_timeout_seconds == 45
    assert flow_row.definition["entry"] == "vm"


# ==================================================================================
# 3. Every seeded row passes the SAME validator the UI create route uses
# ==================================================================================
async def test_seeded_rows_round_trip_through_the_ui_create_routes(client, session):
    """POST each seeded shape back through the real API and diff the stored values."""
    token = await register_and_login(client, "p20v-validators@example.com")
    org = await create_org(client, token, "Verify Validators")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    seeded_templates = (
        await session.execute(
            sa.select(MessageTemplate).order_by(MessageTemplate.name)
        )
    ).scalars().all()
    seeded_group = (
        await session.execute(
            sa.select(RingGroupDef).where(RingGroupDef.name == "Everyone")
        )
    ).scalar_one()
    seeded_flow = (
        await session.execute(sa.select(CallFlow).where(CallFlow.name == "Default"))
    ).scalar_one()

    for row in seeded_templates:
        r = await client.post(
            "/api/v1/templates",
            json={"name": f"copy of {row.name}", "body": row.body},
            headers=h,
        )
        assert r.status_code == 201, f"{row.name}: {r.text}"
        assert r.json()["body"] == row.body

    r = await client.post(
        "/api/v1/ring-groups",
        json={
            "name": "Copy of Everyone",
            "strategy": seeded_group.strategy,
            "member_user_ids": list(seeded_group.member_user_ids or []),
            "ring_timeout_seconds": seeded_group.ring_timeout_seconds,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["strategy"] == seeded_group.strategy
    assert r.json()["ring_timeout_seconds"] == seeded_group.ring_timeout_seconds

    r = await client.post(
        "/api/v1/flows",
        json={"name": "Copy of Default", "definition": seeded_flow.definition},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["definition"] == seeded_flow.definition

    # And the seeded quiet-hours window survives the UI's own clamp untouched.
    current = await client.get("/api/v1/compliance/settings", headers=h)
    r = await client.patch(
        "/api/v1/compliance/settings",
        json={
            "window_start": current.json()["window_start"],
            "window_end": current.json()["window_end"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["window_start"] == current.json()["window_start"]
    assert r.json()["window_end"] == current.json()["window_end"]


# ==================================================================================
# 4. /me/capabilities per caller kind
# ==================================================================================
async def test_capabilities_admin_role(client, session):
    owner_token = await register_and_login(client, "p20v-adm-owner@example.com")
    org = await create_org(client, owner_token, "Verify Admin")
    admin_token = await _add_member(
        client, session, uuid.UUID(org["id"]), "p20v-adm@example.com", "admin"
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(admin_token, org["id"])
    )
    assert r.status_code == 200, r.text
    permissions = r.json()["permissions"]
    assert "settings:write" in permissions
    assert "members:read" in permissions
    # admin is everything EXCEPT the two owner-only permissions.
    assert "org:delete" not in permissions
    assert "org:billing" not in permissions
    assert r.json()["org"]["member_count"] == 2


async def test_capabilities_agent_sees_org_summary_but_no_admin_permissions(client, session):
    owner_token = await register_and_login(client, "p20v-agent-owner@example.com")
    org = await create_org(client, owner_token, "Verify Agent")
    agent_token = await _add_member(
        client, session, uuid.UUID(org["id"]), "p20v-agent@example.com", "agent"
    )

    r = await client.get(
        "/api/v1/me/capabilities", headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "inbox:read" in body["permissions"]
    assert "settings:read" not in body["permissions"]
    assert "settings:write" not in body["permissions"]
    assert body["org"]["member_count"] == 2


async def test_capabilities_with_an_api_key_caller(client):
    """Record what an API key actually gets. Any of 200/401/403 is defensible; this
    test pins the CURRENT behaviour so a change is visible in review."""
    token = await register_and_login(client, "p20v-apikey@example.com")
    org = await create_org(client, token, "Verify ApiKey")
    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "verifier", "scopes": ["contacts:read", "inbox:read"]},
        headers=auth_headers(token, org["id"]),
    )
    assert created.status_code == 201, created.text
    full_key = created.json()["key"]

    r = await client.get(
        "/api/v1/me/capabilities", headers={"Authorization": f"Bearer {full_key}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # A key is scope-limited and can never hold the wildcard, so it must NOT come back
    # with the full catalogue even though the key was minted by an owner.
    assert set(body["permissions"]) == {"contacts:read", "inbox:read"}
    assert body["org"]["id"] == org["id"]


# ==================================================================================
# 5. Router registration order
# ==================================================================================
async def test_me_router_does_not_shadow_auth_me(client, settings):
    application = create_app(settings)
    # This FastAPI build resolves included routers lazily, so app.routes holds
    # _IncludedRouter placeholders; the OpenAPI document is the honest path table.
    paths = application.openapi()["paths"]
    assert "/api/v1/auth/me" in paths
    assert "/api/v1/me/capabilities" in paths
    # Nothing on /api/v1/me is a wildcard that could swallow another router's path,
    # and no other router owns a path under /api/v1/me.
    me_paths = sorted(p for p in paths if p.startswith("/api/v1/me/"))
    # P26 added the personal bell here and P25 added sessions + login history; the guard
    # is still "these exact paths, no wildcard, nothing from another router" - update the
    # list when /me grows again.
    assert me_paths == [
        "/api/v1/me/capabilities",
        "/api/v1/me/login-events",
        "/api/v1/me/notifications",
        "/api/v1/me/notifications/read",
        "/api/v1/me/sessions",
        "/api/v1/me/sessions/revoke-all",
        "/api/v1/me/sessions/{sid}",
    ], me_paths

    token = await register_and_login(client, "p20v-shadow@example.com")
    org = await create_org(client, token, "Verify Shadow")
    r = await client.get("/api/v1/auth/me", headers=auth_headers(token, org["id"]))
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "p20v-shadow@example.com"
