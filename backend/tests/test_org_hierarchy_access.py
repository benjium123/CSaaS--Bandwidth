"""Org hierarchy (0092): managers inherit their reports' DIRECT lines, transitively.

Security contract (services/inbox_access.py, services/org_tree.py, PUT
/orgs/current/members/{id}/manager): a manager holds every line granted directly to anyone
below them with the same role; peers, reports and other workspaces gain nothing;
department lines are not passed up; only a caller who already sees every line may draw
the reports-to edge; loops and cross-workspace managers are refused.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Department, DepartmentMember, InboxGrant, OrgMembership, Role
from app.repositories import users as users_repo
from app.services import notifications
from app.services.inbox_access import resolve_access
from tests.conftest import auth_headers, create_org, register_and_login
from tests.test_p15_inbox_access import _add_number, _inbox_for

E_REP = "+12145550201"
E_SUB = "+12145550202"
E_BOSS = "+12145550203"
E_DEPT = "+12145550204"


async def _join(session, org_id, email, role_name="agent", reports_to=None):
    await_user = await users_repo.get_by_email(session, email)
    role = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == role_name))
    ).scalar_one()
    session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=await_user.id,
            role_id=role.id,
            reports_to_user_id=reports_to,
        )
    )
    await session.flush()
    return await_user


async def _grant(session, org_id, e164, user_id, role="member"):
    inbox = await _inbox_for(session, e164)
    session.add(
        InboxGrant(
            id=uuid.uuid4(), org_id=org_id, inbox_id=inbox.id,
            grantee_type="user", grantee_id=user_id, role=role,
        )
    )


async def _org_with_chain(client, session, tag):
    """boss -> lead -> rep ; peer reports to boss. Lines: rep=E_REP, lead=E_SUB (viewer),
    boss=E_BOSS. Returns (owner_token, org_id, users dict)."""
    token = await register_and_login(client, f"own-{tag}@example.com")
    org = await create_org(client, token, f"Hier {tag}")
    org_id = uuid.UUID(org["id"])
    for e in (E_REP, E_SUB, E_BOSS, E_DEPT):
        await _add_number(client, session, token, org_id, e)
    for name in ("boss", "lead", "rep", "peer"):
        await register_and_login(client, f"{name}-{tag}@example.com")
    set_org_context(session, org_id)
    boss = await _join(session, org_id, f"boss-{tag}@example.com")
    lead = await _join(session, org_id, f"lead-{tag}@example.com", reports_to=boss.id)
    rep = await _join(session, org_id, f"rep-{tag}@example.com", reports_to=lead.id)
    peer = await _join(session, org_id, f"peer-{tag}@example.com", reports_to=boss.id)
    await _grant(session, org_id, E_REP, rep.id)
    await _grant(session, org_id, E_SUB, lead.id, role="viewer")
    await _grant(session, org_id, E_BOSS, boss.id)
    await session.commit()
    return token, org_id, {"boss": boss, "lead": lead, "rep": rep, "peer": peer}


async def test_manager_inherits_reports_lines_transitively_with_same_role(client, session):
    _, org_id, u = await _org_with_chain(client, session, "t1")
    set_org_context(session, org_id)

    lead = await resolve_access(session, u["lead"].id, [])
    assert lead.can_use(E_REP)  # option 1: read, reply AND call on a report's line
    assert lead.can_view(E_SUB) and not lead.can_use(E_SUB)  # own viewer stays viewer
    assert not lead.can_view(E_BOSS)  # never upward

    boss = await resolve_access(session, u["boss"].id, [])
    assert boss.can_use(E_REP)  # two levels down
    assert boss.can_view(E_SUB) and not boss.can_use(E_SUB)  # viewer stays viewer
    assert boss.can_use(E_BOSS)


async def test_reports_and_peers_gain_nothing(client, session):
    _, org_id, u = await _org_with_chain(client, session, "t2")
    set_org_context(session, org_id)

    rep = await resolve_access(session, u["rep"].id, [])
    assert rep.can_use(E_REP)
    assert not rep.can_view(E_SUB) and not rep.can_view(E_BOSS)

    peer = await resolve_access(session, u["peer"].id, [])
    assert not any(peer.can_view(e) for e in (E_REP, E_SUB, E_BOSS, E_DEPT))


async def test_department_lines_of_reports_are_not_passed_up(client, session):
    _, org_id, u = await _org_with_chain(client, session, "t3")
    set_org_context(session, org_id)
    dept = Department(id=uuid.uuid4(), org_id=org_id, name="Sales")
    session.add(dept)
    await session.flush()
    session.add(DepartmentMember(id=uuid.uuid4(), org_id=org_id, department_id=dept.id, user_id=u["rep"].id))
    inbox = await _inbox_for(session, E_DEPT)
    session.add(InboxGrant(id=uuid.uuid4(), org_id=org_id, inbox_id=inbox.id,
                           grantee_type="department", grantee_id=dept.id, role="member"))
    await session.commit()

    assert (await resolve_access(session, u["rep"].id, [])).can_use(E_DEPT)
    assert not (await resolve_access(session, u["lead"].id, [])).can_view(E_DEPT)
    assert not (await resolve_access(session, u["boss"].id, [])).can_view(E_DEPT)


async def test_a_cycle_in_stored_data_terminates_and_grants_only_the_loop(client, session):
    _, org_id, u = await _org_with_chain(client, session, "t4")
    set_org_context(session, org_id)
    boss_m = (await session.execute(
        sa.select(OrgMembership).where(OrgMembership.user_id == u["boss"].id)
    )).scalar_one()
    boss_m.reports_to_user_id = u["rep"].id  # rep -> lead -> boss -> rep
    await session.commit()
    rep = await resolve_access(session, u["rep"].id, [])
    assert rep.can_use(E_BOSS)  # the loop members see each other; must not hang
    peer = await resolve_access(session, u["peer"].id, [])
    assert not peer.can_view(E_REP)


async def test_alerts_reach_every_manager_above_the_line_holder(client, session):
    _, org_id, u = await _org_with_chain(client, session, "t5")
    set_org_context(session, org_id)
    recipients = await notifications.recipients_for_number(session, E_REP)
    assert {u["rep"].id, u["lead"].id, u["boss"].id} <= recipients
    assert u["peer"].id not in recipients


async def test_set_manager_api_happy_path_loop_and_cross_org(client, session):
    token, org_id, u = await _org_with_chain(client, session, "t6")
    h = auth_headers(token, str(org_id))

    r = await client.put(f"/api/v1/orgs/current/members/{u['peer'].id}/manager",
                         json={"manager_user_id": str(u["lead"].id)}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["reports_to_user_id"] == str(u["lead"].id)
    set_org_context(session, org_id)
    assert (await resolve_access(session, u["lead"].id, [])).can_view(E_REP)

    # boss under his own report -> loop
    r = await client.put(f"/api/v1/orgs/current/members/{u['boss'].id}/manager",
                         json={"manager_user_id": str(u["rep"].id)}, headers=h)
    assert r.status_code == 409, r.text
    r = await client.put(f"/api/v1/orgs/current/members/{u['boss'].id}/manager",
                         json={"manager_user_id": str(u["boss"].id)}, headers=h)
    assert r.status_code == 409, r.text

    # a manager from another workspace
    t2 = await register_and_login(client, "other-t6@example.com")
    await create_org(client, t2, "Other t6")
    other = await users_repo.get_by_email(session, "other-t6@example.com")
    r = await client.put(f"/api/v1/orgs/current/members/{u['rep'].id}/manager",
                         json={"manager_user_id": str(other.id)}, headers=h)
    assert r.status_code == 404, r.text

    # clear
    r = await client.put(f"/api/v1/orgs/current/members/{u['rep'].id}/manager",
                         json={"manager_user_id": None}, headers=h)
    assert r.status_code == 200 and r.json()["reports_to_user_id"] is None
    set_org_context(session, org_id)
    assert not (await resolve_access(session, u["lead"].id, [])).can_view(E_REP)

    listed = (await client.get("/api/v1/orgs/current/members", headers=h)).json()
    by_id = {m["user_id"]: m for m in listed}
    assert by_id[str(u["peer"].id)]["reports_to_user_id"] == str(u["lead"].id)


async def test_only_a_caller_who_sees_every_line_may_draw_the_edge(client, session):
    """members:update alone must not let someone make themselves anyone's manager."""
    token, org_id, u = await _org_with_chain(client, session, "t7")
    set_org_context(session, org_id)
    session.add(Role(id=uuid.uuid4(), org_id=org_id, name="hr",
                     permissions=["members:read", "members:update"]))
    await session.flush()
    hr_role = (await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == "hr"))).scalar_one()
    peer_m = (await session.execute(
        sa.select(OrgMembership).where(OrgMembership.user_id == u["peer"].id)
    )).scalar_one()
    peer_m.role_id = hr_role.id
    await session.commit()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "peer-t7@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 200, login.text
    peer_token = login.json()["access_token"]

    r = await client.put(f"/api/v1/orgs/current/members/{u['rep'].id}/manager",
                         json={"manager_user_id": str(u["peer"].id)},
                         headers=auth_headers(peer_token, str(org_id)))
    assert r.status_code == 403, r.text
    assert "manages every line" in r.text  # refused by the hierarchy gate, not auth
    set_org_context(session, org_id)
    assert not (await resolve_access(session, u["peer"].id, [])).can_view(E_REP)


