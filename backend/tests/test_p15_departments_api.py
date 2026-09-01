"""P15: departments + tiered inbox access, exercised through the real HTTP API.

Unit coverage for the resolver itself lives in test_p15_inbox_access.py; this file proves
the routes wire that resolver in correctly - RBAC on the departments/inboxes admin
surface, and the fan-out gates on threads/messages/calls.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import Inbox, OrgMembership, OrgNumber, Role
from app.repositories import users as users_repo
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    create_org,
    fixture_bytes,
    make_settings,
    register_and_login,
    webhook_auth_headers,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

A = "+12145550100"
B = "+12145550111"
PASSWORD = "correct-horse-battery"
MESSAGING_HOOK = "/api/v1/webhooks/bandwidth/messaging"


async def _inbound(client) -> None:
    r = await client.post(
        MESSAGING_HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200


async def _register_member(client, session, org_id: uuid.UUID, email, role_name="agent") -> str:
    """Register a user and attach them to the org with an existing system role, directly
    through the ORM.

    NOT the ``/orgs/current/invites`` endpoint: conftest's default ``allow_open_
    registration=True`` makes every registration take the bootstrap branch (see
    app/api/routes/auth.py::register), which never redeems an invite_token at all - see
    tests/test_invites.py's ``settings`` fixture docstring for the same trap. Mirrors the
    direct-membership pattern in tests/test_rbac.py::test_agent_denied_members_read.
    """
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = (await session.execute(sa.select(Role).where(Role.name == role_name))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def _inbox_id_for(client, headers, e164) -> str:
    r = await client.get("/api/v1/inboxes", headers=headers)
    assert r.status_code == 200, r.text
    row = next(i for i in r.json() if i["e164"] == e164)
    return row["id"]


# ----------------------------------------------------------------------------------
# Departments RBAC + CRUD
# ----------------------------------------------------------------------------------
async def test_agent_denied_department_read_admin_allowed(client, session):
    owner_token = await register_and_login(client, "d1@example.com")
    org = await create_org(client, owner_token, "Org D1")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])

    agent_token = await _register_member(client, session, org_id, "agentd1@example.com")
    h_agent = auth_headers(agent_token, org["id"])

    denied = await client.get("/api/v1/departments", headers=h_agent)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"

    created = await client.post(
        "/api/v1/departments", json={"name": "Sales"}, headers=h_owner
    )
    assert created.status_code == 201, created.text
    dept_id = created.json()["id"]
    assert created.json()["is_active"] is True
    assert created.json()["member_user_ids"] == []

    listed = await client.get("/api/v1/departments", headers=h_owner)
    assert listed.status_code == 200
    assert any(d["id"] == dept_id for d in listed.json())

    patched = await client.patch(
        f"/api/v1/departments/{dept_id}", json={"name": "Sales EU"}, headers=h_owner
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Sales EU"

    deleted = await client.delete(f"/api/v1/departments/{dept_id}", headers=h_owner)
    assert deleted.status_code == 204

    gone = await client.get("/api/v1/departments", headers=h_owner)
    assert all(d["id"] != dept_id for d in gone.json())


# ----------------------------------------------------------------------------------
# Number create -> inbox auto-created
# ----------------------------------------------------------------------------------
async def test_number_create_auto_creates_inbox(client, session):
    token = await register_and_login(client, "d2@example.com")
    org = await create_org(client, token, "Org D2")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/numbers", json={"e164": A}, headers=h)
    assert r.status_code == 201, r.text

    set_org_context(session, org_id)
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == A))
    ).scalar_one()
    inbox = (
        await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))
    ).scalar_one()
    assert inbox.name == A

    listed = await client.get("/api/v1/inboxes", headers=h)
    assert listed.status_code == 200
    row = next(i for i in listed.json() if i["e164"] == A)
    assert row["my_role"] == "admin"  # owner is wildcard-admin


# ----------------------------------------------------------------------------------
# Department members + grants + delete cascade
# ----------------------------------------------------------------------------------
async def test_department_members_grants_and_delete_revokes_access(client, session):
    owner_token = await register_and_login(client, "d3@example.com")
    org = await create_org(client, owner_token, "Org D3")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, org["id"])
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)

    agent_token = await _register_member(client, session, org_id, "agentd3@example.com")
    h_agent = auth_headers(agent_token, org["id"])

    set_org_context(session, org_id)
    agent_user = await users_repo.get_by_email(session, "agentd3@example.com")

    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h_owner)
    assert dept.status_code == 201, dept.text
    dept_id = dept.json()["id"]

    members = await client.put(
        f"/api/v1/departments/{dept_id}/members",
        json={"user_ids": [str(agent_user.id)]},
        headers=h_owner,
    )
    assert members.status_code == 200, members.text
    assert members.json()["member_user_ids"] == [str(agent_user.id)]

    inbox_id = await _inbox_id_for(client, h_owner, A)
    grant = await client.put(
        f"/api/v1/inboxes/{inbox_id}/grants",
        json={"grants": [{"grantee_type": "department", "grantee_id": dept_id, "role": "member"}]},
        headers=h_owner,
    )
    assert grant.status_code == 200, grant.text

    listed = await client.get("/api/v1/inboxes", headers=h_agent)
    assert listed.status_code == 200
    assert any(i["e164"] == A and i["my_role"] == "member" for i in listed.json())

    # Remove the agent from the department -> access revoked immediately.
    emptied = await client.put(
        f"/api/v1/departments/{dept_id}/members", json={"user_ids": []}, headers=h_owner
    )
    assert emptied.status_code == 200
    assert emptied.json()["member_user_ids"] == []

    listed_after = await client.get("/api/v1/inboxes", headers=h_agent)
    assert all(i["e164"] != A for i in listed_after.json())

    # Re-grant via the department, then delete the department outright - the grant must
    # go with it, even though there is deliberately no FK on grantee_id for this.
    regrant = await client.put(
        f"/api/v1/inboxes/{inbox_id}/grants",
        json={"grants": [{"grantee_type": "department", "grantee_id": dept_id, "role": "member"}]},
        headers=h_owner,
    )
    assert regrant.status_code == 200, regrant.text

    deleted = await client.delete(f"/api/v1/departments/{dept_id}", headers=h_owner)
    assert deleted.status_code == 204

    grants_after = await client.get(f"/api/v1/inboxes/{inbox_id}/grants", headers=h_owner)
    assert grants_after.status_code == 200
    assert grants_after.json() == []


# ----------------------------------------------------------------------------------
# Messages: thread filter, 404-not-403 detail, send guard
# ----------------------------------------------------------------------------------
async def test_thread_filter_detail_404_and_send_guard(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    owner_token = await register_and_login(client, "d4@example.com")
    org = await create_org(client, owner_token, "Org D4")
    org_id_str = org["id"]
    org_id = uuid.UUID(org_id_str)
    h_owner = auth_headers(owner_token, org_id_str)
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    await client.post("/api/v1/numbers", json={"e164": B}, headers=h_owner)

    agent_token = await _register_member(client, session, org_id, "agentd4@example.com")
    h_agent = auth_headers(agent_token, org_id_str)

    # No grant yet: send from A is refused, no carrier call happens.
    denied_send = await client.post(
        "/api/v1/messages", json={"to": "+19725550111", "body": "hi", "from": A}, headers=h_agent
    )
    assert denied_send.status_code == 403
    assert denied_send.json()["error"]["code"] == "permission_denied"
    assert fake.sent == []

    set_org_context(session, org_id)
    agent_user = await users_repo.get_by_email(session, "agentd4@example.com")
    inbox_a_id = await _inbox_id_for(client, h_owner, A)
    grant_a = await client.put(
        f"/api/v1/inboxes/{inbox_a_id}/grants",
        json={"grants": [{"grantee_type": "user", "grantee_id": str(agent_user.id), "role": "member"}]},
        headers=h_owner,
    )
    assert grant_a.status_code == 200, grant_a.text

    ok_send = await client.post(
        "/api/v1/messages", json={"to": "+19725550111", "body": "hi", "from": A}, headers=h_agent
    )
    assert ok_send.status_code == 201, ok_send.text
    thread_id = ok_send.json()["thread_id"]

    owner_send = await client.post(
        "/api/v1/messages", json={"to": "+19725550122", "body": "hi", "from": B}, headers=h_owner
    )
    assert owner_send.status_code == 201, owner_send.text
    other_thread_id = owner_send.json()["thread_id"]

    listed = await client.get("/api/v1/threads", headers=h_agent)
    assert listed.status_code == 200
    ids = {t["id"] for t in listed.json()}
    assert thread_id in ids
    assert other_thread_id not in ids

    # An ungranted thread's detail is a 404, not a 403 - existence is not leaked.
    detail_denied = await client.get(f"/api/v1/threads/{other_thread_id}/ai", headers=h_agent)
    assert detail_denied.status_code == 404

    detail_ok = await client.get(f"/api/v1/threads/{thread_id}/ai", headers=h_agent)
    assert detail_ok.status_code == 200

    # Viewer-only on B: can read, cannot send.
    inbox_b_id = await _inbox_id_for(client, h_owner, B)
    grant_b = await client.put(
        f"/api/v1/inboxes/{inbox_b_id}/grants",
        json={"grants": [{"grantee_type": "user", "grantee_id": str(agent_user.id), "role": "viewer"}]},
        headers=h_owner,
    )
    assert grant_b.status_code == 200, grant_b.text

    listed_after = await client.get("/api/v1/threads", headers=h_agent)
    ids_after = {t["id"] for t in listed_after.json()}
    assert other_thread_id in ids_after  # viewer can now SEE it...

    viewer_send = await client.post(
        "/api/v1/messages", json={"to": "+19725550122", "body": "again", "from": B}, headers=h_agent
    )
    assert viewer_send.status_code == 403  # ...but not send from it.


# ----------------------------------------------------------------------------------
# Calls: list filter + place-call guard
# ----------------------------------------------------------------------------------
@pytest.fixture
async def app_with_voice_carrier(engine):
    """Local copy of the fixture from test_voice_api.py (same reason: avoid ruff mistaking
    the parameter for shadowing a module-level import if it were imported instead)."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


