from __future__ import annotations

import uuid

import sqlalchemy as sa

import app.services.agent as agent_svc
from app.db.base import set_org_context
from app.models import (
    Call,
    Contact,
    ContactPhone,
    DepartmentMember,
    Inbox,
    InboxGrant,
    MessageThread,
    Org,
    OrgMembership,
    OrgNumber,
    Role,
)
from app.repositories import users as users_repo
from app.services import contact_visibility
from tests.conftest import auth_headers, create_contact, create_org, register_and_login

A = "+12145550100"
B = "+12145550111"
AGENT_PHONE = "+12135550122"


async def _register_member(client, session, org_id, email, role_name="agent", role_id=None):
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    if role_id is None:
        role_id = (
            await session.execute(sa.select(Role).where(Role.name == role_name))
        ).scalar_one().id
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role_id)
    )
    await session.commit()
    return token, user


async def _join_dept(session, org_id, dept_id, user_id, *, is_lead=False) -> None:
    set_org_context(session, org_id)
    session.add(
        DepartmentMember(
            id=uuid.uuid4(),
            org_id=org_id,
            department_id=dept_id,
            user_id=user_id,
            is_lead=is_lead,
        )
    )
    await session.commit()


async def test_visibility_everyone_returns_no_predicate(client, session):
    token = await register_and_login(client, "vis-everyone@example.com")
    org = await create_org(client, token, "Vis Everyone")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    org_row = await session.get(Org, org_id)
    scope = await contact_visibility.resolve_scope(
        session, org_row, user_id=uuid.uuid4(), permissions=["contacts:write"]
    )
    assert contact_visibility.visible_contacts_filter(scope) is None

    h_owner = auth_headers(token, org["id"])
    d1 = await client.post("/api/v1/departments", json={"name": "Everyone Sales"}, headers=h_owner)
    d2 = await client.post("/api/v1/departments", json={"name": "Everyone Support"}, headers=h_owner)
    d1_id = uuid.UUID(d1.json()["id"])
    d2_id = uuid.UUID(d2.json()["id"])

    agent_token, agent = await _register_member(
        client, session, org_id, "everyone.agent@example.com"
    )
    admin_token, admin = await _register_member(
        client, session, org_id, "everyone.admin@example.com", "admin"
    )
    await _join_dept(session, org_id, d1_id, agent.id)
    await _join_dept(session, org_id, d2_id, admin.id)

    contact_agent = await create_contact(client, agent_token, org["id"], "Everyone Agent C", [A])
    contact_admin = await create_contact(client, admin_token, org["id"], "Everyone Admin C", [B])

    agent_list = await client.get("/api/v1/contacts", headers=auth_headers(agent_token, org["id"]))
    admin_list = await client.get("/api/v1/contacts", headers=auth_headers(admin_token, org["id"]))
    assert agent_list.status_code == 200, agent_list.text
    assert admin_list.status_code == 200, admin_list.text
    agent_ids = {c["id"] for c in agent_list.json()}
    admin_ids = {c["id"] for c in admin_list.json()}
    assert agent_ids == admin_ids
    assert {contact_agent["id"], contact_admin["id"]}.issubset(agent_ids)


async def test_visibility_department_hides_other_team(client, session):
    owner_token = await register_and_login(client, "vis-dept-owner@example.com")
    org = await create_org(client, owner_token, "Vis Dept")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    r = await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "department"},
        headers=h_owner,
    )
    assert r.status_code == 200, r.text

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])
    support_id = uuid.UUID(support.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "alice.dept@example.com")
    bob_token, bob = await _register_member(client, session, org_id, "bob.dept@example.com")
    await _join_dept(session, org_id, sales_id, alice.id)
    await _join_dept(session, org_id, support_id, bob.id)

    contact = await create_contact(
        client, alice_token, org["id"], "Alice Contact", [AGENT_PHONE]
    )
    contact_id = contact["id"]

    h_bob = auth_headers(bob_token, org["id"])
    got = await client.get(f"/api/v1/contacts/{contact_id}", headers=h_bob)
    assert got.status_code == 404, got.text

    listed = await client.get("/api/v1/contacts", headers=h_bob)
    assert listed.status_code == 200, listed.text
    assert all(c["id"] != contact_id for c in listed.json())

    searched = await client.get(
        "/api/v1/contacts", params={"q": AGENT_PHONE, "limit": 100}, headers=h_bob
    )
    assert searched.status_code == 200, searched.text
    assert all(c["id"] != contact_id for c in searched.json())


