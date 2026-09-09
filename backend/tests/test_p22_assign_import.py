from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.models import (
    Contact,
    ContactList,
    ContactPhone,
    DepartmentMember,
    OrgMembership,
    Role,
)
from app.repositories import users as users_repo
from app.services import list_import
from tests.conftest import auth_headers, create_contact, create_org, register_and_login


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


async def _join_dept(session, org_id, dept_id, user_id, *, is_lead=False):
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


async def test_assign_requires_permission(client, session):
    owner_token = await register_and_login(client, "assign-perm-owner@example.com")
    org = await create_org(client, owner_token, "Assign Perm")
    org_id = uuid.UUID(org["id"])
    contact = await create_contact(
        client, owner_token, org["id"], "Owner Contact", ["+12135550100"]
    )
    agent_token, agent = await _register_member(client, session, org_id, "agent.assign@example.com")

    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}/owner",
        json={"owner_user_id": str(agent.id)},
        headers=auth_headers(agent_token, org["id"]),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "permission_denied"


async def test_assign_rejects_non_member(client, session):
    owner_token = await register_and_login(client, "assign-nonmember-owner@example.com")
    org = await create_org(client, owner_token, "Assign NonMember")
    contact = await create_contact(
        client, owner_token, org["id"], "Owner Contact", ["+12135550101"]
    )
    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}/owner",
        json={"owner_user_id": str(uuid.uuid4())},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 422, r.text


async def test_assign_owner_explicit_null_clears_but_omitted_key_is_untouched(client, session):
    """The assign drawer always sends both keys; an explicit null must CLEAR the field
    (not be treated as "unchanged"), while a key the caller never sends at all must be
    left alone - these are the two halves of the exclude_unset contract."""
    owner_token = await register_and_login(client, "assign-null-owner@example.com")
    org = await create_org(client, owner_token, "Assign Null")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    contact = await create_contact(
        client, owner_token, org["id"], "Null Contact", ["+12135550199"]
    )
    agent_token, agent = await _register_member(client, session, org_id, "agent.null@example.com")
    dept = await client.post("/api/v1/departments", json={"name": "Ops"}, headers=h_owner)
    dept_id = dept.json()["id"]

    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}/owner",
        json={"owner_user_id": str(agent.id), "department_id": dept_id},
        headers=h_owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] == str(agent.id)
    assert r.json()["department_id"] == dept_id

    # Omitting a key entirely leaves it untouched.
    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}/owner",
        json={},
        headers=h_owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] == str(agent.id)
    assert r.json()["department_id"] == dept_id

    # Explicit null CLEARS the owner; the omitted department_id stays.
    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}/owner",
        json={"owner_user_id": None},
        headers=h_owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner_user_id"] is None
    assert r.json()["department_id"] == dept_id