async def test_call_list_filter_and_place_guard(app_with_voice_carrier, session):
    client, fake, _app = app_with_voice_carrier
    owner_token = await register_and_login(client, "d5@example.com")
    org = await create_org(client, owner_token, "Org D5")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, str(org_id))
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    await client.post("/api/v1/numbers", json={"e164": B}, headers=h_owner)

    # A bespoke role with calls:place/calls:read but no inboxes:admin - the system "agent"
    # role has neither permission, and "admin" carries inboxes:admin (which would bypass
    # the exact gate this test is proving).
    caller_token = await register_and_login(client, "callerd5@example.com")
    caller_user = await users_repo.get_by_email(session, "callerd5@example.com")

    set_org_context(session, org_id)
    caller_role = Role(
        id=uuid.uuid4(), org_id=org_id, name="caller", permissions=["calls:place", "calls:read"]
    )
    session.add(caller_role)
    await session.flush()
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=caller_user.id, role_id=caller_role.id
        )
    )
    await session.commit()

    h_caller = auth_headers(caller_token, str(org_id))

    denied = await client.post(
        "/api/v1/calls", json={"to": "+19725550111", "from": A}, headers=h_caller
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"
    assert fake.create_calls == []

    inbox_a_id = await _inbox_id_for(client, h_owner, A)
    grant = await client.put(
        f"/api/v1/inboxes/{inbox_a_id}/grants",
        json={"grants": [{"grantee_type": "user", "grantee_id": str(caller_user.id), "role": "member"}]},
        headers=h_owner,
    )
    assert grant.status_code == 200, grant.text

    ok = await client.post(
        "/api/v1/calls", json={"to": "+19725550111", "from": A}, headers=h_caller
    )
    assert ok.status_code == 201, ok.text

    owner_call = await client.post(
        "/api/v1/calls", json={"to": "+19725550122", "from": B}, headers=h_owner
    )
    assert owner_call.status_code == 201, owner_call.text

    listed = await client.get("/api/v1/calls", headers=h_caller)
    assert listed.status_code == 200
    e164s = {c["our_e164"] for c in listed.json()}
    assert A in e164s
    assert B not in e164s

    # An ungranted call's detail is a 404, not a 403 - don't leak existence.
    owner_call_id = owner_call.json()["id"]
    denied_detail = await client.get(f"/api/v1/calls/{owner_call_id}", headers=h_caller)
    assert denied_detail.status_code == 404

    granted_detail = await client.get(f"/api/v1/calls/{owner_call_id}", headers=h_owner)
    assert granted_detail.status_code == 200


# ----------------------------------------------------------------------------------
# Messages by-id reads: gated on can_view, same 404-not-403 pattern as thread detail
# ----------------------------------------------------------------------------------
async def test_message_by_id_reads_gated_by_view_access(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    owner_token = await register_and_login(client, "d6@example.com")
    org = await create_org(client, owner_token, "Org D6")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, str(org_id))
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    await client.post("/api/v1/numbers", json={"e164": B}, headers=h_owner)

    agent_token = await _register_member(client, session, org_id, "agentd6@example.com")
    h_agent = auth_headers(agent_token, str(org_id))

    # Owner sends from B - a number the agent has no grant on at all.
    owner_send = await client.post(
        "/api/v1/messages", json={"to": "+19725550133", "body": "hi", "from": B}, headers=h_owner
    )
    assert owner_send.status_code == 201, owner_send.text
    thread_id = owner_send.json()["thread_id"]
    message_id = owner_send.json()["id"]

    # Ungranted: both the by-thread-id list and the by-id single-message read are 404,
    # never a silent empty list or a 403 - existence is not leaked either way.
    denied_list = await client.get(
        "/api/v1/messages", params={"thread_id": thread_id}, headers=h_agent
    )
    assert denied_list.status_code == 404

    denied_single = await client.get(f"/api/v1/messages/{message_id}", headers=h_agent)
    assert denied_single.status_code == 404

    # Grant VIEWER only on B: reads now work, but sending still does not.
    set_org_context(session, org_id)
    agent_user = await users_repo.get_by_email(session, "agentd6@example.com")
    inbox_b_id = await _inbox_id_for(client, h_owner, B)
    grant = await client.put(
        f"/api/v1/inboxes/{inbox_b_id}/grants",
        json={
            "grants": [
                {"grantee_type": "user", "grantee_id": str(agent_user.id), "role": "viewer"}
            ]
        },
        headers=h_owner,
    )
    assert grant.status_code == 200, grant.text

    ok_list = await client.get(
        "/api/v1/messages", params={"thread_id": thread_id}, headers=h_agent
    )
    assert ok_list.status_code == 200
    assert any(m["id"] == message_id for m in ok_list.json())

    ok_single = await client.get(f"/api/v1/messages/{message_id}", headers=h_agent)
    assert ok_single.status_code == 200
    assert ok_single.json()["id"] == message_id

    viewer_send = await client.post(
        "/api/v1/messages", json={"to": "+19725550133", "body": "again", "from": B}, headers=h_agent
    )
    assert viewer_send.status_code == 403


# ----------------------------------------------------------------------------------
# GET /inbox/threads filtering + mark-read gating
# ----------------------------------------------------------------------------------
async def test_inbox_threads_filter_and_mark_read_gating(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    owner_token = await register_and_login(client, "d7@example.com")
    org = await create_org(client, owner_token, "Org D7")
    org_id = uuid.UUID(org["id"])
    h_owner = auth_headers(owner_token, str(org_id))
    await client.post("/api/v1/numbers", json={"e164": A}, headers=h_owner)
    await _inbound(client)

    threads_owner = (await client.get("/api/v1/inbox/threads", headers=h_owner)).json()
    assert threads_owner["items"], "expected at least one thread"
    thread_id = threads_owner["items"][0]["thread"]["id"]

    agent_token = await _register_member(client, session, org_id, "agentd7@example.com")
    h_agent = auth_headers(agent_token, str(org_id))

    # Ungranted: /inbox/threads shows nothing for the agent, and mark-read is a 404 -
    # never a silent empty response, and never a 403 (existence not leaked).
    threads_ungranted = (await client.get("/api/v1/inbox/threads", headers=h_agent)).json()
    assert threads_ungranted["items"] == []

    denied_read = await client.post(f"/api/v1/threads/{thread_id}/read", headers=h_agent)
    assert denied_read.status_code == 404

    # Grant VIEWER access on A: the thread is now listed, and mark-read succeeds - a
    # viewer may read/acknowledge, only sending/managing needs "member".
    set_org_context(session, org_id)
    agent_user = await users_repo.get_by_email(session, "agentd7@example.com")
    inbox_a_id = await _inbox_id_for(client, h_owner, A)
    grant = await client.put(
        f"/api/v1/inboxes/{inbox_a_id}/grants",
        json={
            "grants": [
                {"grantee_type": "user", "grantee_id": str(agent_user.id), "role": "viewer"}
            ]
        },
        headers=h_owner,
    )
    assert grant.status_code == 200, grant.text

    threads_granted = (await client.get("/api/v1/inbox/threads", headers=h_agent)).json()
    ids = {item["thread"]["id"] for item in threads_granted["items"]}
    assert thread_id in ids

    ok_read = await client.post(f"/api/v1/threads/{thread_id}/read", headers=h_agent)
    assert ok_read.status_code == 204
