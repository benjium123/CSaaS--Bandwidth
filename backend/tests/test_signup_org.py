"""Self-serve signup now mints a workspace; invited signup still does not.

What is really being exercised here is the branch in ``app/api/routes/auth.py::register``
that decides whether a registration carries an org. A registration WITHOUT an invite token
(and with ``allow_open_registration=True``, conftest's default) is a self-serve signup: it
must create exactly one org via ``app.repositories.orgs.create_org_with_owner``, make the
new user its owner, name it from ``company_name`` or the email domain, and return that
membership plus the owner permission set. A registration WITH an invite token takes the
invite branch and must create NO org at all - the invite's org is the only one the new
user joins.

The invite test below issues and redeems its invite with ``allow_open_registration=True``
(conftest's default) rather than overriding the module-wide ``settings`` fixture the way
tests/test_invites.py does: passing ``invite_token`` selects the invite branch regardless
of the flag, so the branch under test is chosen by the token, not by the flag. That keeps
the self-serve tests in this module on the default settings they need.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models import PERMISSIONS, Org
from tests.conftest import auth_headers, register_and_login

pytestmark = pytest.mark.usefixtures("paid_seats")  # adds members; not about seats

PASSWORD = "correct-horse-battery"


async def _register(client, email: str, **extra) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, email: str) -> str:
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _org_count(session) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Org)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


# ----------------------------------------------------------------------------------
# Self-serve signup mints exactly one org, named from company_name
# ----------------------------------------------------------------------------------
async def test_self_serve_signup_owns_exactly_one_org_named_from_company_name(client):
    reg = await _register(
        client, "founder@acme-widgets.com", company_name="Acme Widgets"
    )
    assert len(reg["memberships"]) == 1
    assert reg["memberships"][0]["org_name"] == "Acme Widgets"
    assert reg["memberships"][0]["role_name"] == "owner"

    token = await _login(client, "founder@acme-widgets.com")
    me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200, me.text
    body = me.json()
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["org_name"] == "Acme Widgets"
    assert body["memberships"][0]["role_name"] == "owner"


# ----------------------------------------------------------------------------------
# No company_name -> the email domain names the workspace
# ----------------------------------------------------------------------------------
async def test_org_name_falls_back_to_email_domain(client):
    reg = await _register(client, "founder@acme-widgets.com")
    assert len(reg["memberships"]) == 1
    assert reg["memberships"][0]["org_name"] == "Acme Widgets"

    token = await _login(client, "founder@acme-widgets.com")
    me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200, me.text
    body = me.json()
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["org_name"] == "Acme Widgets"


# ----------------------------------------------------------------------------------
# The new owner really holds the owner permission set on the created org
# ----------------------------------------------------------------------------------
async def test_new_owner_has_owner_permissions_on_the_created_org(client):
    reg = await _register(
        client, "founder@acme-widgets.com", company_name="Acme Widgets"
    )
    # The registration response is what the console uses to land the user, so it must
    # carry the same expansion GET /me would compute.
    assert reg["permissions"] == sorted(PERMISSIONS)
    org_id = reg["memberships"][0]["org_id"]

    token = await _login(client, "founder@acme-widgets.com")
    me = await client.get(
        "/api/v1/auth/me", headers=auth_headers(token, org_id)
    )
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["permissions"] == sorted(PERMISSIONS)
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["org_id"] == org_id
    assert body["memberships"][0]["role_name"] == "owner"


# ----------------------------------------------------------------------------------
# Invited signup joins the invite's org and creates none
# ----------------------------------------------------------------------------------
async def test_invited_signup_joins_the_invite_org_and_creates_none(client, session):
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    owner_token = await register_and_login(client, owner_email)
    owner_me = await client.get("/api/v1/auth/me", headers=auth_headers(owner_token))
    assert owner_me.status_code == 200, owner_me.text
    org_a = owner_me.json()["memberships"][0]
    h_owner = auth_headers(owner_token, org_a["org_id"])

    invitee_email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    inv = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": invitee_email, "role_name": "agent"},
        headers=h_owner,
    )
    assert inv.status_code == 201, inv.text
    invite_token = inv.json()["token"]

    before = await _org_count(session)

    reg = await _register(client, invitee_email, invite_token=invite_token)
    # Unchanged from before this change: the invite branch returns no membership and no
    # permissions, because the invite carries the org.
    assert reg["memberships"] == []
    assert reg["permissions"] == []

    invitee_token = await _login(client, invitee_email)
    me = await client.get("/api/v1/auth/me", headers=auth_headers(invitee_token))
    assert me.status_code == 200, me.text
    body = me.json()
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["org_id"] == org_a["org_id"]
    assert body["memberships"][0]["role_name"] == "agent"

    after = await _org_count(session)
    assert after == before


# ----------------------------------------------------------------------------------
# Two signups with the same company_name must not collide on the unique slug
# ----------------------------------------------------------------------------------
async def test_org_slug_is_unique_when_two_signups_use_the_same_company_name(client):
    first = await _register(
        client, "founder-one@acme-widgets.com", company_name="Acme Widgets"
    )
    second = await _register(
        client, "founder-two@acme-widgets.com", company_name="Acme Widgets"
    )
    assert len(first["memberships"]) == 1
    assert len(second["memberships"]) == 1
    assert first["memberships"][0]["org_slug"] != second["memberships"][0]["org_slug"]