async def test_bulk_assign_caps_at_500(client, session):
    owner_token = await register_and_login(client, "assign-cap-owner@example.com")
    org = await create_org(client, owner_token, "Assign Cap")
    ids = [str(uuid.uuid4()) for _ in range(501)]
    r = await client.post(
        "/api/v1/contacts/bulk/assign",
        json={"contact_ids": ids},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 422, r.text
    assert "at most 500" in r.json()["error"]["message"]


async def test_bulk_assign_skips_invisible_contacts(client, session):
    owner_token = await register_and_login(client, "assign-skip-owner@example.com")
    org = await create_org(client, owner_token, "Assign Skip")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "department"},
        headers=h_owner,
    )

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])
    support_id = uuid.UUID(support.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "alice.bulk@example.com")
    bob_token, bob = await _register_member(client, session, org_id, "bob.bulk@example.com")
    await _join_dept(session, org_id, sales_id, alice.id)
    await _join_dept(session, org_id, support_id, bob.id)

    contact1 = await create_contact(client, alice_token, org["id"], "Sales C", ["+12135550102"])
    contact2 = await create_contact(client, bob_token, org["id"], "Support C", ["+12135550103"])

    assigner_role = await client.post(
        "/api/v1/roles",
        json={
            "name": "Assigner",
            "permissions": ["contacts:read", "contacts:write", "contacts:assign"],
        },
        headers=h_owner,
    )
    assigner_role_id = uuid.UUID(assigner_role.json()["id"])
    assigner_token, assigner_user = await _register_member(
        client, session, org_id, "assigner.bulk@example.com", role_id=assigner_role_id
    )
    await _join_dept(session, org_id, sales_id, assigner_user.id)

    r = await client.post(
        "/api/v1/contacts/bulk/assign",
        json={
            "contact_ids": [contact1["id"], contact2["id"]],
            "owner_user_id": str(assigner_user.id),
        },
        headers=auth_headers(assigner_token, org["id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped"] == 1


async def test_import_owner_column_maps_email_and_reports_unknowns(client, session):
    owner_token = await register_and_login(client, "import-owner-owner@example.com")
    org = await create_org(client, owner_token, "Import Owner")
    org_id = uuid.UUID(org["id"])

    known_token, known_user = await _register_member(
        client, session, org_id, "known.import@example.com"
    )

    set_org_context(session, org_id)
    list_id = uuid.uuid4()
    session.add(
        ContactList(
            id=list_id,
            org_id=org_id,
            name="p22-import.csv",
            status="importing",
            total_rows=0,
            accepted_count=0,
            invalid_count=0,
            duplicate_count=0,
            dnc_count=0,
        )
    )
    await session.commit()

    csv_bytes = (
        b"Name,Phone,Owner\n"
        b"Known,+12135550110,known.import@example.com\n"
        b"Unknown,+12135550111,unknown.import@example.com\n"
    )

    result = await list_import.run_import(
        get_sessionmaker(),
        list_id=list_id,
        org_id=org_id,
        filename="p22-import.csv",
        data=csv_bytes,
        mapping={"phone": "Phone", "owner": "Owner"},
    )

    assert result["unknown_owner_emails"] == ["unknown.import@example.com"]
    assert result["assigned"] == 1

    async with get_sessionmaker()() as s:
        set_org_context(s, org_id)
        contact = (
            await s.execute(
                sa.select(Contact)
                .join(ContactPhone, ContactPhone.contact_id == Contact.id)
                .where(ContactPhone.e164 == "+12135550110")
            )
        ).scalar_one()
        assert contact.owner_user_id == known_user.id


async def test_contacts_filter_chips_mine_team_unowned(client, session):
    owner_token = await register_and_login(client, "chips-owner@example.com")
    org = await create_org(client, owner_token, "Chips")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])
    support_id = uuid.UUID(support.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "alice.chips@example.com")
    bob_token, bob = await _register_member(client, session, org_id, "bob.chips@example.com")
    await _join_dept(session, org_id, sales_id, alice.id)
    await _join_dept(session, org_id, support_id, bob.id)

    contact_alice = await create_contact(client, alice_token, org["id"], "Alice C", ["+12135550120"])
    contact_bob = await create_contact(client, bob_token, org["id"], "Bob C", ["+12135550121"])

    set_org_context(session, org_id)
    contact_unowned = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="Unowned C",
        attributes={},
        owner_user_id=None,
        department_id=None,
    )
    session.add(contact_unowned)
    await session.commit()

    h_alice = auth_headers(alice_token, org["id"])

    mine = await client.get("/api/v1/contacts", params={"scope": "mine"}, headers=h_alice)
    assert mine.status_code == 200, mine.text
    mine_ids = {c["id"] for c in mine.json()}
    assert contact_alice["id"] in mine_ids
    assert contact_bob["id"] not in mine_ids
    assert str(contact_unowned.id) not in mine_ids

    team = await client.get("/api/v1/contacts", params={"scope": "team"}, headers=h_alice)
    assert team.status_code == 200, team.text
    team_ids = {c["id"] for c in team.json()}
    assert contact_alice["id"] in team_ids
    assert contact_bob["id"] not in team_ids

    unowned = await client.get("/api/v1/contacts", params={"scope": "unowned"}, headers=h_alice)
    assert unowned.status_code == 200, unowned.text
    unowned_ids = {c["id"] for c in unowned.json()}
    assert str(contact_unowned.id) in unowned_ids
    assert contact_alice["id"] not in unowned_ids


async def test_custom_team_lead_role_can_reassign_inside_team(client, session):
    owner_token = await register_and_login(client, "lead-reassign-owner@example.com")
    org = await create_org(client, owner_token, "Lead Reassign")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": "department"},
        headers=h_owner,
    )

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])
    support_id = uuid.UUID(support.json()["id"])

    lead_role = await client.post(
        "/api/v1/roles",
        json={
            "name": "Team lead",
            "permissions": ["contacts:read", "contacts:write", "contacts:assign"],
        },
        headers=h_owner,
    )
    lead_role_id = uuid.UUID(lead_role.json()["id"])

    lead_token, lead_user = await _register_member(
        client, session, org_id, "lead.custom@example.com", role_id=lead_role_id
    )

    alice_token, alice = await _register_member(client, session, org_id, "alice.team@example.com")
    bob_token, bob = await _register_member(client, session, org_id, "bob.team@example.com")

    await _join_dept(session, org_id, sales_id, lead_user.id, is_lead=False)
    await _join_dept(session, org_id, sales_id, alice.id)
    await _join_dept(session, org_id, support_id, bob.id)

    contact_sales = await create_contact(client, alice_token, org["id"], "Sales Lead C", ["+12135550122"])
    contact_support = await create_contact(client, bob_token, org["id"], "Support Lead C", ["+12135550123"])

    h_lead = auth_headers(lead_token, org["id"])

    ok = await client.patch(
        f"/api/v1/contacts/{contact_sales['id']}/owner",
        json={"owner_user_id": str(alice.id)},
        headers=h_lead,
    )
    assert ok.status_code == 200, ok.text

    hidden = await client.patch(
        f"/api/v1/contacts/{contact_support['id']}/owner",
        json={"owner_user_id": str(bob.id)},
        headers=h_lead,
    )
    assert hidden.status_code == 404, hidden.text
