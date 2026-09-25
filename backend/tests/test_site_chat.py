"""Public website: chat assistant, handoff to a person, and Talk to sales (routes/site.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes import site as site_routes
from app.main import create_app
from app.models import SecurityAlert
from app.models.site import SiteChat, SiteLead
from tests.conftest import _install, FakeCarrier, auth_headers, make_settings
from tests.test_p41_kyc import _make_operator


@pytest.fixture
def site_settings():
    return make_settings(deepseek_api_key="test-key")


@pytest.fixture
async def client(engine, site_settings):
    application = create_app(site_settings)
    _install(application, FakeCarrier())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        yield c


def _fake_llm(reply: str):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "48 contiguous US states" in body  # fixed facts always sent
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ask_answers_from_facts(client, monkeypatch):
    monkeypatch.setattr(site_routes, "_client_factory", _fake_llm("Team is $29 per number a month."))
    r = await client.post(
        "/api/v1/public/site-chat/ask",
        json={"question": "how much is team", "history": [], "context": [{"q": "Plans?", "a": "Team $29"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": "Team is $29 per number a month.", "handoff": False}


async def test_ask_offers_a_person_when_the_model_cannot_answer(client, monkeypatch):
    monkeypatch.setattr(site_routes, "_client_factory", _fake_llm("HANDOFF"))
    r = await client.post("/api/v1/public/site-chat/ask", json={"question": "can you fix my invoice"})
    assert r.json()["handoff"] is True


async def test_ask_survives_a_provider_failure(client, monkeypatch):
    def boom():
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500)))

    monkeypatch.setattr(site_routes, "_client_factory", boom)
    r = await client.post("/api/v1/public/site-chat/ask", json={"question": "anything"})
    assert r.status_code == 200 and r.json()["handoff"] is True


async def test_handoff_round_trip(client, session):
    r = await client.post(
        "/api/v1/public/site-chat/handoff",
        json={
            "name": "Dana",
            "email": "Dana@Example.com",
            "phone": None,
            "sms_consent": True,
            "page": "/pricing",
            "reason": "asked",
            "transcript": [{"role": "visitor", "text": "Talk to a person"}],
        },
    )
    assert r.status_code == 201, r.text
    chat_id, token = r.json()["chat_id"], r.json()["token"]
    chat = await session.get(SiteChat, uuid.UUID(chat_id))
    assert chat.email == "dana@example.com" and chat.sms_consent is False  # no phone, no texts
    assert chat.token_hash != token
    alert = (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "site_chat_handoff"))
    ).scalar_one()
    assert alert.detail["chat_id"] == chat_id

    # A wrong token sees nothing.
    bad = await client.get(f"/api/v1/public/site-chat/{chat_id}/messages", params={"token": "nope"})
    assert bad.status_code == 404

    # The visitor writes, an operator replies, the visitor sees the reply, the operator closes.
    sent = await client.post(f"/api/v1/public/site-chat/{chat_id}/messages", json={"token": token, "text": "Hello?"})
    assert sent.status_code == 201
    op_token = await _make_operator(client, session, f"op-{uuid.uuid4().hex[:6]}@example.com", role="reviewer")
    h = auth_headers(op_token)
    listing = await client.get("/api/v1/ops/site/chats", headers=h)
    assert listing.status_code == 200, listing.text
    assert any(c["id"] == chat_id for c in listing.json()["chats"])
    reply = await client.post(f"/api/v1/ops/site/chats/{chat_id}/reply", json={"text": "Hi Dana!"}, headers=h)
    assert reply.status_code == 201, reply.text
    polled = (
        await client.get(f"/api/v1/public/site-chat/{chat_id}/messages", params={"token": token, "after": ""})
    ).json()
    assert polled["status"] == "active"
    assert [m["text"] for m in polled["messages"]] == ["Hi Dana!"]
    closed = await client.post(f"/api/v1/ops/site/chats/{chat_id}/close", json={}, headers=h)
    assert closed.status_code == 200
    after_close = await client.post(f"/api/v1/public/site-chat/{chat_id}/messages", json={"token": token, "text": "Still there?"})
    assert after_close.status_code == 409


async def test_ops_routes_need_an_operator(client):
    r = await client.get("/api/v1/ops/site/chats")
    assert r.status_code in (401, 403)


async def test_sales_lead_is_stored_and_flagged(client, session):
    r = await client.post(
        "/api/v1/public/sales-leads",
        json={
            "name": "Sam",
            "email": "sam@example.com",
            "phone": "+15125550100",
            "company": None,
            "team_size": "4-8",
            "numbers_needed": "2-4",
            "switching_from": "Quo (OpenPhone)",
            "message": "Moving 3 numbers",
            "sms_consent": True,
            "plan": "team",
            "page": "/sales",
        },
    )
    assert r.status_code == 202, r.text
    lead = (await session.execute(sa.select(SiteLead))).scalar_one()
    assert lead.sms_consent is True and lead.plan == "team"
    assert (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "sales_lead"))
    ).scalar_one()


async def test_sales_lead_rejects_a_bad_email(client):
    r = await client.post(
        "/api/v1/public/sales-leads",
        json={"name": "x", "email": "not-an-email", "team_size": "1", "numbers_needed": "1"},
    )
    assert r.status_code == 422


def test_staffed_hours_are_weekdays_nine_to_six_central():
    assert site_routes.staffed_now(datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc))  # Mon 10:00 CDT
    assert not site_routes.staffed_now(datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc))  # Sun 22:00 CDT
    assert not site_routes.staffed_now(datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc))  # Sun noon