async def test_removing_a_manager_detaches_their_reports(client, session):
    token, org_id, u = await _org_with_chain(client, session, "t8")
    r = await client.delete(f"/api/v1/orgs/current/members/{u['lead'].id}",
                            headers=auth_headers(token, str(org_id)))
    assert r.status_code == 204, r.text
    set_org_context(session, org_id)
    rep_m = (await session.execute(
        sa.select(OrgMembership).where(OrgMembership.user_id == u["rep"].id)
    )).scalar_one()
    await session.refresh(rep_m)
    assert rep_m.reports_to_user_id is None


async def test_inherited_lines_are_marked_and_do_not_ring_the_manager(client, session):
    from app.api.routes.softphone import _event_visible

    _, org_id, u = await _org_with_chain(client, session, "t9")
    set_org_context(session, org_id)
    lead = await resolve_access(session, u["lead"].id, [])
    assert lead.inherited_e164s == frozenset({E_REP})  # own E_SUB is not "inherited"
    rep = await resolve_access(session, u["rep"].id, [])
    assert rep.inherited_e164s == frozenset()

    ring = {"type": "call.ring", "call_id": "c1", "to": E_REP}
    assert await _event_visible(ring, rep, org_id, u["rep"].id) is True
    assert await _event_visible(ring, lead, org_id, u["lead"].id) is False
    parked = {"type": "call.parked", "call_id": "c1", "to": E_REP}
    assert await _event_visible(parked, lead, org_id, u["lead"].id) is True


