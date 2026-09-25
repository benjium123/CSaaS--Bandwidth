"""Pins the "second factor is for privileged accounts only" policy.

A second factor used to be mandatory for every account. It is now mandatory only for a
user who holds a privileged role in ANY org they belong to - as of SYSTEM_ROLES that is
"owner" (wildcard) and "admin" - and optional for everyone else. This file pins the whole
contract:

* a self-serve owner and an invited admin with no factor are still required and are gated
  with an exact HTTP 403 / error code ``two_factor_required`` on a non-exempt endpoint,
  while ``/api/v1/auth/me`` (always exempt, and how the console learns it must send the
  user to enrolment) reports ``second_factor_required``;
* an invited agent with no factor is NOT required and can actually work - the lockout this
  change exists to remove;
* the requirement is DERIVED live from the user's current roles, so promoting an agent to
  admin flips it on for the SAME token with no new login;
* a user with no memberships is not required, the master flag still switches the rule off,
  and the privileged permission split matches SYSTEM_ROLES;
* a live deployment's OLD ``REQUIRE_2FA_ALL_USERS`` env var still binds, while production
  refuses the flag being off with a message describing the NEW rule.

Every status assertion is the exact code and every error assertion is the exact error code:
a bare ``!= 200`` would happily let a 500 or a lockout through.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import httpx
import pyotp
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.config import Settings
from app.db.base import set_org_context
from app.errors import ConfigurationError
from app.models import OrgMembership, Role, User
from app.models.rbac import SYSTEM_ROLES, is_privileged_permissions
from app.services import second_factor
from tests.conftest import (
    approve_workspaces,
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)

pytestmark = pytest.mark.usefixtures("paid_seats")  # adds members; not about seats

#: conftest's default password shape (>= password_min_length of 10).
PASSWORD = "correct-horse-battery"

#: TOTP enrolment encrypts the secret at rest, so the settings need a Fernet key.
FERNET_KEY = Fernet.generate_key().decode()


# ==================================================================================
# Fixtures - the flag is ON for every test in this file.
# ==================================================================================
@pytest.fixture
def sf_settings():
    return make_settings(
        require_2fa_privileged_users=True,
        credential_encryption_key=FERNET_KEY,
    )


@pytest.fixture
async def sf_client(engine, sf_settings):
    from app.main import create_app

    application = create_app(sf_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ==================================================================================
# Helpers
# ==================================================================================
async def _give_factor(session, email: str) -> None:
    """Give the user a second factor directly in the database.

    ``User.has_second_factor`` is ``totp_enabled or has_passkey``, so setting the column and
    committing is sufficient and honest. Driving the whole TOTP enrolment flow here would
    test enrolment, not the gate. Follows tests/conftest.py::mark_recent_2fa's shape.
    """
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    user.totp_enabled = True
    await session.commit()


async def _owner_with_org(sf_client: httpx.AsyncClient, session, email: str, org_name: str):
    """Register + log in an owner, give them a factor, then create an org.

    The order matters: with the flag on, an owner is privileged and gets 403
    ``two_factor_required`` from ``POST /api/v1/orgs`` until they hold a factor.
    Returns ``(token, org_dict, headers)`` where the headers carry the new org's id.
    """
    token = await register_and_login(sf_client, email)
    await _give_factor(session, email)
    org = await create_org(sf_client, token, org_name)
    await approve_workspaces(email)  # privileged roles only count in approved workspaces
    return token, org, auth_headers(token, org["id"])


async def _invited_user(
    sf_client: httpx.AsyncClient, owner_headers: dict, email: str, role_name: str
) -> str:
    """Invite ``email`` into the owner's current org, register them from the invite and log
    in.

    A genuinely non-privileged account can only be created this way: registering WITHOUT
    an invite token creates a new org and makes the registrant its owner (privileged).
    This branch redeems the invite and creates no org, so the user's only membership is the
    invited role. Returns the invited user's access token.
    """
    r = await sf_client.post(
        "/api/v1/orgs/current/invites",
        json={"email": email, "role_name": role_name},
        headers=owner_headers,
    )
    assert r.status_code == 201, r.text
    invite = r.json()

    r = await sf_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "invite_token": invite["token"]},
    )
    assert r.status_code == 201, r.text

    r = await sf_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _membership_org_id(entry: dict) -> str:
    """The org id out of one ``/auth/me`` ``memberships`` entry.

    Tolerates an embedded ``org`` object as well as a flat ``org_id`` key so this file does
    not silently depend on one serialisation; a shape it does not recognise fails loudly
    rather than returning something wrong.
    """
    if "org_id" in entry:
        return str(entry["org_id"])
    org = entry.get("org")
    if isinstance(org, dict) and "id" in org:
        return str(org["id"])
    raise AssertionError(f"no org id in membership entry: {entry!r}")


async def _me(sf_client: httpx.AsyncClient, token: str, org_id=None) -> dict:
    r = await sf_client.get("/api/v1/auth/me", headers=auth_headers(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


async def _set_role(session, org_id, user_email: str, role_name: str) -> None:
    """Repoint the user's membership in one org at a different system role.

    Used for the promotion test - the Role/OrgMembership query shape matches
    tests/test_rbac.py::test_agent_denied_members_read.
    """
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    role = (
        await session.execute(sa.select(Role).where(Role.name == role_name))
    ).scalar_one()
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == user_email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    membership = (
        await session.execute(
            sa.select(OrgMembership).where(
                OrgMembership.user_id == user.id,
                OrgMembership.org_id == org_uuid,
            )
        )
    ).scalar_one()
    membership.role_id = role.id
    await session.commit()


async def _enroll_and_activate(
    client: httpx.AsyncClient, token: str, password: str = PASSWORD
) -> str:
    """Enrol TOTP through the real endpoints and return the base32 secret.

    Copied from tests/test_twofa.py so this file exercises the genuine enrolment path
    rather than stamping ``totp_enabled`` on the row (which ``_give_factor`` does where the
    enrolment flow itself is not what is under test).
    """
    enroll = await client.post(
        "/api/v1/auth/2fa/enroll",
        json={"password": password},
        headers=auth_headers(token),
    )
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    assert enroll.json()["provisioning_uri"].startswith("otpauth://totp/")

    code = pyotp.TOTP(secret).now()
    activate = await client.post(
        "/api/v1/auth/2fa/activate", json={"code": code}, headers=auth_headers(token)
    )
    assert activate.status_code == 200, activate.text
    assert activate.json()["totp_enabled"] is True
    return secret


# ==================================================================================
# HTTP tests - the gate itself
# ==================================================================================
async def test_owner_is_asked_for_a_factor_only_once_approved(sf_client):
    """A self-serve owner is NOT asked for a factor while confirming email and filling in
    verification - there is nothing to protect yet. The moment platform review approves the
    workspace, ``/auth/me`` says a factor is needed and a non-exempt endpoint refuses them.

    If the first half failed, every signup would hit 2FA enrolment before they could even
    apply; if the second half failed, an approved workspace could run on a password alone.
    """
    email = "gated-owner@example.com"
    token = await register_and_login(sf_client, email)

    me = await _me(sf_client, token)
    assert me["second_factor_required"] is False
    org_id = _membership_org_id(me["memberships"][0])
    r = await sf_client.get("/api/v1/contacts", headers=auth_headers(token, org_id))
    assert r.status_code == 200, r.text

    await approve_workspaces(email)
    me = await _me(sf_client, token)
    assert me["second_factor_required"] is True
    assert len(me["memberships"]) == 1, me["memberships"]
    org_id = _membership_org_id(me["memberships"][0])

    r = await sf_client.get("/api/v1/contacts", headers=auth_headers(token, org_id))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "two_factor_required"


async def test_admin_without_a_factor_is_required(sf_client, session):
    """An invited ``admin`` holds a privileged permission set, so they too are required.

    If this failed, an admin - who can rewrite roles and members - could run on a password
    alone, which is exactly the account the original all-users rule existed to protect.
    """
    _owner_token, org, owner_headers = await _owner_with_org(
        sf_client, session, "admin-owner@example.com", "Admin Org"
    )
    admin_token = await _invited_user(
        sf_client, owner_headers, "invited-admin@example.com", "admin"
    )

    me = await _me(sf_client, admin_token)
    assert me["second_factor_required"] is True

    r = await sf_client.get(
        "/api/v1/contacts", headers=auth_headers(admin_token, org["id"])
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "two_factor_required"


async def test_agent_without_a_factor_is_not_required_and_can_work(sf_client, session):
    """THE central test of the change: an ordinary agent with no factor is not required
    and can actually use the product.

    If this failed, every ordinary staff account would be locked out of its inbox until it
    enrolled a factor - the lockout this whole change exists to remove.
    """
    _owner_token, org, owner_headers = await _owner_with_org(
        sf_client, session, "agent-owner@example.com", "Agent Org"
    )
    agent_token = await _invited_user(
        sf_client, owner_headers, "invited-agent@example.com", "agent"
    )

    me = await _me(sf_client, agent_token)
    assert me["second_factor_required"] is False

    r = await sf_client.get(
        "/api/v1/contacts", headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


async def test_agent_with_a_factor_is_unaffected(sf_client, session):
    """Enrolling a factor is optional, not forbidden: an agent who has one is still not
    "required" and still works.

    If this failed, the change would have broken every agent who had already enrolled -
    a regression dressed up as a policy.
    """
    _owner_token, org, owner_headers = await _owner_with_org(
        sf_client, session, "agent2-owner@example.com", "Agent2 Org"
    )
    agent_email = "agent-with-factor@example.com"
    agent_token = await _invited_user(sf_client, owner_headers, agent_email, "agent")
    await _give_factor(session, agent_email)

    me = await _me(sf_client, agent_token)
    assert me["second_factor_required"] is False

    r = await sf_client.get(
        "/api/v1/contacts", headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 200, r.text


async def test_promoting_an_agent_to_admin_flips_the_requirement_on(sf_client, session):
    """THE privilege-escalation test: a factorless agent is promoted to admin and, using
    the SAME token with no new login, is now required and gated.

    This is what stops the requirement being stored at signup: if it were cached on the
    user row, a promoted agent would stay exempt for ever and nothing would look wrong - a
    privilege escalation wearing the costume of a preference. If this failed, every
    promotion would be a silent way to keep using an admin account with a password alone.
    """
    _owner_token, org, owner_headers = await _owner_with_org(
        sf_client, session, "promote-owner@example.com", "Promote Org"
    )
    agent_email = "promote-agent@example.com"
    agent_token = await _invited_user(sf_client, owner_headers, agent_email, "agent")

    me = await _me(sf_client, agent_token)
    assert me["second_factor_required"] is False
    r = await sf_client.get(
        "/api/v1/contacts", headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 200, r.text

    # Promotion, straight in the database - the same token is reused below.
    await _set_role(session, uuid.UUID(org["id"]), agent_email, "admin")

    me = await _me(sf_client, agent_token)
    assert me["second_factor_required"] is True
    r = await sf_client.get(
        "/api/v1/contacts", headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "two_factor_required"


async def test_agent_in_one_org_and_admin_in_another_is_required(sf_client, session):
    """Privileged in ANY org counts: the same user is an agent in one workspace and an
    admin in another, and the admin membership obliges a factor for the whole account.

    If this failed, a user could sidestep the requirement by being invited as an agent into
    their "real" workspace while holding an admin membership elsewhere, and only ever log
    in through the agent one.
    """
    _owner_a_token, org_a, owner_a_headers = await _owner_with_org(
        sf_client, session, "owner-a@example.com", "Org A"
    )
    _owner_b_token, org_b, _owner_b_headers = await _owner_with_org(
        sf_client, session, "owner-b@example.com", "Org B"
    )

    shared_email = "dual-role@example.com"
    token = await _invited_user(sf_client, owner_a_headers, shared_email, "agent")

    # A SECOND membership in org B as admin, inserted directly: the invite endpoint would
    # insist on a fresh registration and this user already exists.
    org_b_uuid = uuid.UUID(org_b["id"])
    set_org_context(session, org_b_uuid)
    admin_role = (
        await session.execute(sa.select(Role).where(Role.name == "admin"))
    ).scalar_one()
    shared_user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == shared_email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=org_b_uuid,
            user_id=shared_user.id,
            role_id=admin_role.id,
        )
    )
    await session.commit()

    me = await _me(sf_client, token)
    assert len(me["memberships"]) == 2, me["memberships"]
    assert me["second_factor_required"] is True


async def test_agent_may_remove_their_only_factor(sf_client, session):
    """A second factor is optional for an agent, and optional has to mean reversible.

    If this failed, enrolling one voluntarily would be a one-way door: an ordinary staff
    member would be trapped behind a factor they were never obliged to hold and could never
    turn it off.
    """
    _owner_token, _org, owner_headers = await _owner_with_org(
        sf_client, session, "remove-owner@example.com", "Remove Org"
    )
    agent_token = await _invited_user(
        sf_client, owner_headers, "agent-remover@example.com", "agent"
    )
    secret = await _enroll_and_activate(sf_client, agent_token)

    # A TOTP code cannot be reused, so the code spent on activation must have expired
    # before the disable below can use a fresh one. Do NOT delete this sleep: without it
    # the disable is frequently rejected with a 401 replay error and the test goes flaky.
    time.sleep(31 - (int(time.time()) % 30))

    r = await sf_client.post(
        "/api/v1/auth/2fa/disable",
        json={"code": pyotp.TOTP(secret).now(), "password": PASSWORD},
        headers=auth_headers(agent_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["totp_enabled"] is False


async def test_admin_may_not_remove_their_only_factor(sf_client, session):
    """An admin can rewrite roles and members, so they must not be able to strip their own
    second factor down to a password alone.

    If this failed, the privileged account protected by this whole change could shed its
    factor at will, which is the requirement simply not existing.
    """
    _owner_token, _org, owner_headers = await _owner_with_org(
        sf_client, session, "lastfactor-owner@example.com", "Lastfactor Org"
    )
    admin_token = await _invited_user(
        sf_client, owner_headers, "admin-remover@example.com", "admin"
    )
    await _enroll_and_activate(sf_client, admin_token)

    # The deliberately-wrong code proves the last-factor guard fires BEFORE the code is
    # checked: an admin gets the guard's error even with a code that could never be valid.
    # If the guard were removed this would be a 401 for the bad code rather than a 200, so
    # the test would still catch the regression - which is why we ALSO pin the exact error
    # code instead of merely asserting the request failed.
    r = await sf_client.post(
        "/api/v1/auth/2fa/disable",
        json={"code": "000000", "password": PASSWORD},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "last_second_factor"


# ==================================================================================
# Unit-level tests - the policy function itself
# ==================================================================================
async def test_user_with_no_memberships_is_not_required():
    """A user belonging to nowhere holds no privileged role, so a factor is optional.

    If this failed, an account with no memberships at all (an invited user whose invite was
    revoked, say) would be blocked everywhere with no way to satisfy the gate.
    """
    assert (
        second_factor.required_from_roles(
            make_settings(require_2fa_privileged_users=True), []
        )
        is False
    )


async def test_flag_off_requires_nobody():
    """The master switch still switches everything off, even for a wildcard role.

    If this failed, a deployment that turned the requirement off would still gate its most
    privileged users - the opposite of the operator's instruction.
    """
    role = SimpleNamespace(permissions=["*"])
    assert (
        second_factor.required_from_roles(
            make_settings(require_2fa_privileged_users=False), [role]
        )
        is False
    )


async def test_privileged_split_matches_system_roles():
    """Pins the privileged/non-privileged split the product owner asked for.

    If someone adds ``members:update`` (or the wildcard) to the agent role, or removes a
    privileged permission from admin, this fails here rather than silently changing who
    must enrol a factor.
    """
    assert is_privileged_permissions(SYSTEM_ROLES["owner"]) is True
    assert is_privileged_permissions(SYSTEM_ROLES["admin"]) is True
    assert is_privileged_permissions(SYSTEM_ROLES["agent"]) is False


async def test_old_env_var_name_still_binds_and_production_message_is_accurate(monkeypatch):
    """The field was renamed, so this pins that a live deployment's existing env var did
    not silently stop working, and that production still refuses the flag being off with a
    message describing the NEW rule.

    If the first half failed, every existing deployment's ``REQUIRE_2FA_ALL_USERS=false``
    would have quietly reverted to the default - re-gating or un-gating users on upgrade.
    If the second half failed, an operator would be told to fix a rule that no longer
    exists ("every account") and would never find the setting that is actually wrong.
    """
    monkeypatch.setenv("REQUIRE_2FA_ALL_USERS", "false")
    old_only = Settings(app_env="test", jwt_secret="x" * 40, session_secret="y" * 40)
    assert old_only.require_2fa_privileged_users is False

    # The new name is the canonical one and wins when both are present.
    monkeypatch.setenv("REQUIRE_2FA_PRIVILEGED_USERS", "true")
    both = Settings(app_env="test", jwt_secret="x" * 40, session_secret="y" * 40)
    assert both.require_2fa_privileged_users is True

    with pytest.raises(ConfigurationError) as exc:
        make_settings(app_env="production", require_2fa_privileged_users=False)
    message = str(exc.value)
    assert "REQUIRE_2FA_PRIVILEGED_USERS" in message
    assert "every account" not in message
