"""P22 adversarial verification tests (Opus verifier).

Every test here exists because a mutation of the P22 code survived the shipped
test files, or because a cross-tenant / enumeration path was never probed.
Do not delete one without killing the mutation it pins.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

import app.services.agent as agent_svc
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    Call,
    Contact,
    ContactPhone,
    Department,
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
from tests.conftest import (
    auth_headers,
    create_contact,
    create_org,
    fixture_bytes,
    make_org_with_number,
    register_and_login,
    webhook_auth_headers,
)

A = "+12145550100"
B = "+12145550111"
C = "+12145550133"
D = "+12145550144"
HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
THEIRS = "+19725550199"


# ----------------------------------------------------------------------------------
# Local helpers (mirrors of the ones in test_p22_visibility.py)
# ----------------------------------------------------------------------------------
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


async def _set_policy(client, headers, policy: str) -> None:
    r = await client.patch(
        "/api/v1/orgs/current/settings",
        json={"contact_visibility": policy},
        headers=headers,
    )
    assert r.status_code == 200, r.text


async def _unscoped(session, model):
    return list(
        (
            await session.execute(
                sa.select(model).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )


async def _make_role(client, headers, name, permissions):
    r = await client.post(
        "/api/v1/roles", json={"name": name, "permissions": permissions}, headers=headers
    )
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


# ==================================================================================
# 1. Cross-tenant probes
# ==================================================================================
async def test_cross_org_member_cannot_reach_foreign_contacts(client, session):
    """Org A runs the permissive `everyone` policy; an org-B admin holding
    contacts:read_all must still be blind to org A. read_all is an intra-org bypass."""
    a_token = await register_and_login(client, "x-org-a@example.com")
    org_a = await create_org(client, a_token, "Cross Org A")
    h_a = auth_headers(a_token, org_a["id"])
    await _set_policy(client, h_a, "everyone")
    secret = await create_contact(client, a_token, org_a["id"], "Org A Secret", [A])

    b_token = await register_and_login(client, "x-org-b@example.com")
    org_b = await create_org(client, b_token, "Cross Org B")
    org_b_id = uuid.UUID(org_b["id"])
    admin_token, admin = await _register_member(
        client, session, org_b_id, "x-org-b-admin@example.com", "admin"
    )
    h_b = auth_headers(admin_token, org_b["id"])

    # The org-B admin really does hold read_all inside their own org.
    set_org_context(session, org_b_id)
    admin_role = (
        await session.execute(sa.select(Role).where(Role.name == "admin"))
    ).scalar_one()
    assert "contacts:read_all" in (admin_role.permissions or [])

    cid = secret["id"]
    assert (await client.get("/api/v1/contacts", headers=h_b)).json() == []
    q = await client.get("/api/v1/contacts", params={"q": "Org A Secret"}, headers=h_b)
    assert q.json() == []
    assert (await client.get(f"/api/v1/contacts/{cid}", headers=h_b)).status_code == 404
    assert (
        await client.patch(f"/api/v1/contacts/{cid}", json={"display_name": "pwn"}, headers=h_b)
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/contacts/{cid}/owner",
            json={"owner_user_id": str(admin.id)},
            headers=h_b,
        )
    ).status_code == 404
    bulk = await client.post(
        "/api/v1/contacts/bulk/assign", json={"contact_ids": [cid]}, headers=h_b
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json() == {"updated": 0, "skipped": 1}

    # And presenting org A's id without a membership there is refused outright.
    forged = await client.get(
        f"/api/v1/contacts/{cid}", headers=auth_headers(admin_token, org_a["id"])
    )
    assert forged.status_code in (403, 404), forged.text

    # Nothing was mutated.
    rows = [c for c in await _unscoped(session, Contact) if c.id == uuid.UUID(cid)]
    assert rows[0].display_name == "Org A Secret"
    assert rows[0].owner_user_id != admin.id


async def test_cross_org_roles_are_unreachable(client, session):
    """roles.py::_get_role uses session.get(Role, id) with no explicit org filter -
    this pins that the session-level tenant guard actually covers Session.get()."""
    a_token = await register_and_login(client, "x-role-a@example.com")
    org_a = await create_org(client, a_token, "Role Org A")
    h_a = auth_headers(a_token, org_a["id"])
    role_a = await _make_role(client, h_a, "OrgA Custom", ["contacts:read"])

    b_token = await register_and_login(client, "x-role-b@example.com")
    org_b = await create_org(client, b_token, "Role Org B")
    h_b = auth_headers(b_token, org_b["id"])

    listed = await client.get("/api/v1/roles", headers=h_b)
    assert listed.status_code == 200, listed.text
    assert str(role_a) not in {r["id"] for r in listed.json()}
    assert "OrgA Custom" not in {r["name"] for r in listed.json()}

    patched = await client.patch(
        f"/api/v1/roles/{role_a}", json={"name": "pwned"}, headers=h_b
    )
    assert patched.status_code == 404, patched.text

    cloned = await client.post(
        "/api/v1/roles", json={"name": "Clone", "clone_from": str(role_a)}, headers=h_b
    )
    assert cloned.status_code == 404, cloned.text

    deleted = await client.delete(f"/api/v1/roles/{role_a}", headers=h_b)
    assert deleted.status_code == 404, deleted.text

    # Org A's role is untouched and still there.
    still = await client.get("/api/v1/roles", headers=h_a)
    assert "OrgA Custom" in {r["name"] for r in still.json()}


# ==================================================================================
# 2. Enumeration: every by-id contact route must 404, never 403 or 200
# ==================================================================================
async def test_owner_policy_by_id_routes_all_404_never_403_or_200(client, session):
    owner_token = await register_and_login(client, "enum-owner@example.com")
    org = await create_org(client, owner_token, "Enum Org")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, "owner")

    tag = await client.post("/api/v1/tags", json={"name": "enum-tag"}, headers=h_owner)
    tag_id = tag.json()["id"]

    alice_token, alice = await _register_member(client, session, org_id, "enum.alice@example.com")
    # Bob holds read/write/assign but NOT contacts:read_all, and shares no department.
    bob_role = await _make_role(
        client, h_owner, "Enum Assigner", ["contacts:read", "contacts:write", "contacts:assign"]
    )
    bob_token, _bob = await _register_member(
        client, session, org_id, "enum.bob@example.com", role_id=bob_role
    )

    contact = await create_contact(client, alice_token, org["id"], "Alice Only", [B])
    cid = contact["id"]
    h_bob = auth_headers(bob_token, org["id"])

    probes = [
        ("GET", f"/api/v1/contacts/{cid}", None),
        ("PATCH", f"/api/v1/contacts/{cid}", {"display_name": "nope"}),
        ("GET", f"/api/v1/contacts/{cid}/notes", None),
        ("POST", f"/api/v1/contacts/{cid}/notes", {"body": "nope"}),
        ("PUT", f"/api/v1/contacts/{cid}/tags", {"tag_ids": [tag_id]}),
        ("PATCH", f"/api/v1/contacts/{cid}/owner", {"owner_user_id": None}),
        ("DELETE", f"/api/v1/contacts/{cid}", None),
    ]
    leaks = []
    for method, url, body in probes:
        kwargs = {"headers": h_bob}
        if body is not None:
            kwargs["json"] = body
        r = await client.request(method, url, **kwargs)
        if r.status_code != 404:
            leaks.append(f"{method} {url} -> {r.status_code} {r.text[:120]}")
    assert not leaks, "by-id routes that do not fail closed with 404: " + "; ".join(leaks)

    # The contact still exists and still belongs to Alice.
    still = await client.get(f"/api/v1/contacts/{cid}", headers=auth_headers(alice_token, org["id"]))
    assert still.status_code == 200
    assert still.json()["owner_user_id"] == str(alice.id)


# ==================================================================================
# 3. Machine path: /agent/contact/{e164}
# ==================================================================================
async def _agent_lookup_fixture(client, session, monkeypatch, *, policy, contact_kwargs, phone):
    owner_token = await register_and_login(
        client, f"mach-{uuid.uuid4().hex[:8]}@example.com"
    )
    org = await create_org(client, owner_token, f"Machine {uuid.uuid4().hex[:6]}")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, policy)

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

    resolved = dict(contact_kwargs)
    if resolved.get("department_id") == "support":
        resolved["department_id"] = support_id
    elif resolved.get("department_id") == "sales":
        resolved["department_id"] = sales_id

    contact = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="Machine Subject",
        attributes={},
        **resolved,
    )
    session.add_all(
        [
            contact,
            ContactPhone(
                id=uuid.uuid4(),
                org_id=org_id,
                contact_id=contact.id,
                e164=phone,
                label="mobile",
                is_primary=True,
            ),
        ]
    )
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=phone,
        our_e164=A,
        carrier="test",
        status="active",
    )
    session.add(call)
    await session.commit()

    monkeypatch.setattr(agent_svc, "verify_worker_token", lambda *a, **k: True)

    async def fake_get_contact_context(*a, **k):
        return {"name": "Machine Subject", "tags": ["vip"], "last_messages": []}

    monkeypatch.setattr(agent_svc, "get_contact_context", fake_get_contact_context)

    r = await client.get(
        f"/api/v1/agent/contact/{phone}", params={"call_id": str(call.id)}
    )
    assert r.status_code == 200, r.text
    return r.json(), org_id, support_id, sales_id


async def test_agent_lookup_sees_contact_in_its_own_inbox_department(
    client, session, monkeypatch
):
    """Positive half of the machine scoping: dropping the department clause from
    machine_scope must break something."""
    body, *_ = await _agent_lookup_fixture(
        client,
        session,
        monkeypatch,
        policy="owner",
        contact_kwargs={"owner_user_id": None, "department_id": "support"},
        phone="+12135551001",
    )
    assert body["name"] == "Machine Subject"
    assert body["tags"] == ["vip"]


async def test_agent_lookup_sees_unowned_unteamed_contact(client, session, monkeypatch):
    body, *_ = await _agent_lookup_fixture(
        client,
        session,
        monkeypatch,
        policy="owner",
        contact_kwargs={"owner_user_id": None, "department_id": None},
        phone="+12135551002",
    )
    assert body["name"] == "Machine Subject"


async def test_agent_lookup_never_honours_read_all(client, session, monkeypatch):
    """A contact that is reachable ONLY via contacts:read_all (owned by a human, in a
    foreign department) must stay hidden from the worker."""
    owner_token = await register_and_login(client, "mach-readall@example.com")
    org = await create_org(client, owner_token, "Machine ReadAll")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, "owner")

    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support_id = uuid.UUID(support.json()["id"])
    sales_id = uuid.UUID(sales.json()["id"])

    _admin_token, admin = await _register_member(
        client, session, org_id, "mach.admin@example.com", "admin"
    )

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
    phone = "+12135551003"
    contact = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="Read All Only",
        attributes={},
        owner_user_id=admin.id,
        department_id=sales_id,
    )
    session.add_all(
        [
            contact,
            ContactPhone(
                id=uuid.uuid4(),
                org_id=org_id,
                contact_id=contact.id,
                e164=phone,
                label="mobile",
                is_primary=True,
            ),
        ]
    )
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=phone,
        our_e164=A,
        carrier="test",
        status="active",
    )
    session.add(call)
    await session.commit()

    # It IS reachable to a read_all holder over the human API - so the machine result
    # below is genuinely "read_all was not honoured", not "the contact does not exist".
    admin_get = await client.get(
        f"/api/v1/contacts/{contact.id}", headers=auth_headers(_admin_token, org["id"])
    )
    assert admin_get.status_code == 200, admin_get.text

    monkeypatch.setattr(agent_svc, "verify_worker_token", lambda *a, **k: True)

    async def fake_get_contact_context(*a, **k):
        return {"name": "Read All Only", "tags": ["vip"], "last_messages": []}

    monkeypatch.setattr(agent_svc, "get_contact_context", fake_get_contact_context)

    r = await client.get(
        f"/api/v1/agent/contact/{phone}", params={"call_id": str(call.id)}
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == ""
    assert r.json()["tags"] == []


async def test_department_for_inbox_number_ignores_non_department_grants(client, session):
    """The grantee_type == 'department' filter is load-bearing: grantee_id is a bare
    UUID column shared by user and department grants."""
    owner_token = await register_and_login(client, "grantee-type@example.com")
    org = await create_org(client, owner_token, "Grantee Type")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await client.post("/api/v1/numbers", json={"e164": C}, headers=h_owner)
    dept = await client.post("/api/v1/departments", json={"name": "Ghost"}, headers=h_owner)
    dept_id = uuid.UUID(dept.json()["id"])

    set_org_context(session, org_id)
    number = (await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == C))).scalar_one()
    inbox = (await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))).scalar_one()
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="user",  # NOT a department grant
            grantee_id=dept_id,   # ...but the id happens to be a department id
            role="member",
        )
    )
    await session.commit()

    assert await contact_visibility.department_for_inbox_number(session, C) is None


# ==================================================================================
# 4. Inbound auto-create through the real webhook path
# ==================================================================================
async def test_inbound_webhook_autocreate_stamps_department_and_leaves_owner_null(
    app_with_carrier, session
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "inb-dept@example.com", "Inb Dept", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    dept = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h)
    dept_id = uuid.UUID(dept.json()["id"])

    set_org_context(session, org_id)
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == OUR))
    ).scalar_one()
    inbox = (
        await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))
    ).scalar_one()
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
    await session.commit()

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text

    contacts = await _unscoped(session, Contact)
    assert len(contacts) == 1
    assert contacts[0].display_name == THEIRS
    assert contacts[0].department_id == dept_id
    assert contacts[0].owner_user_id is None


async def test_inbound_webhook_autocreate_takes_owner_from_thread_assignee(
    app_with_carrier, session
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "inb-own@example.com", "Inb Own", OUR)
    org_id = uuid.UUID(org["id"])
    assignee = await users_repo.get_by_email(session, "inb-own@example.com")

    # A thread for this pair already exists and is assigned to a human, with no contact
    # linked yet - the state the ownership stamp is meant to pick up.
    set_org_context(session, org_id)
    session.add(
        MessageThread(
            id=uuid.uuid4(),
            org_id=org_id,
            our_e164=OUR,
            contact_e164=THEIRS,
            contact_id=None,
            status="open",
            assigned_user_id=assignee.id,
        )
    )
    await session.commit()

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text

    contacts = await _unscoped(session, Contact)
    assert len(contacts) == 1
    assert contacts[0].owner_user_id == assignee.id


# ==================================================================================
# 5. Surviving-mutation fills: visibility predicate clauses
# ==================================================================================
async def test_department_policy_owner_match_wins_over_department(client, session):
    """Kills: dropping `owner_user_id == me` from the `department` branch. Alice owns a
    contact that was reassigned to a team she is not in - she must keep seeing it."""
    owner_token = await register_and_login(client, "dp-own@example.com")
    org = await create_org(client, owner_token, "Dept Owner Clause")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, "department")

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])
    support_id = uuid.UUID(support.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "dp.alice@example.com")
    await _join_dept(session, org_id, sales_id, alice.id)

    contact = await create_contact(client, alice_token, org["id"], "Alice Owned", [B])
    cid = contact["id"]
    moved = await client.patch(
        f"/api/v1/contacts/{cid}/owner",
        json={"department_id": str(support_id)},
        headers=h_owner,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["owner_user_id"] == str(alice.id)
    assert moved.json()["department_id"] == str(support_id)

    h_alice = auth_headers(alice_token, org["id"])
    got = await client.get(f"/api/v1/contacts/{cid}", headers=h_alice)
    assert got.status_code == 200, got.text
    assert cid in {c["id"] for c in (await client.get("/api/v1/contacts", headers=h_alice)).json()}


async def test_owner_policy_owner_match_survives_department_move(client, session):
    """Same clause, `owner` branch."""
    owner_token = await register_and_login(client, "op-own@example.com")
    org = await create_org(client, owner_token, "Owner Owner Clause")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, "owner")

    support = await client.post("/api/v1/departments", json={"name": "Support"}, headers=h_owner)
    support_id = uuid.UUID(support.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "op.alice@example.com")
    contact = await create_contact(client, alice_token, org["id"], "Alice Owned 2", [B])
    cid = contact["id"]
    await client.patch(
        f"/api/v1/contacts/{cid}/owner",
        json={"department_id": str(support_id)},
        headers=h_owner,
    )
    got = await client.get(f"/api/v1/contacts/{cid}", headers=auth_headers(alice_token, org["id"]))
    assert got.status_code == 200, got.text
    assert got.json()["owner_user_id"] == str(alice.id)


async def _unowned_fallback_case(client, session, policy, email_prefix):
    owner_token = await register_and_login(client, f"{email_prefix}-own@example.com")
    org = await create_org(client, owner_token, f"Unowned {email_prefix}")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, policy)

    # A genuinely unowned, unteamed contact: the org owner has no department, so
    # default_ownership_for_creator gives department NULL; then clear the owner.
    contact = await create_contact(client, owner_token, org["id"], "Nobody's Contact", [B])
    cid = contact["id"]
    cleared = await client.patch(
        f"/api/v1/contacts/{cid}/owner",
        json={"owner_user_id": None, "department_id": None},
        headers=h_owner,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["owner_user_id"] is None
    assert cleared.json()["department_id"] is None

    writer_role = await _make_role(
        client, h_owner, "Writer", ["contacts:read", "contacts:write"]
    )
    reader_role = await _make_role(client, h_owner, "Reader", ["contacts:read"])
    writer_token, _ = await _register_member(
        client, session, org_id, f"{email_prefix}.writer@example.com", role_id=writer_role
    )
    reader_token, _ = await _register_member(
        client, session, org_id, f"{email_prefix}.reader@example.com", role_id=reader_role
    )
    return org, cid, writer_token, reader_token


async def test_unowned_fallback_is_gated_on_contacts_write_department_policy(client, session):
    """Kills: making the unowned+unteamed clause unconditional. A read-only role must
    NOT inherit the writer escape hatch."""
    org, cid, writer_token, reader_token = await _unowned_fallback_case(
        client, session, "department", "unw-dept"
    )
    w = await client.get(f"/api/v1/contacts/{cid}", headers=auth_headers(writer_token, org["id"]))
    assert w.status_code == 200, w.text
    r = await client.get(f"/api/v1/contacts/{cid}", headers=auth_headers(reader_token, org["id"]))
    assert r.status_code == 404, r.text
    assert (await client.get("/api/v1/contacts", headers=auth_headers(reader_token, org["id"]))).json() == []


async def test_unowned_fallback_is_gated_on_contacts_write_owner_policy(client, session):
    """Same, `owner` branch - which had NO coverage of the fallback at all."""
    org, cid, writer_token, reader_token = await _unowned_fallback_case(
        client, session, "owner", "unw-own"
    )
    w = await client.get(f"/api/v1/contacts/{cid}", headers=auth_headers(writer_token, org["id"]))
    assert w.status_code == 200, w.text
    r = await client.get(f"/api/v1/contacts/{cid}", headers=auth_headers(reader_token, org["id"]))
    assert r.status_code == 404, r.text


async def test_inactive_department_membership_grants_nothing(client, session):
    """Kills: dropping `Department.is_active` from resolve_scope. Deactivating a team
    must revoke the reach it conferred."""
    owner_token = await register_and_login(client, "inact-own@example.com")
    org = await create_org(client, owner_token, "Inactive Dept")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, "department")

    sales = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    sales_id = uuid.UUID(sales.json()["id"])

    alice_token, alice = await _register_member(client, session, org_id, "inact.alice@example.com")
    bob_token, bob = await _register_member(client, session, org_id, "inact.bob@example.com")
    await _join_dept(session, org_id, sales_id, alice.id)
    await _join_dept(session, org_id, sales_id, bob.id)

    contact = await create_contact(client, alice_token, org["id"], "Sales Contact", [B])
    cid = contact["id"]
    h_bob = auth_headers(bob_token, org["id"])
    assert (await client.get(f"/api/v1/contacts/{cid}", headers=h_bob)).status_code == 200

    off = await client.patch(
        f"/api/v1/departments/{sales_id}", json={"is_active": False}, headers=h_owner
    )
    assert off.status_code == 200, off.text
    session.expire_all()
    assert (await client.get(f"/api/v1/contacts/{cid}", headers=h_bob)).status_code == 404


async def test_api_key_sees_all_contacts_under_owner_policy(client, session):
    """PINNED DECISION (Fable, P22): an API-key caller has no user identity, so the
    ownership policy cannot apply to it - integrations always see every contact. If a
    future change narrows this, it must be a deliberate one that edits this test."""
    owner_token = await register_and_login(client, "apikey-vis@example.com")
    org = await create_org(client, owner_token, "ApiKey Vis")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await _set_policy(client, h_owner, "owner")

    alice_token, alice = await _register_member(client, session, org_id, "apikey.alice@example.com")
    contact = await create_contact(client, alice_token, org["id"], "Alice Private", [B])
    cid = contact["id"]

    key = await client.post(
        "/api/v1/api-keys", json={"name": "integration", "scopes": ["contacts:read"]}, headers=h_owner
    )
    assert key.status_code == 201, key.text
    kh = {"Authorization": f"Bearer {key.json()['key']}"}

    listed = await client.get("/api/v1/contacts", headers=kh)
    assert listed.status_code == 200, listed.text
    assert cid in {c["id"] for c in listed.json()}
    got = await client.get(f"/api/v1/contacts/{cid}", headers=kh)
    assert got.status_code == 200, got.text
    assert got.json()["owner_user_id"] == str(alice.id)


# ==================================================================================
# 6. Surviving-mutation fills: role guards
# ==================================================================================
async def test_delete_system_role_409(client, session):
    """Only the PATCH half of system-role immutability was covered."""
    token = await register_and_login(client, "sysdel@example.com")
    org = await create_org(client, token, "Sys Del")
    h = auth_headers(token, org["id"])
    roles = (await client.get("/api/v1/roles", headers=h)).json()
    agent_role = next(r for r in roles if r["name"] == "agent")
    assert agent_role["is_system"] is True
    r = await client.delete(f"/api/v1/roles/{agent_role['id']}", headers=h)
    assert r.status_code == 409, r.text
    assert agent_role["id"] in {x["id"] for x in (await client.get("/api/v1/roles", headers=h)).json()}


async def test_patch_role_enforces_escalation_and_validation(client, session):
    """The escalation guard and validate_permissions were only ever exercised on
    POST /roles - PATCH went straight through in every shipped test."""
    owner_token = await register_and_login(client, "esc-owner@example.com")
    org = await create_org(client, owner_token, "Escalation Patch")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    # A limited manager: may edit roles, holds contacts:read but NOT calls:supervise.
    manager_role = await _make_role(
        client, h_owner, "Limited Manager", ["roles:read", "roles:write", "contacts:read"]
    )
    target_role = await _make_role(client, h_owner, "Target", ["contacts:read"])
    mgr_token, _ = await _register_member(
        client, session, org_id, "esc.mgr@example.com", role_id=manager_role
    )
    h_mgr = auth_headers(mgr_token, org["id"])

    escalate = await client.patch(
        f"/api/v1/roles/{target_role}",
        json={"permissions": ["contacts:read", "calls:supervise"]},
        headers=h_mgr,
    )
    assert escalate.status_code == 403, escalate.text

    wildcard = await client.patch(
        f"/api/v1/roles/{target_role}", json={"permissions": ["*"]}, headers=h_owner
    )
    assert wildcard.status_code == 422, wildcard.text

    billing = await client.patch(
        f"/api/v1/roles/{target_role}", json={"permissions": ["org:billing"]}, headers=h_owner
    )
    assert billing.status_code == 422, billing.text

    unknown = await client.patch(
        f"/api/v1/roles/{target_role}", json={"permissions": ["not:a:real:perm"]}, headers=h_owner
    )
    assert unknown.status_code == 422, unknown.text

    # Nothing stuck.
    after = (await client.get("/api/v1/roles", headers=h_owner)).json()
    target = next(r for r in after if r["id"] == str(target_role))
    assert target["permissions"] == ["contacts:read"]


async def test_unknown_role_id_404(client, session):
    token = await register_and_login(client, "role404@example.com")
    org = await create_org(client, token, "Role 404")
    h = auth_headers(token, org["id"])
    ghost = uuid.uuid4()
    assert (await client.patch(f"/api/v1/roles/{ghost}", json={"name": "x"}, headers=h)).status_code == 404
    assert (await client.delete(f"/api/v1/roles/{ghost}", headers=h)).status_code == 404
    cloned = await client.post(
        "/api/v1/roles", json={"name": "y", "clone_from": str(ghost)}, headers=h
    )
    assert cloned.status_code == 404, cloned.text


# ==================================================================================
# 7. Remaining guards: policy validation, bulk cap, import assignment inputs
# ==================================================================================
async def test_invalid_contact_visibility_value_is_rejected(client, session):
    """Kills: dropping the POLICIES check in orgs.py. An unrecognised value would be
    persisted and then silently degrade to `everyone` in resolve_scope - a policy that
    LOOKS set in the UI but enforces nothing."""
    token = await register_and_login(client, "badpolicy@example.com")
    org = await create_org(client, token, "Bad Policy")
    h = auth_headers(token, org["id"])
    bad = await client.patch(
        "/api/v1/orgs/current/settings", json={"contact_visibility": "nobody"}, headers=h
    )
    assert bad.status_code == 422, bad.text
    current = await client.get("/api/v1/orgs/current/settings", headers=h)
    assert current.json()["contact_visibility"] == "everyone"


async def test_bulk_assign_cap_counts_raw_ids_not_deduped(client, session):
    """Kills: removing the FIRST 500 cap in bulk_assign_contacts. The second cap runs
    after de-duplication, so 600 mostly-duplicate ids slip past it."""
    token = await register_and_login(client, "bulkcap@example.com")
    org = await create_org(client, token, "Bulk Cap")
    h = auth_headers(token, org["id"])
    ids = [str(uuid.uuid4()) for _ in range(10)] * 60  # 600 raw, 10 distinct
    r = await client.post("/api/v1/contacts/bulk/assign", json={"contact_ids": ids}, headers=h)
    assert r.status_code == 422, f"{r.status_code} {r.text[:200]}"


async def test_import_assign_all_to_accepts_a_foreign_org_user(client, session):
    """B2 closed (Fable, 2026-09-10): run_import ignores an `assign_all_to` that is not a
    member of this org (and commit_list refuses it with 422 / needs contacts:assign), so a
    foreign user id can never become a contact owner - matching PATCH /contacts/{id}/owner
    and /contacts/bulk/assign."""
    from app.db.session import get_sessionmaker
    from app.models import ContactList
    from app.services import list_import

    a_token = await register_and_login(client, "imp-a@example.com")
    org_a = await create_org(client, a_token, "Import Org A")
    org_a_id = uuid.UUID(org_a["id"])

    b_token = await register_and_login(client, "imp-b@example.com")
    org_b = await create_org(client, b_token, "Import Org B")
    outsider = await users_repo.get_by_email(session, "imp-b@example.com")
    assert org_b["id"] != org_a["id"]

    set_org_context(session, org_a_id)
    list_id = uuid.uuid4()
    session.add(
        ContactList(
            id=list_id,
            org_id=org_a_id,
            name="foreign-owner.csv",
            status="importing",
            total_rows=0,
            accepted_count=0,
            invalid_count=0,
            duplicate_count=0,
            dnc_count=0,
        )
    )
    await session.commit()

    await list_import.run_import(
        get_sessionmaker(),
        list_id=list_id,
        org_id=org_a_id,
        filename="foreign-owner.csv",
        data=b"Name,Phone\nX,+12135550990\n",
        mapping={"phone": "Phone"},
        assign_all_to=outsider.id,
    )

    async with get_sessionmaker()() as s:
        set_org_context(s, org_a_id)
        contact = (
            await s.execute(
                sa.select(Contact)
                .join(ContactPhone, ContactPhone.contact_id == Contact.id)
                .where(ContactPhone.e164 == "+12135550990")
            )
        ).scalar_one()
        assert contact.owner_user_id is None  # foreign id ignored, contact stays unowned

    # The equivalent request through the assign API is refused, which is the contrast
    # that makes the gap a real inconsistency rather than a design choice.
    h_a = auth_headers(a_token, org_a["id"])
    refused = await client.post(
        "/api/v1/contacts/bulk/assign",
        json={"contact_ids": [str(contact.id)], "owner_user_id": str(outsider.id)},
        headers=h_a,
    )
    assert refused.status_code == 422, refused.text


async def test_inbound_does_not_adopt_a_pre_existing_unowned_contact(app_with_carrier, session):
    """Opus N1: the ownership stamp applies to contacts the inbound just CREATED. A contact
    that already existed (here: created by a human, then left with no team) keeps
    department_id NULL after an inbound on an inbox that has a department grant."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "inb-keep@example.com", "Inb Keep", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    created = await client.post(
        "/api/v1/contacts",
        json={"display_name": "Existing", "phones": [{"e164": THEIRS}]},
        headers=h,
    )
    assert created.status_code == 201, created.text
    contact_id = uuid.UUID(created.json()["id"])

    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h)
    dept_id = uuid.UUID(dept.json()["id"])
    set_org_context(session, org_id)
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == OUR))
    ).scalar_one()
    inbox = (
        await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))
    ).scalar_one()
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
    await session.commit()

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    contact = await session.get(Contact, contact_id)
    assert contact is not None
    assert contact.department_id is None


