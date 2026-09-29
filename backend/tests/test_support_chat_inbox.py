"""0095: the signed-in customer support-chat inbox (routes/site.py).

A signed-in customer hands the console chat to a person (POST /api/v1/support/chat); the
anonymous visitor keeps the public handoff (POST /api/v1/public/site-chat/handoff). Platform
operators read and answer both in /api/v1/ops/site/... .
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes import site as site_routes
from app.main import create_app
from app.models import SecurityAlert, User
from app.models import Session as IdentitySession
from app.models.site import SiteChat, SiteChatMessage
from app.services import device_push, fcm
from tests.conftest import auth_headers, create_org, make_settings, register_and_login


@pytest.fixture
def chat_settings():
    return make_settings()


@pytest.fixture
async def client(engine, chat_settings):
    application = create_app(chat_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
async def _operator(client, session, email="ops@example.com", role="admin") -> str:
    """A named platform operator with a second factor and a session that proved it."""
    from app.services import operators as operators_svc

    token = await register_and_login(client, email)
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    user.has_passkey = True
    await operators_svc.grant(session, email=email, role=role)
    live = (
        await session.execute(
            sa.select(IdentitySession)
            .where(IdentitySession.user_id == user.id)
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    live.second_factor_at = datetime.now(timezone.utc)
    await session.commit()
    return token


async def _customer_chat(
    client, email="cust@example.com", org_name="Acme Plumbing", text="I need a person"
):
    """register -> login -> create an org -> hand the console chat to the team."""
    token = await register_and_login(client, email)
    org = await create_org(client, token, org_name)
    r = await client.post(
        "/api/v1/support/chat",
        json={"page": "/app", "transcript": [{"role": "visitor", "text": text}]},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    return r.json(), org


async def _visitor_chat(client, name="Dana", email="dana@example.com"):
    r = await client.post(
        "/api/v1/public/site-chat/handoff",
        json={"name": name, "email": email, "transcript": [{"role": "visitor", "text": "Hello"}]},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _chat_item(client, chat_id, headers) -> dict:
    listing = await client.get("/api/v1/ops/site/chats", headers=headers)
    assert listing.status_code == 200, listing.text
    return next(c for c in listing.json()["chats"] if c["id"] == chat_id)


async def _is_unread(client, chat_id, headers) -> bool:
    return (await _chat_item(client, chat_id, headers))["unread"]


# ----------------------------------------------------------------------------------
# the signed-in customer handoff
# ----------------------------------------------------------------------------------
async def test_signed_in_customer_handoff_records_identity_from_the_session(client, session):
    token = await register_and_login(client, "cust@example.com")
    org = await create_org(client, token, "Acme Plumbing")
    r = await client.post(
        "/api/v1/support/chat",
        json={
            "page": "/app/billing",
            "transcript": [{"role": "visitor", "text": "My card was declined"}],
            # The client does not get to choose who it claims to be: these must be ignored.
            "name": "Not The Customer",
            "email": "someone-else@example.com",
        },
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"chat_id", "token", "staffed"}
    assert isinstance(body["staffed"], bool)
    assert len(body["token"]) > 20

    chat = await session.get(SiteChat, uuid.UUID(body["chat_id"]))
    assert chat is not None
    assert str(chat.org_id) == org["id"]
    assert chat.user_id is not None
    assert chat.reason == "customer"
    # Identity comes from the user row, never the request body.
    assert chat.name == "cust"
    assert chat.email == "cust@example.com"
    assert chat.page == "/app/billing"
    assert chat.last_visitor_at is not None
    assert chat.token_hash != body["token"]

    msgs = (
        await session.execute(sa.select(SiteChatMessage).where(SiteChatMessage.chat_id == chat.id))
    ).scalars().all()
    assert [(m.role, m.text) for m in msgs] == [("visitor", "My card was declined")]

    alert = (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "site_chat_handoff"))
    ).scalar_one()
    assert alert.detail["chat_id"] == body["chat_id"]


async def test_customer_handoff_needs_auth(client):
    r = await client.post("/api/v1/support/chat", json={"transcript": []})
    assert r.status_code in (401, 403), r.text


# ----------------------------------------------------------------------------------
# the anonymous visitor handoff still works, unchanged
# ----------------------------------------------------------------------------------
async def test_public_visitor_handoff_creates_an_orgless_chat(client, session):
    vis = await _visitor_chat(client)
    chat = await session.get(SiteChat, uuid.UUID(vis["chat_id"]))
    assert chat is not None
    assert chat.org_id is None and chat.user_id is None
    assert chat.reason is None

    op = await _operator(client, session)
    item = await _chat_item(client, vis["chat_id"], auth_headers(op))
    assert item["kind"] == "visitor"
    assert item["org_id"] is None
    assert item["org_name"] is None
    assert item["assigned_user_id"] is None
    assert item["unread"] is True


async def test_visitor_message_sets_last_visitor_at(client, session):
    vis = await _visitor_chat(client)
    chat_id, token = vis["chat_id"], vis["token"]

    sent = await client.post(
        f"/api/v1/public/site-chat/{chat_id}/messages", json={"token": token, "text": "Still there?"}
    )
    assert sent.status_code == 201, sent.text

    session.expire_all()
    chat = await session.get(SiteChat, uuid.UUID(chat_id))
    assert chat.last_visitor_at is not None
    msgs = (
        await session.execute(sa.select(SiteChatMessage).where(SiteChatMessage.chat_id == chat.id))
    ).scalars().all()
    assert "Still there?" in [m.text for m in msgs]


# ----------------------------------------------------------------------------------
# ops inbox
# ----------------------------------------------------------------------------------
async def test_ops_list_chats_exposes_kind_org_assignee_and_unread(client, session):
    cust, org = await _customer_chat(client)
    vis = await _visitor_chat(client)
    op = await _operator(client, session)
    h = auth_headers(op)

    listing = await client.get("/api/v1/ops/site/chats", headers=h)
    assert listing.status_code == 200, listing.text
    items = {c["id"]: c for c in listing.json()["chats"]}
    assert cust["chat_id"] in items and vis["chat_id"] in items

    c = items[cust["chat_id"]]
    assert c["kind"] == "customer"
    assert c["org_id"] == org["id"]
    assert c["org_name"] == "Acme Plumbing"
    assert c["assigned_user_id"] is None
    assert c["assigned_name"] is None
    assert c["unread"] is True

    v = items[vis["chat_id"]]
    assert v["kind"] == "visitor"
    assert v["org_id"] is None
    assert v["org_name"] is None
    assert v["unread"] is True


async def test_list_filters_by_kind_status_and_mine(client, session):
    cust, _ = await _customer_chat(client)
    vis = await _visitor_chat(client)
    cust_id, vis_id = cust["chat_id"], vis["chat_id"]
    op = await _operator(client, session)
    h = auth_headers(op)

    customer_only = (
        await client.get("/api/v1/ops/site/chats", params={"kind": "customer"}, headers=h)
    ).json()["chats"]
    assert [c["id"] for c in customer_only] == [cust_id]

    visitor_only = (
        await client.get("/api/v1/ops/site/chats", params={"kind": "visitor"}, headers=h)
    ).json()["chats"]
    assert [c["id"] for c in visitor_only] == [vis_id]

    # An open-only listing drops a closed chat.
    await client.post(f"/api/v1/ops/site/chats/{vis_id}/close", json={}, headers=h)
    open_ids = [
        c["id"]
        for c in (
            await client.get("/api/v1/ops/site/chats", params={"status": "open"}, headers=h)
        ).json()["chats"]
    ]
    assert cust_id in open_ids
    assert vis_id not in open_ids

    # mine=true is only what the caller owns.
    await client.post(f"/api/v1/ops/site/chats/{cust_id}/assign", json={"to_me": True}, headers=h)
    mine = (
        await client.get("/api/v1/ops/site/chats", params={"mine": "true"}, headers=h)
    ).json()["chats"]
    assert [c["id"] for c in mine] == [cust_id]


async def test_read_unread_and_reply_mark_the_flag(client, session):
    cust, _ = await _customer_chat(client)
    chat_id, token = cust["chat_id"], cust["token"]
    op = await _operator(client, session)
    h = auth_headers(op)

    # A brand-new chat starts unread.
    assert await _is_unread(client, chat_id, h) is True

    read = await client.post(f"/api/v1/ops/site/chats/{chat_id}/read", headers=h)
    assert read.status_code == 200, read.text
    assert read.json() == {"unread": False}
    assert await _is_unread(client, chat_id, h) is False

    # A later visitor message makes it unread again.
    sent = await client.post(
        f"/api/v1/public/site-chat/{chat_id}/messages", json={"token": token, "text": "anyone?"}
    )
    assert sent.status_code == 201, sent.text
    assert await _is_unread(client, chat_id, h) is True

    # An operator reply marks it read.
    reply = await client.post(f"/api/v1/ops/site/chats/{chat_id}/reply", json={"text": "Hi!"}, headers=h)
    assert reply.status_code == 201, reply.text
    assert await _is_unread(client, chat_id, h) is False


async def test_unread_endpoint_counts_open_and_unassigned_or_mine(client, session):
    cust, _ = await _customer_chat(client)
    chat_id = cust["chat_id"]

    op1 = await _operator(client, session, email="ops1@example.com")
    h1 = auth_headers(op1)
    body = (await client.get("/api/v1/ops/site/chats/unread", headers=h1)).json()
    ids = [c["id"] for c in body["chats"]]
    assert chat_id in ids
    assert body["count"] == len(body["chats"]) == 1
    entry = next(c for c in body["chats"] if c["id"] == chat_id)
    assert entry["kind"] == "customer"
    assert entry["name"] == "cust"
    assert entry["last_visitor_at"] is not None

    # Assigned to ANOTHER operator -> not counted for op1, counted for op2.
    op2 = await _operator(client, session, email="ops2@example.com")
    h2 = auth_headers(op2)
    assigned = await client.post(
        f"/api/v1/ops/site/chats/{chat_id}/assign", json={"to_me": True}, headers=h2
    )
    assert assigned.status_code == 200, assigned.text
    assert chat_id not in [
        c["id"] for c in (await client.get("/api/v1/ops/site/chats/unread", headers=h1)).json()["chats"]
    ]
    assert chat_id in [
        c["id"] for c in (await client.get("/api/v1/ops/site/chats/unread", headers=h2)).json()["chats"]
    ]

    # A closed chat never counts, even for its assignee.
    closed = await client.post(f"/api/v1/ops/site/chats/{chat_id}/close", json={}, headers=h2)
    assert closed.status_code == 200, closed.text
    assert chat_id not in [
        c["id"] for c in (await client.get("/api/v1/ops/site/chats/unread", headers=h2)).json()["chats"]
    ]


async def test_assign_to_me_and_back(client, session):
    cust, _ = await _customer_chat(client)
    chat_id = cust["chat_id"]
    op = await _operator(client, session)
    h = auth_headers(op)

    assigned = await client.post(
        f"/api/v1/ops/site/chats/{chat_id}/assign", json={"to_me": True}, headers=h
    )
    assert assigned.status_code == 200, assigned.text
    body = assigned.json()
    assert body["assigned_user_id"] is not None
    assert body["assigned_name"]

    item = await _chat_item(client, chat_id, h)
    assert item["assigned_user_id"] == body["assigned_user_id"]
    assert item["assigned_name"] == body["assigned_name"]

    mine = (
        await client.get("/api/v1/ops/site/chats", params={"mine": "true"}, headers=h)
    ).json()["chats"]
    assert [c["id"] for c in mine] == [chat_id]

    cleared = await client.post(
        f"/api/v1/ops/site/chats/{chat_id}/assign", json={"to_me": False}, headers=h
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"assigned_user_id": None, "assigned_name": None}
    assert (
        await client.get("/api/v1/ops/site/chats", params={"mine": "true"}, headers=h)
    ).json()["chats"] == []


async def test_chat_detail_carries_customer_context(client, session):
    cust, org = await _customer_chat(client)
    vis = await _visitor_chat(client)
    op = await _operator(client, session)
    h = auth_headers(op)

    detail = await client.get(f"/api/v1/ops/site/chats/{cust['chat_id']}", headers=h)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["kind"] == "customer"
    assert body["customer"] is not None
    assert body["customer"]["org_id"] == org["id"]
    assert body["customer"]["org_name"] == "Acme Plumbing"
    assert "plan" in body["customer"]
    assert "balance_usd" in body["customer"]
    assert body["messages"][0]["text"] == "I need a person"

    vdetail = await client.get(f"/api/v1/ops/site/chats/{vis['chat_id']}", headers=h)
    assert vdetail.status_code == 200, vdetail.text
    assert vdetail.json()["customer"] is None


async def test_new_ops_routes_reject_non_operators(client, session):
    vis = await _visitor_chat(client)
    chat_id = vis["chat_id"]

    token = await register_and_login(client, "plain@example.com")
    h = auth_headers(token)
    assert (await client.get("/api/v1/ops/site/chats/unread", headers=h)).status_code in (401, 403)
    assert (
        await client.post(f"/api/v1/ops/site/chats/{chat_id}/read", headers=h)
    ).status_code in (401, 403)
    assert (
        await client.post(
            f"/api/v1/ops/site/chats/{chat_id}/assign", json={"to_me": True}, headers=h
        )
    ).status_code in (401, 403)

    # No credentials at all is refused too.
    assert (await client.get("/api/v1/ops/site/chats/unread")).status_code in (401, 403)
    assert (
        await client.post(f"/api/v1/ops/site/chats/{chat_id}/read")
    ).status_code in (401, 403)
    assert (
        await client.post(
            f"/api/v1/ops/site/chats/{chat_id}/assign", json={"to_me": True}
        )
    ).status_code in (401, 403)


# ----------------------------------------------------------------------------------
# push
# ----------------------------------------------------------------------------------
async def test_push_is_scheduled_for_a_new_customer_chat(client, monkeypatch):
    """With FCM on, alerting must schedule a push for the chat (assignee None when unassigned)."""
    calls: list[tuple] = []

    async def recorder(settings, chat_id, assignee, title, body):
        calls.append((chat_id, assignee))

    monkeypatch.setattr(site_routes, "_push_operators", recorder)
    monkeypatch.setattr(fcm, "enabled", lambda settings: True)

    cust, _ = await _customer_chat(client)
    chat_id = uuid.UUID(cust["chat_id"])

    # Let any scheduled background task run.
    for _ in range(10):
        await asyncio.sleep(0)
    pending = getattr(device_push, "_pending", None)
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)

    assert (chat_id, None) in calls


async def test_history_poll_includes_the_customers_own_messages(client, session):
    handoff = await client.post(
        "/api/v1/public/site-chat/handoff",
        json={"name": "Vi", "email": "vi@example.com", "transcript": [{"role": "visitor", "text": "hello"}]},
    )
    assert handoff.status_code == 201
    chat_id, token = handoff.json()["chat_id"], handoff.json()["token"]
    sent = await client.post(
        f"/api/v1/public/site-chat/{chat_id}/messages", json={"token": token, "text": "anyone there?"}
    )
    assert sent.status_code == 201

    plain = await client.get(f"/api/v1/public/site-chat/{chat_id}/messages", params={"token": token})
    assert all(m["role"] in ("agent", "system") for m in plain.json()["messages"])

    full = await client.get(
        f"/api/v1/public/site-chat/{chat_id}/messages", params={"token": token, "history": "true"}
    )
    texts = [(m["role"], m["text"]) for m in full.json()["messages"]]
    assert ("visitor", "hello") in texts
    assert ("visitor", "anyone there?") in texts
