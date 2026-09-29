"""The support assistant (services/support_agent.py) in stored chats and the ask endpoints.

DeepSeek is faked with an httpx MockTransport that records every request body, so the tests
can check what the model was sent (knowledge, account summary, history) and script what it
answers (a reply, or a reply ending in the handoff marker).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes import site as site_routes
from app.models import SecurityAlert
from app.models.site import SiteChat, SiteChatMessage
from app.services import support_agent
from tests.conftest import auth_headers, create_org, make_settings, register_and_login
from tests.test_support_chat_inbox import _chat_item, _operator


@pytest.fixture
def chat_settings():
    return make_settings(deepseek_api_key="test-key")


@pytest.fixture
async def client(engine, chat_settings):
    from app.main import create_app

    application = create_app(chat_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        yield c


class FakeLLM:
    def __init__(self, monkeypatch, *answers: str):
        self.answers = list(answers)
        self.bodies: list[dict] = []
        monkeypatch.setattr(support_agent, "client_factory", self._factory)

    def _factory(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.read().decode()))
            text = self.answers.pop(0) if self.answers else "OK."
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def system(self, i: int = -1) -> str:
        return self.bodies[i]["messages"][0]["content"]

    def turns(self, i: int = -1) -> list[tuple[str, str]]:
        return [(m["role"], m["content"]) for m in self.bodies[i]["messages"][1:]]


@pytest.fixture(autouse=True)
def _staffed(monkeypatch):
    monkeypatch.setattr(site_routes, "staffed_now", lambda now=None: True)


async def _customer(client, email="cust@example.com", org_name="Acme Plumbing"):
    token = await register_and_login(client, email)
    org = await create_org(client, token, org_name)
    return auth_headers(token, org["id"])


async def _start(client, headers, text="How do I record calls?", **extra):
    r = await client.post(
        "/api/v1/support/chat",
        json={"page": "/app", "transcript": [{"role": "visitor", "text": text}], **extra},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _poll(client, body, history=True):
    r = await client.get(
        f"/api/v1/public/site-chat/{body['chat_id']}/messages",
        params={"token": body["token"], "history": str(history).lower()},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _alerts(session) -> int:
    return len(
        (await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "site_chat_handoff")))
        .scalars()
        .all()
    )


async def test_app_chat_is_answered_by_the_assistant_with_docs_and_account(client, session, monkeypatch):
    llm = FakeLLM(monkeypatch, "Turn it on in Settings -> Calling -> Recording & results.")
    body = await _start(client, await _customer(client))
    assert body["ai"] is True

    polled = await _poll(client, body)
    assert polled["ai"] is True
    assert [(m["role"], m.get("ai", False)) for m in polled["messages"]] == [
        ("visitor", False),
        ("agent", True),
    ]
    assert "Recording & results" in polled["messages"][1]["text"]

    system = llm.system()
    assert "Ringlite Support Knowledge Base" in system  # the docs
    assert "Acme Plumbing" in system  # this customer's workspace summary
    assert "Prepaid balance" in system
    assert llm.turns() == [("user", "How do I record calls?")]
    # The assistant alone: nobody is paged, and the chat is not unread for operators.
    assert await _alerts(session) == 0


async def test_follow_ups_carry_the_whole_conversation(client, monkeypatch):
    llm = FakeLLM(monkeypatch, "First answer.", "Second answer.")
    body = await _start(client, await _customer(client), text="Question one")
    r = await client.post(
        f"/api/v1/public/site-chat/{body['chat_id']}/messages",
        json={"token": body["token"], "text": "And question two?"},
    )
    assert r.status_code == 201, r.text
    assert llm.turns() == [
        ("user", "Question one"),
        ("assistant", "First answer."),
        ("user", "And question two?"),
    ]
    texts = [m["text"] for m in (await _poll(client, body))["messages"]]
    assert texts == ["Question one", "First answer.", "And question two?", "Second answer."]


async def test_the_assistant_hands_off_and_the_team_is_alerted(client, session, monkeypatch):
    FakeLLM(monkeypatch, f"I'll pass you to our team for the refund. {support_agent.HANDOFF}")
    body = await _start(client, await _customer(client), text="I want a refund")
    chat = await session.get(SiteChat, uuid.UUID(body["chat_id"]))
    await session.refresh(chat)
    assert chat.ai_state == "handoff"
    assert await _alerts(session) == 1

    msgs = (await _poll(client, body))["messages"]
    assert msgs[-1]["text"] == "I'll pass you to our team for the refund."  # marker stripped
    assert (await _poll(client, body))["ai"] is False

    op = auth_headers(await _operator(client, session))
    assert (await _chat_item(client, body["chat_id"], op))["unread"] is True


async def test_operator_reply_stops_the_assistant(client, session, monkeypatch):
    llm = FakeLLM(monkeypatch, "First answer.")
    body = await _start(client, await _customer(client))
    op = auth_headers(await _operator(client, session))
    item = await _chat_item(client, body["chat_id"], op)
    assert item["ai_state"] == "active" and item["unread"] is False

    r = await client.post(f"/api/v1/ops/site/chats/{body['chat_id']}/reply", json={"text": "Hi, Sam here."}, headers=op)
    assert r.status_code == 201, r.text
    await client.post(
        f"/api/v1/public/site-chat/{body['chat_id']}/messages",
        json={"token": body["token"], "text": "Thanks Sam"},
    )
    assert len(llm.bodies) == 1  # no second assistant call
    roles = [m["role"] for m in (await _poll(client, body))["messages"]]
    assert roles == ["visitor", "agent", "agent", "visitor"]
    assert (await _chat_item(client, body["chat_id"], op))["ai_state"] == "off"


async def test_asking_for_a_person_in_hours_keeps_the_assistant_out(client, session, monkeypatch):
    llm = FakeLLM(monkeypatch)
    body = await _start(client, await _customer(client), want_person=True)
    assert body["ai"] is False
    assert await _alerts(session) == 1
    assert llm.bodies == []


async def test_out_of_hours_the_assistant_helps_and_the_team_still_sees_it(client, session, monkeypatch):
    monkeypatch.setattr(site_routes, "staffed_now", lambda now=None: False)
    llm = FakeLLM(monkeypatch, "Here is how.")
    r = await client.post(
        "/api/v1/public/site-chat/handoff",
        json={"name": "Dana", "email": "dana@example.com", "transcript": [{"role": "visitor", "text": "Hi"}]},
    )
    body = r.json()
    assert body["ai"] is True and body["staffed"] is False
    await client.post(
        f"/api/v1/public/site-chat/{body['chat_id']}/messages",
        json={"token": body["token"], "text": "How do I port a number?"},
    )
    assert len(llm.bodies) == 1
    assert "not signed in" in llm.system()  # a visitor: no account summary
    op = auth_headers(await _operator(client, session))
    item = await _chat_item(client, body["chat_id"], op)
    assert item["ai_state"] == "assist" and item["unread"] is True


async def test_support_ask_sees_the_account_and_public_ask_does_not(client, monkeypatch):
    llm = FakeLLM(monkeypatch, "Your texting is pending.", "Generic answer.")
    headers = await _customer(client, org_name="Zed Roofing")
    r = await client.post("/api/v1/support/ask", json={"question": "why can't I text?"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": "Your texting is pending.", "handoff": False}
    assert "Zed Roofing" in llm.system()

    r = await client.post("/api/v1/public/site-chat/ask", json={"question": "why can't I text?"})
    assert r.json()["handoff"] is False
    assert "Zed Roofing" not in llm.system()


async def test_support_ask_needs_a_session(client):
    r = await client.post("/api/v1/support/ask", json={"question": "hi"})
    assert r.status_code in (401, 403)


def test_roles_map_to_the_chat_api():
    assert support_agent.to_messages(
        [("visitor", "q"), ("ai", "a"), ("agent", "team"), ("system", "x"), ("visitor", " ")]
    ) == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "assistant", "content": "(Ringlite team member) team"},
    ]


async def test_no_key_means_handoff_without_a_network_call(monkeypatch):
    llm = FakeLLM(monkeypatch)
    reply = await support_agent.reply(make_settings(deepseek_api_key=""), [("visitor", "hi")])
    assert reply.handoff is True and llm.bodies == []