async def test_patch_role_keeps_existing_non_grantable_permissions(client, session):
    """B1 (fixed by Fable 2026-09-10): a limited role-manager saving a role that already
    holds a permission they lack (the UI always sends the full list) must succeed - keeping
    a permission is not granting it. Adding one they lack is still refused."""
    owner_token = await register_and_login(client, "b1-owner@example.com")
    org = await create_org(client, owner_token, "B1 Keep")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    manager_role = await _make_role(
        client, h_owner, "Limited Manager", ["roles:read", "roles:write", "contacts:read"]
    )
    target_role = await _make_role(
        client, h_owner, "Target", ["contacts:read", "calls:supervise"]
    )
    mgr_token, _ = await _register_member(
        client, session, org_id, "b1.mgr@example.com", role_id=manager_role
    )
    h_mgr = auth_headers(mgr_token, org["id"])

    keep = await client.patch(
        f"/api/v1/roles/{target_role}",
        json={"name": "Renamed", "permissions": ["contacts:read", "calls:supervise"]},
        headers=h_mgr,
    )
    assert keep.status_code == 200, keep.text
    assert keep.json()["name"] == "Renamed"
    assert sorted(keep.json()["permissions"]) == ["calls:supervise", "contacts:read"]

    add = await client.patch(
        f"/api/v1/roles/{target_role}",
        json={"permissions": ["contacts:read", "calls:supervise", "calls:place"]},
        headers=h_mgr,
    )
    assert add.status_code == 403, add.text