async def test_my_numbers_for_911_never_include_reports_lines(client, session):
    """A manager's 911 address must never be pushed onto a report's number."""
    from app.services import profile

    _, org_id, u = await _org_with_chain(client, session, "t10")
    set_org_context(session, org_id)
    boss_numbers = {n.e164 for n in await profile.assigned_numbers(session, org_id, u["boss"].id)}
    assert boss_numbers == {E_BOSS}
    lead_numbers = {n.e164 for n in await profile.assigned_numbers(session, org_id, u["lead"].id)}
    assert E_REP not in lead_numbers


async def test_an_edge_in_another_workspace_grants_nothing_here(client, session):
    _, org_a, ua = await _org_with_chain(client, session, "t11a")
    # Workspace B: same people, but boss_b manages rep of workspace A by id.
    tb = await register_and_login(client, "own-t11b@example.com")
    org_b = uuid.UUID((await create_org(client, tb, "Hier t11b"))["id"])
    set_org_context(session, org_b)
    role = (await session.execute(sa.select(Role).where(Role.org_id == org_b, Role.name == "agent"))).scalar_one()
    session.add(OrgMembership(id=uuid.uuid4(), org_id=org_b, user_id=ua["peer"].id,
                              role_id=role.id, reports_to_user_id=None))
    session.add(OrgMembership(id=uuid.uuid4(), org_id=org_b, user_id=ua["rep"].id,
                              role_id=role.id, reports_to_user_id=ua["peer"].id))
    await session.commit()

    set_org_context(session, org_a)
    peer_in_a = await resolve_access(session, ua["peer"].id, [])
    assert not peer_in_a.can_view(E_REP)