async def test_visibility_owner_lead_sees_team(client, session):
    owner_token = await register_and_login(client, "vis-lead-owner@example.com")
    org = await create_org(client, owner_token, "Vis Lead")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "owner"},
        headers=h_owner,
    )
    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    dept_id = uuid.UUID(dept.json()["id"])

    lead_token, lead = await _register_member(client, session, org_id, "lead@example.com")
    peer_token, peer = await _register_member(client, session, org_id, "peer@example.com")
    await _join_dept(session, org_id, dept_id, lead.id, is_lead=True)
    await _join_dept(session, org_id, dept_id, peer.id, is_lead=False)

    contact = await create_contact(
        client, peer_token, org["id"], "Peer Contact", [AGENT_PHONE]
    )
    h_lead = auth_headers(lead_token, org["id"])
    got = await client.get(f"/api/v1/contacts/{contact['id']}", headers=h_lead)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == contact["id"]


async def test_read_all_bypasses_policy(client, session):
    owner_token = await register_and_login(client, "vis-readall-owner@example.com")
    org = await create_org(client, owner_token, "Vis ReadAll")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "owner"},
        headers=h_owner,
    )
    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    dept_id = uuid.UUID(dept.json()["id"])

    admin_token, admin = await _register_member(client, session, org_id, "admin.readall@example.com", "admin")
    agent_token, agent = await _register_member(client, session, org_id, "agent.readall@example.com")
    await _join_dept(session, org_id, dept_id, agent.id)

    contact = await create_contact(client, agent_token, org["id"], "Agent Contact", [AGENT_PHONE])

    h_admin = auth_headers(admin_token, org["id"])
    got = await client.get(f"/api/v1/contacts/{contact['id']}", headers=h_admin)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == contact["id"]


async def test_unowned_unteamed_visible_to_writers(client, session):
    owner_token = await register_and_login(client, "vis-unowned-owner@example.com")
    org = await create_org(client, owner_token, "Vis Unowned")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "department"},
        headers=h_owner,
    )

    agent_token, agent = await _register_member(client, session, org_id, "agent.unowned@example.com")

    set_org_context(session, org_id)
    contact = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="Unowned Contact",
        attributes={},
        owner_user_id=None,
        department_id=None,
    )
    session.add(contact)
    await session.commit()

    h_agent = auth_headers(agent_token, org["id"])
    got = await client.get(f"/api/v1/contacts/{contact.id}", headers=h_agent)
    assert got.status_code == 200, got.text


async def test_inbound_autocreate_sets_department_from_inbox(client, session):
    owner_token = await register_and_login(client, "vis-inbox-owner@example.com")
    org = await create_org(client, owner_token, "Vis Inbox Dept")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    dept = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    dept_id = uuid.UUID(dept.json()["id"])

    set_org_context(session, org_id)
    number = (await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == A))).scalar_one()
    inbox = (await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))).scalar_one()
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="department",
            grantee_id=dept_id,
            role="member",
        )
    )

    contact = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="+12135550123",
        attributes={},
        owner_user_id=None,
        department_id=None,
    )
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=A,
        contact_e164="+12135550123",
        contact_id=contact.id,
        status="open",
        assigned_user_id=None,
    )
    session.add_all([contact, thread])
    await session.commit()

    await contact_visibility.stamp_inbound_ownership(session, contact, thread=thread)
    assert contact.department_id == dept_id


