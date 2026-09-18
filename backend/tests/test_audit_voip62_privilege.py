"""Adversarial audit (session voip-62): can a member mint privilege they do not hold?

The codebase has a consistent rule for this, `_role_assignable_by` (routes/orgs.py:335):
an actor may only set or act on a role whose permission set is a SUBSET of their own,
with the wildcard exempt. Its docstring is explicit that without it "an admin could
self-promote to owner".

These tests check that rule is applied on EVERY path that hands someone a role, not just
on the one it was written for.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.models import OrgMembership, Role, User
from tests.conftest import auth_headers, create_org, register_and_login


async def _member_with_custom_role(
    session, org_id: uuid.UUID, email: str, permissions: list[str]
) -> None:
    """Give an existing user a custom role in this org, holding exactly `permissions`."""
    from app.db.base import set_org_context

    set_org_context(session, org_id)
    user = (
        await session.execute(
            sa.select(User).where(sa.func.lower(User.email) == email.lower())
        )
    ).scalar_one()
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name="inviter",
        permissions=list(permissions),
        is_system=False,
    )
    session.add(role)
    await session.flush()
    session.add(OrgMembership(org_id=org_id, user_id=user.id, role_id=role.id))
    await session.commit()


# ======================================================================================
# Inviting someone as "admin" from a role that is strictly weaker than admin.
#
# The system "admin" role is EVERY permission except org:delete and org:billing
# (models/rbac.py:70). A custom role holding only members:invite is a tiny subset of it.
#
# routes/orgs.py update_member applies `_role_assignable_by(ctx.role, new_role)` and
# refuses with "You cannot assign a role with more permissions than your own"
# (orgs.py:430-433). create_invite does NOT apply it -- it only checks
# `_privileged_grant_action`, which triggers a selfie STEP-UP. A step-up proves the
# inviter is a verified human; it does not prove they are entitled to grant the role.
# And `check_org_selfie_step_up` is a no-op entirely while KYC_ENFORCED is off, which is
# the configuration these tests run in, so the authorization gap is visible on its own.
#
# Attack: a member whose job is "invite people" cannot promote themselves (update_member
# forbids it, and you cannot change your own role at all). So instead they invite a second
# address they control AS ADMIN, accept it, and now operate with near-total workspace
# power. Escalation by proxy.
# ======================================================================================
async def test_cannot_invite_a_role_more_powerful_than_your_own(client, session):
    owner_token = await register_and_login(client, "owner@example.com")
    org = await create_org(client, owner_token, "Invite Co")
    org_id = org["id"]

    await register_and_login(client, "inviter@example.com")
    await _member_with_custom_role(
        session, uuid.UUID(org_id), "inviter@example.com", ["members:invite", "members:read"]
    )

    # Sign in as the weak member and try to create an ADMIN.
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "inviter@example.com", "password": "correct-horse-battery"},
    )
    assert r.status_code == 200, r.text
    weak_token = r.json()["access_token"]

    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "confederate@example.com", "role_name": "admin"},
        headers=auth_headers(weak_token, org_id),
    )
    assert r.status_code == 403, (
        "a member holding only members:invite created an invitation for the ADMIN role, "
        "which holds every permission except org:delete/org:billing. update_member refuses "
        f"the equivalent grant; create_invite allowed it. Response {r.status_code}: {r.text}"
    )


async def test_control_update_member_does_refuse_the_same_grant(client, session):
    """Control: the identical escalation via update_member IS refused, which is what makes
    the invite path an inconsistency rather than a deliberate policy."""
    owner_token = await register_and_login(client, "owner2@example.com")
    org = await create_org(client, owner_token, "Invite Co 2")
    org_id = org["id"]

    await register_and_login(client, "inviter2@example.com")
    await _member_with_custom_role(
        session, uuid.UUID(org_id), "inviter2@example.com", ["members:invite", "members:read"]
    )
    # The weak member also needs members:update to reach that route at all; grant it so the
    # ONLY thing under test is the containment check rather than the permission gate.
    from app.db.base import set_org_context

    set_org_context(session, uuid.UUID(org_id))
    role = (
        await session.execute(sa.select(Role).where(Role.name == "inviter"))
    ).scalar_one()
    role.permissions = ["members:invite", "members:read", "members:update"]
    await session.commit()

    # A third person to aim the promotion at.
    await register_and_login(client, "target@example.com")
    set_org_context(session, uuid.UUID(org_id))
    target = (
        await session.execute(
            sa.select(User).where(sa.func.lower(User.email) == "target@example.com")
        )
    ).scalar_one()
    agent_role = (
        await session.execute(sa.select(Role).where(Role.name == "agent"))
    ).scalar_one()
    session.add(
        OrgMembership(org_id=uuid.UUID(org_id), user_id=target.id, role_id=agent_role.id)
    )
    await session.commit()

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "inviter2@example.com", "password": "correct-horse-battery"},
    )
    weak_token = r.json()["access_token"]

    r = await client.patch(
        f"/api/v1/orgs/current/members/{target.id}",
        json={"role_name": "admin"},
        headers=auth_headers(weak_token, org_id),
    )
    assert r.status_code == 403, r.text
    # Either containment check may fire first (the TARGET's current role, or the NEW role);
    # both are `_role_assignable_by`, and either way the grant is refused.
    assert "higher role than your own" in r.text or "more permissions than your own" in r.text
