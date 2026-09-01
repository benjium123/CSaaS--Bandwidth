from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Inbox, InboxGrant, OrgMembership, OrgNumber, Role
from app.repositories import users as users_repo
from tests.conftest import (
    auth_headers,
    fixture_bytes,
    make_org_with_number,
    register_and_login,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
THEIRS = "+19725550199"


async def _inbound(client) -> None:
    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200


async def _first_thread(client, h) -> dict:
    body = (await client.get("/api/v1/inbox/threads", headers=h)).json()
    assert body["items"], "expected at least one thread"
    return body["items"][0]


async def test_assign_close_read(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "ts1@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    await _inbound(client)

    item = await _first_thread(client, h)
    thread_id = item["thread"]["id"]
    assert item["unread"] == 1
    assert item["thread"]["status"] == "open"

    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assigned = await client.patch(
        f"/api/v1/threads/{thread_id}", json={"assigned_user_id": me["id"]}, headers=h
    )
    assert assigned.status_code == 200
    assert assigned.json()["assigned_user_id"] == me["id"]

    # A non-member cannot be assigned.
    stranger = await client.patch(
        f"/api/v1/threads/{thread_id}",
        json={"assigned_user_id": str(uuid.uuid4())},
        headers=h,
    )
    assert stranger.status_code == 422

    read = await client.post(f"/api/v1/threads/{thread_id}/read", headers=h)
    assert read.status_code == 204
    assert (await _first_thread(client, h))["unread"] == 0

    closed = await client.patch(
        f"/api/v1/threads/{thread_id}", json={"status": "closed"}, headers=h
    )
    assert closed.status_code == 200
    assert (await _first_thread(client, h))["thread"]["status"] == "closed"


async def test_new_inbound_reopens_and_unreads(app_with_carrier):
    """A closed conversation that gets a reply is not closed any more."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "ts2@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await _inbound(client)
    item = await _first_thread(client, h)
    thread_id = item["thread"]["id"]
    await client.post(f"/api/v1/threads/{thread_id}/read", headers=h)
    await client.patch(f"/api/v1/threads/{thread_id}", json={"status": "closed"}, headers=h)
    assert (await _first_thread(client, h))["unread"] == 0

    # A DIFFERENT inbound message on the same thread.
    import json

    payload = json.loads(fixture_bytes("message-received.json"))
    payload[0]["message"]["id"] = "1755000000000-inbound-second"
    payload[0]["message"]["text"] = "are you there?"
    r = await client.post(
        HOOK, content=json.dumps(payload).encode(), headers=webhook_auth_headers()
    )
    assert r.status_code == 200

    after = await _first_thread(client, h)
    assert after["thread"]["status"] == "open", "an inbound reply must reopen the thread"
    assert after["unread"] == 1


async def test_reopen_is_replay_safe(app_with_carrier):
    """Delivering the SAME webhook twice must change nothing the second time."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "ts3@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await _inbound(client)
    item = await _first_thread(client, h)
    thread_id = item["thread"]["id"]
    await client.patch(f"/api/v1/threads/{thread_id}", json={"status": "closed"}, headers=h)

    # Replay the identical payload - it is a duplicate, so it must NOT reopen.
    await _inbound(client)
    after = await _first_thread(client, h)
    assert after["thread"]["status"] == "closed", "a replayed duplicate must not reopen"
    msgs = (await client.get("/api/v1/messages", headers=h)).json()
    assert len(msgs) == 1


async def test_labels_replace_set(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "ts4@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    await _inbound(client)
    thread_id = (await _first_thread(client, h))["thread"]["id"]

    from tests.conftest import create_tag

    a = await create_tag(client, token, org["id"], "hot")
    b = await create_tag(client, token, org["id"], "cold")

    await client.put(
        f"/api/v1/threads/{thread_id}/labels", json={"tag_ids": [a["id"], b["id"]]}, headers=h
    )
    assert len((await _first_thread(client, h))["labels"]) == 2

    # PUT replaces, it does not append.
    await client.put(
        f"/api/v1/threads/{thread_id}/labels", json={"tag_ids": [a["id"]]}, headers=h
    )
    labels = (await _first_thread(client, h))["labels"]
    assert [lbl["name"] for lbl in labels] == ["hot"]

    bad = await client.put(
        f"/api/v1/threads/{thread_id}/labels",
        json={"tag_ids": [str(uuid.uuid4())]},
        headers=h,
    )
    assert bad.status_code == 422


async def test_inbox_manage_permission(app_with_carrier, session):
    client, _, _ = app_with_carrier
    owner_token, org, _ = await make_org_with_number(client, "ts5@example.com", "Org A", OUR)
    owner_h = auth_headers(owner_token, org["id"])
    await _inbound(client)
    thread_id = (await _first_thread(client, owner_h))["thread"]["id"]

    agent_token = await register_and_login(client, "ts6@example.com")
    agent_user = await users_repo.get_by_email(session, "ts6@example.com")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    agent_role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=agent_user.id, role_id=agent_role.id
        )
    )
    await session.commit()

    agent_h = auth_headers(agent_token, org["id"])

    # P15: inbox:manage alone is no longer enough - fail-closed with no grant on OUR's
    # inbox. An inaccessible thread's mutation is a 404, not a 403 (existence not leaked).
    no_grant = await client.patch(
        f"/api/v1/threads/{thread_id}", json={"status": "closed"}, headers=agent_h
    )
    assert no_grant.status_code == 404

    inbox = (
        await session.execute(
            sa.select(Inbox)
            .join(OrgNumber, OrgNumber.id == Inbox.number_id)
            .where(OrgNumber.e164 == OUR)
        )
    ).scalar_one()
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="user",
            grantee_id=agent_user.id,
            role="member",
        )
    )
    await session.commit()

    # Agents DO get inbox:manage in P2 - they work the inbox - PLUS (P15) a grant on it.
    ok = await client.patch(
        f"/api/v1/threads/{thread_id}", json={"status": "closed"}, headers=agent_h
    )
    assert ok.status_code == 200

    # Strip the permission and it must be refused - RBAC still gates on top of the grant.
    agent_role.permissions = ["inbox:read", "inbox:send"]
    await session.commit()
    denied = await client.patch(
        f"/api/v1/threads/{thread_id}", json={"status": "open"}, headers=agent_h
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


async def test_thread_tenancy(app_with_carrier):
    client, _, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "ts7@example.com", "Org A", OUR)
    ha = auth_headers(token_a, org_a["id"])
    await _inbound(client)
    thread_id = (await _first_thread(client, ha))["thread"]["id"]

    token_b, org_b, _ = await make_org_with_number(
        client, "ts8@example.com", "Org B", "+12145550111"
    )
    hb = auth_headers(token_b, org_b["id"])
    assert (
        await client.patch(f"/api/v1/threads/{thread_id}", json={"status": "closed"}, headers=hb)
    ).status_code == 404
    assert (await client.post(f"/api/v1/threads/{thread_id}/read", headers=hb)).status_code == 404