async def test_inbound_autocreate_sets_owner_from_thread_assignee(client, session):
    owner_token = await register_and_login(client, "vis-inbound-owner2@example.com")
    org = await create_org(client, owner_token, "Vis Inbox Owner")
    owner_user = await users_repo.get_by_email(session, "vis-inbound-owner2@example.com")

    contact = Contact(
        id=uuid.uuid4(),
        org_id=uuid.UUID(org["id"]),
        display_name="+12135550124",
        attributes={},
        owner_user_id=None,
        department_id=uuid.uuid4(),  # skip inbox lookup for this focused test
    )
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=uuid.UUID(org["id"]),
        our_e164=A,
        contact_e164="+12135550124",
        contact_id=contact.id,
        status="open",
        assigned_user_id=owner_user.id,
    )

    await contact_visibility.stamp_inbound_ownership(session, contact, thread=thread)
    assert contact.owner_user_id == owner_user.id


async def test_agent_contact_lookup_scoped_to_inbox_department(client, session, monkeypatch):
    owner_token = await register_and_login(client, "vis-worker-owner@example.com")
    org = await create_org(client, owner_token, "Vis Worker")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "department"},
        headers=h_owner,
    )

    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support_id = uuid.UUID(support.json()["id"])
    sales_id = uuid.UUID(sales.json()["id"])

    set_org_context(session, org_id)
    number = (await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == A))).scalar_one()
    inbox = (await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))).scalar_one()
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="department",
            grantee_id=support_id,
            role="member",
        )
    )

    contact = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="Sales Secret",
        attributes={},
        owner_user_id=None,
        department_id=sales_id,
    )
    session.add_all(
        [
            contact,
            ContactPhone(
                id=uuid.uuid4(),
                org_id=org_id,
                contact_id=contact.id,
                e164=AGENT_PHONE,
                label="mobile",
                is_primary=True,
            ),
        ]
    )

    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=AGENT_PHONE,
        our_e164=A,
        carrier="test",
        status="active",
    )
    session.add(call)
    await session.commit()

    monkeypatch.setattr(agent_svc, "verify_worker_token", lambda *a, **k: True)

    async def fake_get_contact_context(*a, **k):
        return {"name": "Sales Secret", "tags": ["vip"], "last_messages": []}

    monkeypatch.setattr(agent_svc, "get_contact_context", fake_get_contact_context)

    r = await client.get(
        f"/api/v1/agent/contact/{AGENT_PHONE}",
        params={"call_id": str(call.id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == ""
    assert body["tags"] == []


async def test_full_flow_owner_policy_reassign(client, session):
    owner_token = await register_and_login(client, "vis-flow-owner@example.com")
    org = await create_org(client, owner_token, "Vis Flow")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "owner"},
        headers=h_owner,
    )

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])
    support_id = uuid.UUID(support.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "alice.flow@example.com")
    lead_token, lead = await _register_member(client, session, org_id, "lead.flow@example.com")
    peer_token, peer = await _register_member(client, session, org_id, "peer.flow@example.com")
    admin_token, _admin = await _register_member(client, session, org_id, "admin.flow@example.com", "admin")

    await _join_dept(session, org_id, sales_id, alice.id, is_lead=False)
    await _join_dept(session, org_id, sales_id, lead.id, is_lead=True)
    await _join_dept(session, org_id, support_id, peer.id, is_lead=False)

    contact = await create_contact(client, alice_token, org["id"], "Flow Contact", [AGENT_PHONE])
    contact_id = contact["id"]

    h_lead = auth_headers(lead_token, org["id"])
    assert (await client.get(f"/api/v1/contacts/{contact_id}", headers=h_lead)).status_code == 200

    h_peer = auth_headers(peer_token, org["id"])
    assert (await client.get(f"/api/v1/contacts/{contact_id}", headers=h_peer)).status_code == 404

    reassigned = await client.patch(
        f"/api/v1/contacts/{contact_id}/owner",
        json={"owner_user_id": str(peer.id)},
        headers=auth_headers(admin_token, org["id"]),
    )
    assert reassigned.status_code == 200, reassigned.text

    assert (await client.get(f"/api/v1/contacts/{contact_id}", headers=h_peer)).status_code == 200
    assert (
        await client.get(
            f"/api/v1/contacts/{contact_id}", headers=auth_headers(alice_token, org["id"])
        )
    ).status_code == 404
