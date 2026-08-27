"""Invite-only registration: the door that only opens for the first user or a valid invite.

``allow_open_registration`` is TEST/DEV escape hatch the rest of the suite relies on to
register many users cheaply — it is deliberately turned OFF here (via the local
``settings`` fixture override below) so these tests exercise the REAL gate: the bootstrap
count-of-users check and the invite system, not the shortcut. See app/config.py and
app/api/routes/auth.py::register.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Invite
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings():
    """Overrides the module-wide `settings` fixture from conftest.py.

    `engine` and `client` (defined in conftest) depend on `settings` BY NAME, so this
    override is picked up by every fixture in this file automatically. Invite-only
    registration must be exercised for real here — the rest of the suite relies on
    ``allow_open_registration=True`` (conftest's default) as a shortcut to register many
    users; that shortcut would make the "no token is refused" and bootstrap tests below
    pass for the wrong reason.
    """
    return make_settings(allow_open_registration=False)


async def _owner_org(client) -> tuple[str, dict, dict]:
    """First registration on a fresh instance: bootstrap, then owns a fresh org."""
    owner_token = await register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@example.com")
    org = await create_org(client, owner_token, "Acme")
    return owner_token, org, auth_headers(owner_token, org["id"])


async def _invite(client, headers, email, role_name="agent"):
    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": email, "role_name": role_name},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


# ----------------------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------------------
async def test_bootstrap_first_registration_needs_no_token_and_becomes_owner(client):
    """The very first account on an empty instance registers with no invite_token, then
    creates an org and lands there as owner."""
    owner_token, org, h = await _owner_org(client)
    members = (await client.get("/api/v1/orgs/current/members", headers=h)).json()
    assert len(members) == 1
    assert members[0]["role_name"] == "owner"


async def test_registration_without_token_after_bootstrap_is_refused(client):
    """Once the instance has an owner, the bootstrap exception is spent forever."""
    await _owner_org(client)
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "nobody-invited@example.com", "password": PASSWORD},
    )
    assert r.status_code == 422
    assert "administrator" in r.json()["error"]["message"].lower()


# ----------------------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------------------
async def test_valid_invite_lands_new_user_in_org_with_invited_role(client):
    _, org, h = await _owner_org(client)
    invite = await _invite(client, h, "newagent@example.com", "agent")
    assert invite["token"]
    assert invite["token"] in invite["accept_url"]

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "newagent@example.com",
            "password": PASSWORD,
            "invite_token": invite["token"],
        },
    )
    assert reg.status_code == 201, reg.text

    members = (await client.get("/api/v1/orgs/current/members", headers=h)).json()
    new_member = next(m for m in members if m["email"] == "newagent@example.com")
    assert new_member["role_name"] == "agent"


# ----------------------------------------------------------------------------------
# Single use
# ----------------------------------------------------------------------------------
async def test_invite_cannot_be_redeemed_twice(client):
    _, org, h = await _owner_org(client)
    invite = await _invite(client, h, "onceonly@example.com")

    first = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "onceonly@example.com",
            "password": PASSWORD,
            "invite_token": invite["token"],
        },
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "onceonly@example.com",
            "password": "another-long-password",
            "invite_token": invite["token"],
        },
    )
    assert second.status_code == 422
    assert "already been used" in second.json()["error"]["message"].lower()


# ----------------------------------------------------------------------------------
# Expiry
# ----------------------------------------------------------------------------------
async def test_expired_invite_is_refused(client, session):
    _, org, h = await _owner_org(client)
    invite = await _invite(client, h, "expired@example.com")

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(Invite).where(Invite.id == uuid.UUID(invite["id"])))
    ).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "expired@example.com",
            "password": PASSWORD,
            "invite_token": invite["token"],
        },
    )
    assert reg.status_code == 422
    assert "expired" in reg.json()["error"]["message"].lower()


# ----------------------------------------------------------------------------------
# Revoked
# ----------------------------------------------------------------------------------
async def test_revoked_invite_is_refused(client):
    _, org, h = await _owner_org(client)
    invite = await _invite(client, h, "revokeme@example.com")

    revoke = await client.delete(f"/api/v1/orgs/current/invites/{invite['id']}", headers=h)
    assert revoke.status_code == 200
    assert revoke.json()["revoked_at"] is not None

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "revokeme@example.com",
            "password": PASSWORD,
            "invite_token": invite["token"],
        },
    )
    assert reg.status_code == 422
    assert "revoked" in reg.json()["error"]["message"].lower()


# ----------------------------------------------------------------------------------
# Email binding — the leak this exists to prevent
# ----------------------------------------------------------------------------------
async def test_invite_bound_to_email_rejects_a_different_redeemer_without_leaking_it(client):
    _, org, h = await _owner_org(client)
    invite = await _invite(client, h, "a@x.com")

    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": "b@x.com", "password": PASSWORD, "invite_token": invite["token"]},
    )
    assert reg.status_code == 422
    body_text = reg.text
    assert "a@x.com" not in body_text
    assert "different email address" in reg.json()["error"]["message"].lower()


# ----------------------------------------------------------------------------------
# Wrong token entirely
# ----------------------------------------------------------------------------------
async def test_wrong_token_entirely_is_refused(client):
    await _owner_org(client)
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "someone@example.com",
            "password": PASSWORD,
            "invite_token": "this-token-was-never-issued",
        },
    )
    assert reg.status_code == 422


# ----------------------------------------------------------------------------------
# Role safety — an invite may never mint an owner, nor a role that does not exist
# ----------------------------------------------------------------------------------
async def test_invite_cannot_mint_owner_role(client):
    _, org, h = await _owner_org(client)
    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "wannabe-owner@example.com", "role_name": "owner"},
        headers=h,
    )
    assert r.status_code == 422


async def test_invite_rejects_nonsense_role(client):
    _, org, h = await _owner_org(client)
    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "confused@example.com", "role_name": "definitely-not-a-role"},
        headers=h,
    )
    assert r.status_code == 422


# ----------------------------------------------------------------------------------
# Duplicate membership
# ----------------------------------------------------------------------------------
async def test_inviting_an_existing_member_is_409(client):
    owner_token, org, h = await _owner_org(client)
    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "newagent@example.com", "role_name": "agent"},
        headers=h,
    )
    assert r.status_code == 201
    invite = r.json()
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "newagent@example.com",
            "password": PASSWORD,
            "invite_token": invite["token"],
        },
    )
    assert reg.status_code == 201

    dup = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "newagent@example.com", "role_name": "agent"},
        headers=h,
    )
    assert dup.status_code == 409


# ----------------------------------------------------------------------------------
# Tenancy — org B cannot see or revoke org A's invites
# ----------------------------------------------------------------------------------
async def test_org_b_cannot_see_or_revoke_org_a_invites(client):
    """Same owner, two separate orgs — tenancy is scoped by X-Org-Id, not by "who you
    are", so a single user acting as owner of two orgs is a valid, minimal way to prove
    isolation without needing a second real registrant under invite-only gating."""
    owner_token, org_a, h_a = await _owner_org(client)
    invite_a = await _invite(client, h_a, "target@example.com")

    org_b = await create_org(client, owner_token, "Org B")
    h_b = auth_headers(owner_token, org_b["id"])

    listing = await client.get("/api/v1/orgs/current/invites", headers=h_b)
    assert listing.status_code == 200
    assert listing.json() == []

    revoke = await client.delete(f"/api/v1/orgs/current/invites/{invite_a['id']}", headers=h_b)
    assert revoke.status_code == 404


# ----------------------------------------------------------------------------------
# RBAC — an agent may never issue invites
# ----------------------------------------------------------------------------------
async def test_agent_role_cannot_create_invite(client):
    owner_token, org, h_owner = await _owner_org(client)
    invite = await _invite(client, h_owner, "fieldagent@example.com", "agent")
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "fieldagent@example.com",
            "password": PASSWORD,
            "invite_token": invite["token"],
        },
    )
    assert reg.status_code == 201

    agent_login = await client.post(
        "/api/v1/auth/login", json={"email": "fieldagent@example.com", "password": PASSWORD}
    )
    assert agent_login.status_code == 200
    h_agent = auth_headers(agent_login.json()["access_token"], org["id"])

    denied = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "another@example.com", "role_name": "agent"},
        headers=h_agent,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


# ----------------------------------------------------------------------------------
# The list endpoint never returns a token
# ----------------------------------------------------------------------------------
async def test_list_invites_never_includes_a_token(client):
    _, org, h = await _owner_org(client)
    await _invite(client, h, "listed1@example.com")
    await _invite(client, h, "listed2@example.com")

    listing = await client.get("/api/v1/orgs/current/invites", headers=h)
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 2
    for row in rows:
        assert "token" not in row
