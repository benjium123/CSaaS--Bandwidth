"""P15: unit tests for the InboxAccess resolver (app/services/inbox_access.py).

Numbers/inboxes are seeded through the real HTTP API (add_number auto-creates the Inbox
row - P15 also proves that behaviour), then grants are wired directly through the ORM so
each test isolates exactly the resolution path it names.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.api.routes.softphone import _event_visible
from app.db.base import set_org_context
from app.models import Department, DepartmentMember, Inbox, InboxGrant, OrgNumber
from app.repositories import users as users_repo
from app.services.inbox_access import InboxAccess, resolve_access
from tests.conftest import auth_headers, create_org, register_and_login

E164_A = "+12145550100"
E164_B = "+12145550111"


async def _add_number(client, token, org_id, e164: str) -> None:
    r = await client.post(
        "/api/v1/numbers", json={"e164": e164}, headers=auth_headers(token, str(org_id))
    )
    assert r.status_code == 201, r.text


async def _inbox_for(session, e164: str) -> Inbox:
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == e164))
    ).scalar_one()
    return (
        await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))
    ).scalar_one()


async def test_admin_permission_is_admin_and_can_use_anything(client, session):
    token = await register_and_login(client, "ia1@example.com")
    org = await create_org(client, token, "Org IA1")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "ia1@example.com")

    access = await resolve_access(session, user.id, ["inboxes:admin"])
    assert access.is_admin
    assert access.can_view(E164_A)
    assert access.can_use(E164_A)
    assert access.can_use("+19999999999")  # admin bypasses grants entirely


async def test_wildcard_permission_is_also_admin(client, session):
    token = await register_and_login(client, "ia2@example.com")
    org = await create_org(client, token, "Org IA2")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "ia2@example.com")

    access = await resolve_access(session, user.id, ["*"])
    assert access.is_admin
    assert access.can_use(E164_A)


async def test_api_key_caller_bypasses_the_tier(client, session):
    """actor_user_id is None for an API-key-authenticated request (P13 DR-3) - such a
    caller is scoped by its own explicit permission list, not per-user grants."""
    token = await register_and_login(client, "ia3@example.com")
    org = await create_org(client, token, "Org IA3")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    set_org_context(session, org_id)
    access = await resolve_access(session, None, ["inbox:send"])
    assert access.is_admin
    assert access.can_use(E164_A)


async def test_direct_user_grant_member(client, session):
    token = await register_and_login(client, "ia4@example.com")
    org = await create_org(client, token, "Org IA4")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)
    await _add_number(client, token, org_id, E164_B)

    await register_and_login(client, "member4@example.com")
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "member4@example.com")
    inbox = await _inbox_for(session, E164_A)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="user",
            grantee_id=user.id,
            role="member",
        )
    )
    await session.commit()

    access = await resolve_access(session, user.id, [])
    assert not access.is_admin
    assert access.can_view(E164_A)
    assert access.can_use(E164_A)
    assert not access.can_view(E164_B)
    assert not access.can_use(E164_B)


async def test_direct_user_grant_viewer_is_read_only(client, session):
    token = await register_and_login(client, "ia5@example.com")
    org = await create_org(client, token, "Org IA5")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    await register_and_login(client, "viewer5@example.com")
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "viewer5@example.com")
    inbox = await _inbox_for(session, E164_A)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="user",
            grantee_id=user.id,
            role="viewer",
        )
    )
    await session.commit()

    access = await resolve_access(session, user.id, [])
    assert access.can_view(E164_A)
    assert not access.can_use(E164_A)


async def test_department_grant_member_scopes_to_members_only(client, session):
    token = await register_and_login(client, "ia6@example.com")
    org = await create_org(client, token, "Org IA6")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    await register_and_login(client, "indept6@example.com")
    await register_and_login(client, "outsider6@example.com")

    set_org_context(session, org_id)
    in_dept_user = await users_repo.get_by_email(session, "indept6@example.com")
    outsider = await users_repo.get_by_email(session, "outsider6@example.com")

    dept = Department(id=uuid.uuid4(), org_id=org_id, name="Sales")
    session.add(dept)
    await session.flush()
    session.add(
        DepartmentMember(
            id=uuid.uuid4(), org_id=org_id, department_id=dept.id, user_id=in_dept_user.id
        )
    )
    inbox = await _inbox_for(session, E164_A)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="department",
            grantee_id=dept.id,
            role="member",
        )
    )
    await session.commit()

    access_member = await resolve_access(session, in_dept_user.id, [])
    assert access_member.can_use(E164_A)

    access_outsider = await resolve_access(session, outsider.id, [])
    assert not access_outsider.can_view(E164_A)


async def test_member_beats_viewer_when_both_paths_exist(client, session):
    token = await register_and_login(client, "ia7@example.com")
    org = await create_org(client, token, "Org IA7")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    await register_and_login(client, "dual7@example.com")
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "dual7@example.com")
    inbox = await _inbox_for(session, E164_A)

    dept = Department(id=uuid.uuid4(), org_id=org_id, name="Ops")
    session.add(dept)
    await session.flush()
    session.add(
        DepartmentMember(id=uuid.uuid4(), org_id=org_id, department_id=dept.id, user_id=user.id)
    )
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="department",
            grantee_id=dept.id,
            role="member",
        )
    )
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="user",
            grantee_id=user.id,
            role="viewer",
        )
    )
    await session.commit()

    access = await resolve_access(session, user.id, [])
    assert access.can_use(E164_A)
    assert E164_A in access.member_e164s
    assert E164_A not in access.viewer_e164s


async def test_no_grants_is_fail_closed(client, session):
    token = await register_and_login(client, "ia8@example.com")
    org = await create_org(client, token, "Org IA8")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "ia8@example.com")

    # Deliberately passing the OWNER's own permissions in as an empty list - this isolates
    # the "no grant" branch of the resolver from the owner's real (wildcard) role.
    access = await resolve_access(session, user.id, [])
    assert not access.is_admin
    assert access.member_e164s == frozenset()
    assert access.viewer_e164s == frozenset()
    assert not access.can_view(E164_A)


async def test_org_isolation_grants_in_org_b_invisible_under_org_a(client, session):
    token = await register_and_login(client, "ia9@example.com")
    org_a = await create_org(client, token, "Org A9")
    org_b = await create_org(client, token, "Org B9")
    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    await _add_number(client, token, org_b_id, E164_A)

    await register_and_login(client, "watcher9@example.com")
    set_org_context(session, org_b_id)
    user = await users_repo.get_by_email(session, "watcher9@example.com")
    inbox = await _inbox_for(session, E164_A)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_b_id,
            inbox_id=inbox.id,
            grantee_type="user",
            grantee_id=user.id,
            role="member",
        )
    )
    await session.commit()

    # The grant lives in org B - resolving under org A's context must see nothing.
    set_org_context(session, org_a_id)
    access_under_a = await resolve_access(session, user.id, [])
    assert access_under_a.member_e164s == frozenset()
    assert not access_under_a.can_use(E164_A)

    set_org_context(session, org_b_id)
    access_under_b = await resolve_access(session, user.id, [])
    assert access_under_b.can_use(E164_A)


async def test_deactivating_department_revokes_member_access(client, session):
    token = await register_and_login(client, "ia10@example.com")
    org = await create_org(client, token, "Org IA10")
    org_id = uuid.UUID(org["id"])
    await _add_number(client, token, org_id, E164_A)

    await register_and_login(client, "deptmember10@example.com")
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, "deptmember10@example.com")
    inbox = await _inbox_for(session, E164_A)

    dept = Department(id=uuid.uuid4(), org_id=org_id, name="Sales")
    session.add(dept)
    await session.flush()
    session.add(
        DepartmentMember(id=uuid.uuid4(), org_id=org_id, department_id=dept.id, user_id=user.id)
    )
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="department",
            grantee_id=dept.id,
            role="member",
        )
    )
    await session.commit()

    access_before = await resolve_access(session, user.id, [])
    assert access_before.can_use(E164_A)

    dept.is_active = False
    await session.commit()

    access_after = await resolve_access(session, user.id, [])
    assert not access_after.can_view(E164_A)


# ----------------------------------------------------------------------------------
# _event_visible (softphone.py) - pure over both call.ring payload shapes
# ----------------------------------------------------------------------------------
async def test_event_visible_call_ring_with_to_gates_by_member_only():
    org_id = uuid.uuid4()
    event = {"type": "call.ring", "call_id": "c1", "to": E164_A}

    member_access = InboxAccess(
        is_admin=False, member_e164s=frozenset({E164_A}), viewer_e164s=frozenset()
    )
    viewer_access = InboxAccess(
        is_admin=False, member_e164s=frozenset(), viewer_e164s=frozenset({E164_A})
    )
    admin_access = InboxAccess(is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset())

    assert await _event_visible(event, member_access, org_id) is True
    # A viewer can_view the number but cannot answer a call from it - a ring is a
    # MEMBER-only event, not merely a view-only one.
    assert await _event_visible(event, viewer_access, org_id) is False
    assert await _event_visible(event, admin_access, org_id) is True


async def test_event_visible_call_ring_without_to_is_fail_closed_for_non_admins():
    org_id = uuid.uuid4()
    event = {"type": "call.ring", "call_id": "c1"}  # no "to" at all

    member_access = InboxAccess(
        is_admin=False, member_e164s=frozenset({E164_A}), viewer_e164s=frozenset()
    )
    admin_access = InboxAccess(is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset())

    # A ring with no resolvable "to" is dropped for every non-admin - shown by default is
    # exactly the wrong failure mode for "who can answer this call".
    assert await _event_visible(event, member_access, org_id) is False
    assert await _event_visible(event, admin_access, org_id) is True
