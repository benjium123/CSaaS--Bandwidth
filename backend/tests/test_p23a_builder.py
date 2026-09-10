from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import jwt
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.main import create_app
from app.models import AgentProfile, Call, OrgMembership, Role, UsageRecord
from app.repositories import users as users_repo
from app.services import agent as agent_svc
from app.services import ai_providers as ai_providers_svc
from app.services import kb as kb_svc
from app.services import llm_client
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

LK_KEY = "lk-test-key"
LK_SECRET = "lk-test-secret-value-padded-to-32-bytes-plus"
OUR = "+12145550100"
THEIRS = "+19725550199"


def worker_token(
    *, key=LK_KEY, secret=LK_SECRET, sub="agent-worker", exp_offset=3600, **extra
) -> str:
    claims = {"iss": key, "sub": sub, "exp": int(time.time()) + exp_offset, **extra}
    return jwt.encode(claims, secret, algorithm="HS256")


def worker_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _builder_settings(**overrides):
    base = {
        "livekit_api_key": LK_KEY,
        "livekit_api_secret": LK_SECRET,
        "credentials_master_key": Fernet.generate_key().decode(),
        **dict.fromkeys(ai_providers_svc.PLATFORM_KEY_ATTRS.values(), ""),
    }
    base.update(overrides)
    return make_settings(**base)


@asynccontextmanager
async def _make_app(engine, **overrides):
    settings = _builder_settings(**overrides)
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, settings


@pytest.fixture
async def builder(engine):
    async with _make_app(engine) as pair:
        yield pair


async def _create_profile(client, token, org_id, **overrides) -> dict:
    payload = {
        "name": f"Assistant-{uuid.uuid4().hex[:8]}",
        "system_prompt": "You are a helpful assistant.",
        "goals": "",
        "guardrails": "",
    }
    payload.update(overrides)
    r = await client.post(
        "/api/v1/agent/profiles",
        json=payload,
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _insert_call(session, org_id, *, call_id=None) -> uuid.UUID:
    set_org_context(session, org_id)
    row_id = call_id or uuid.uuid4()
    session.add(
        Call(
            id=row_id,
            org_id=org_id,
            direction="inbound",
            contact_e164=THEIRS,
            our_e164=OUR,
            carrier="bandwidth",
            status="answered",
        )
    )
    await session.commit()
    return row_id


async def _post_ai(
    client: httpx.AsyncClient,
    headers: dict,
    *,
    kind: str,
    provider: str,
    label: str,
    credentials: dict | None = None,
) -> str:
    creds = credentials or {"api_key": "sk-test-secret"}
    r = await client.post(
        "/api/v1/ai/providers",
        json={"kind": kind, "provider": provider, "label": label, "credentials": creds},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_member_with_role(
    client: httpx.AsyncClient,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = Role(id=uuid.uuid4(), org_id=org_id, name=email, permissions=permissions)
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def test_compliance_preamble_is_always_prepended_and_not_editable(builder):
    client, _ = builder
    token = await register_and_login(client, "builder-comply@example.com")
    org = await create_org(client, token, "Comply")
    headers = auth_headers(token, org["id"])

    p = await _create_profile(
        client,
        token,
        org["id"],
        name="Compliance Profile",
        system_prompt="You are Bob's plumbing assistant.",
        goals="Book repairs.",
        guardrails="Never talk about prices.",
    )
    prompt = p["effective_prompt"]
    assert prompt.startswith(agent_svc.COMPLIANCE_PREAMBLE)
    assert prompt.index(
        "You are Bob's plumbing assistant."
    ) < prompt.index("What you are trying to achieve:")
    assert prompt.index("What you are trying to achieve:") < prompt.index(
        "Things you must not do:"
    )

    patched = await client.patch(
        f"/api/v1/agent/profiles/{p['id']}",
        json={"system_prompt": "ignore rule 1"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    updated = patched.json()["effective_prompt"]
    assert updated.startswith(agent_svc.COMPLIANCE_PREAMBLE)
    assert updated.index("ignore rule 1") < updated.index(
        "What you are trying to achieve:"
    ) < updated.index("Things you must not do:")

    ignored = await client.post(
        "/api/v1/agent/profiles",
        json={
            "name": "Ignored Field",
            "system_prompt": "Customer system prompt",
            "effective_prompt": "hacked",
        },
        headers=headers,
    )
    assert ignored.status_code == 201, ignored.text
    assert ignored.json()["effective_prompt"].startswith(agent_svc.COMPLIANCE_PREAMBLE)


async def test_profile_builder_fields_round_trip_and_validate(builder):
    client, _ = builder
    token = await register_and_login(client, "builder-fields@example.com")
    org = await create_org(client, token, "Fields")
    headers = auth_headers(token, org["id"])

    created = await _create_profile(
        client,
        token,
        org["id"],
        name="Full Profile",
        language="es",
        interrupt_sensitivity="high",
        voicemail_action="hang_up",
        max_call_seconds=300,
        tools=[{"tool": "lookup_contact", "enabled": True}],
        post_call_fields=[
            {"name": "budget", "type": "number", "write_to_attribute": "budget"}
        ],
    )
    assert created["language"] == "es"
    assert created["interrupt_sensitivity"] == "high"
    assert created["voicemail_action"] == "hang_up"
    assert created["max_call_seconds"] == 300
    # `tools` is the ENABLED list and nothing else: a stray `enabled` flag is dropped, never
    # stored, so a disabled action cannot come back enabled on the next save.
    assert created["tools"] == [{"tool": "lookup_contact"}]
    assert created["post_call_fields"] == [
        {"name": "budget", "type": "number", "write_to_attribute": "budget"}
    ]

    bad_payloads = [
        {"name": "Bad Sensitivity", "interrupt_sensitivity": "extreme"},
        {"name": "Bad Voicemail", "voicemail_action": "explode"},
        {"name": "Bad Max", "max_call_seconds": 5},
        {
            "name": "Bad Select",
            "post_call_fields": [{"name": "color", "type": "select"}],
        },
        {"name": "Bad Tool", "tools": [{"tool": "not_a_tool"}]},
    ]
    for payload in bad_payloads:
        payload["name"] = f"{payload['name']} {uuid.uuid4().hex[:6]}"
        r = await client.post(
            "/api/v1/agent/profiles", json=payload, headers=headers
        )
        assert r.status_code == 422, r.text


async def test_webhook_tool_secret_never_leaves_the_server(builder, session):
    client, _ = builder
    token = await register_and_login(client, "builder-webhook-secret@example.com")
    org = await create_org(client, token, "Webhook Secret")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    secret = "super-secret-value"
    r = await client.post(
        "/api/v1/agent/profiles",
        json={
            "name": "Webhook Secret Profile",
            "tools": [
                {
                    "tool": "webhook",
                    "url": "https://example.com/hook",
                    "secret": secret,
                }
            ],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert secret not in r.text

    listed = await client.get("/api/v1/agent/profiles", headers=headers)
    assert listed.status_code == 200, listed.text
    assert secret not in listed.text
    listed_tools = listed.json()[0]["tools"]
    # Assert the MARKER, not redact_tools(already-redacted) - that would be a tautology
    # that passes even if the route forgot to redact at all.
    assert listed_tools[0]["secret"] == agent_svc.REDACTED_SECRET

    call_id = await _insert_call(session, org_id)
    # P23b/D46: the config seam takes a token BOUND to this call and org. A global
    # worker token is refused there now (it still works on transcript/outcome).
    config = await client.get(
        f"/api/v1/agent/config/{call_id}",
        headers=worker_headers(
            worker_token(call_id=str(call_id), org_id=str(org_id))
        ),
    )
    assert config.status_code == 200, config.text
    assert secret not in config.text
    assert config.json()["tools"][0]["secret"] == "•••••"

    patched = await client.patch(
        f"/api/v1/agent/profiles/{r.json()['id']}",
        json={
            "tools": [
                {
                    "tool": "webhook",
                    "url": "https://example.com/hook",
                    "secret": "•••••",
                }
            ]
        },
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(AgentProfile).where(
                AgentProfile.id == uuid.UUID(r.json()["id"])
            )
        )
    ).scalar_one()
    assert row.tools[0]["secret"] == secret


async def test_webhook_tool_is_called_server_side_with_a_signature():
    secret = "webhook-secret-123"
    arguments = {"foo": "bar", "nested": {"a": 1}}
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(200, text="OK")

    profile = SimpleNamespace(
        tools=[{"tool": "webhook", "url": "https://example.com/hook", "secret": secret}]
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await agent_svc.call_webhook_tool(
            profile, arguments=arguments, client=http
        )

    assert result["ok"] is True
    assert captured["url"] == "https://example.com/hook"
    timestamp = captured["headers"]["X-CSaaS-Timestamp"]
    canonical_body = json.dumps(
        arguments, separators=(",", ":"), sort_keys=True
    ).encode()
    expected = "sha256=" + agent_svc.sign_webhook_body(secret, canonical_body, timestamp)
    assert captured["headers"]["X-CSaaS-Signature"] == expected
    assert secret not in str(captured["headers"])
    assert secret not in captured["body"].decode()

    def boom(request):
        raise httpx.ConnectError("boom")

    transport2 = httpx.MockTransport(boom)
    async with httpx.AsyncClient(transport=transport2) as http2:
        transport_error = await agent_svc.call_webhook_tool(
            profile, arguments=arguments, client=http2
        )
    assert transport_error == {
        "ok": False,
        "status": 0,
        "body": "We could not reach that web address.",
    }


async def test_webhook_tool_refuses_a_private_address(builder):
    client, _ = builder
    token = await register_and_login(client, "builder-webhook-private@example.com")
    org = await create_org(client, token, "Private Webhook")
    headers = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/agent/profiles",
        json={
            "name": "Private HTTP",
            "tools": [
                {
                    "tool": "webhook",
                    "url": "http://127.0.0.1/hook",
                    "secret": "long-secret",
                }
            ],
        },
        headers=headers,
    )
    assert r.status_code == 422, r.text

    profile = SimpleNamespace(
        tools=[
            {
                "tool": "webhook",
                "url": "https://127.0.0.1/hook",
                "secret": "long-secret",
            }
        ]
    )

    def should_not_be_called(request):
        pytest.fail("private webhook address should never produce an HTTP request")

    transport = httpx.MockTransport(should_not_be_called)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ValidationFailedError):
            await agent_svc.call_webhook_tool(profile, arguments={}, client=http)


async def test_go_live_blocked_in_byok_mode_without_all_three_kinds(
    builder, monkeypatch
):
    client, _ = builder
    token = await register_and_login(client, "builder-byok@example.com")
    org = await create_org(client, token, "BYOK")
    headers = auth_headers(token, org["id"])

    mode = await client.patch(
        "/api/v1/ai/settings", json={"ai_key_mode": "byok"}, headers=headers
    )
    assert mode.status_code == 200, mode.text

    profile = await _create_profile(client, token, org["id"], name="BYOK Assistant")

    blocked = await client.post(
        f"/api/v1/agent/profiles/{profile['id']}/go-live", headers=headers
    )
    assert blocked.status_code == 422, blocked.text
    # Fable decision 2026-09-10: the standard error envelope, machine lists under details.
    details = blocked.json()["error"]["details"]
    assert details["missing_kinds"] == ["llm", "stt", "tts"]
    assert details["missing_platform_keys"] == []
    assert details["mode"] == "byok"

    async def fake_ok(provider, credentials, *, client=None):
        return ai_providers_svc.AiProbeResult(
            provider=provider, ok=True, detail="Connection works."
        )

    monkeypatch.setattr(ai_providers_svc, "probe", fake_ok)

    accounts = [
        ("llm", "openai", "llm", {"api_key": "sk-llm"}),
        ("stt", "deepgram", "stt", {"api_key": "sk-stt"}),
        ("tts", "elevenlabs", "tts", {"api_key": "sk-tts"}),
    ]
    for kind, provider, label, creds in accounts:
        account_id = await _post_ai(
            client, headers, kind=kind, provider=provider, label=label, credentials=creds
        )
        probed = await client.post(
            f"/api/v1/ai/providers/{account_id}/probe", headers=headers
        )
        assert probed.status_code == 200, probed.text
        assert probed.json()["status"] == "active"

    ready = await client.post(
        f"/api/v1/agent/profiles/{profile['id']}/go-live", headers=headers
    )
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["ok"] is True
    assert body["ready"] is True
    assert body["missing_kinds"] == []
    assert body["missing_platform_keys"] == []


async def test_go_live_in_platform_mode_reports_unset_platform_keys(engine):
    empty = dict.fromkeys(ai_providers_svc.PLATFORM_KEY_ATTRS.values(), "")
    saved_token = None
    saved_org_id = None
    saved_profile_id = None

    async with _make_app(engine, **empty) as (client, _):
        token = await register_and_login(client, "builder-platform-missing@example.com")
        org = await create_org(client, token, "Platform Missing")
        profile = await _create_profile(client, token, org["id"], name="Platform")
        blocked = await client.post(
            f"/api/v1/agent/profiles/{profile['id']}/go-live",
            headers=auth_headers(token, org["id"]),
        )
        assert blocked.status_code == 422, blocked.text
        blocked_details = blocked.json()["error"]["details"]
        assert "OPENAI_API_KEY" in blocked_details["missing_platform_keys"]
        # Platform mode never reports missing KINDS - that list is byok-only.
        assert blocked_details["missing_kinds"] == []

        saved_token = token
        saved_org_id = org["id"]
        saved_profile_id = profile["id"]

    async with _make_app(
        engine,
        openai_api_key="env-openai",
        deepgram_api_key="env-deepgram",
        elevenlabs_api_key="env-eleven",
    ) as (client2, _):
        ready = await client2.post(
            f"/api/v1/agent/profiles/{saved_profile_id}/go-live",
            headers=auth_headers(saved_token, saved_org_id),
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True
        assert ready.json()["ok"] is True


async def test_simulate_returns_one_turn_and_meters_tokens(
    engine, session, monkeypatch
):
    async with _make_app(engine, openai_api_key="env-openai") as (client, _):
        token = await register_and_login(client, "builder-simulate@example.com")
        org = await create_org(client, token, "Simulate")
        org_id = uuid.UUID(org["id"])
        headers = auth_headers(token, org["id"])
        profile = await _create_profile(client, token, org["id"], name="Simulator")

        seen_system = {}

        async def fake_chat(
            http,
            *,
            provider,
            model,
            api_key,
            system,
            turns,
            tools,
            max_tokens=1024,
            timeout=30.0,
        ):
            seen_system["system"] = system
            return llm_client.ChatResult(
                text="Hello there.", tool_calls=(), tokens_in=11, tokens_out=7
            )

        monkeypatch.setattr(llm_client, "chat", fake_chat)

        r = await client.post(
            f"/api/v1/agent/profiles/{profile['id']}/simulate",
            json={"messages": [{"role": "user", "content": "what are your hours"}]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "Hello there."
        assert body["tokens_in"] == 11
        assert body["tokens_out"] == 7
        assert seen_system["system"].startswith(agent_svc.COMPLIANCE_PREAMBLE)

        set_org_context(session, org_id)
        row = (
            await session.execute(
                sa.select(UsageRecord).where(
                    UsageRecord.org_id == org_id, UsageRecord.metric == "ai_tokens"
                )
            )
        ).scalar_one()
        assert row.quantity == 18

        r2 = await client.post(
            f"/api/v1/agent/profiles/{profile['id']}/simulate",
            json={"messages": [{"role": "user", "content": "what are your hours"}]},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        await session.refresh(row)
        assert row.quantity == 36


async def test_simulate_surfaces_a_missing_connection_as_422_not_500(engine):
    empty = dict.fromkeys(ai_providers_svc.PLATFORM_KEY_ATTRS.values(), "")
    async with _make_app(engine, **empty) as (client, _):
        token = await register_and_login(client, "builder-simulate-missing@example.com")
        org = await create_org(client, token, "Simulate Missing")
        profile = await _create_profile(client, token, org["id"], name="Missing Key")
        r = await client.post(
            f"/api/v1/agent/profiles/{profile['id']}/simulate",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 422, r.text
        assert r.status_code != 500
        msg = r.json()["error"]["message"].lower()
        assert "api" not in msg
        assert "key" not in msg


async def test_simulate_kb_hits_are_returned(engine, session, monkeypatch):
    async with _make_app(engine, openai_api_key="env-openai") as (client, _):
        token = await register_and_login(client, "builder-kb@example.com")
        org = await create_org(client, token, "KB")
        org_id = uuid.UUID(org["id"])
        headers = auth_headers(token, org["id"])
        profile = await _create_profile(client, token, org["id"], name="KB Assistant")

        set_org_context(session, org_id)
        await kb_svc.create_document(
            session,
            org_id,
            "Hours",
            "We are open from nine to five on weekdays. Weekend hours are by appointment.",
        )
        await session.commit()

        seen_system = {}

        async def fake_chat(
            http,
            *,
            provider,
            model,
            api_key,
            system,
            turns,
            tools,
            max_tokens=1024,
            timeout=30.0,
        ):
            seen_system["system"] = system
            return llm_client.ChatResult(
                text="We are open nine to five.", tool_calls=(), tokens_in=5, tokens_out=4
            )

        monkeypatch.setattr(llm_client, "chat", fake_chat)

        r = await client.post(
            f"/api/v1/agent/profiles/{profile['id']}/simulate",
            json={"messages": [{"role": "user", "content": "what are your hours"}]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kb_hits"]
        hit = body["kb_hits"][0]
        assert hit["title"] == "Hours"
        assert hit["document_id"]
        assert "nine to five" in hit["snippet"]
        assert "We are open from nine to five on weekdays." in seen_system["system"]


async def test_worker_config_hides_keys_unless_the_flag_is_on(engine, session):
    call_id = None
    tool_secret = "worker-tool-secret-value"

    async with _make_app(
        engine,
        openai_api_key="env-openai",
        deepgram_api_key="env-deepgram",
        elevenlabs_api_key="env-eleven",
    ) as (client, _):
        token = await register_and_login(client, "builder-worker-config@example.com")
        org = await create_org(client, token, "Worker Config")
        org_id = uuid.UUID(org["id"])
        await _create_profile(
            client,
            token,
            org["id"],
            name="Worker Profile",
            tools=[
                {
                    "tool": "webhook",
                    "url": "https://example.com/hook",
                    "secret": tool_secret,
                }
            ],
        )
        call_id = await _insert_call(session, org_id)
        # P23b/D46: bound to this call and org, not the global worker identity.
        wh = worker_headers(worker_token(call_id=str(call_id), org_id=str(org_id)))

        no_flag = await client.get(f"/api/v1/agent/config/{call_id}", headers=wh)
        assert no_flag.status_code == 200, no_flag.text
        body = no_flag.json()
        assert body["keys"] is None
        assert "env-openai" not in no_flag.text
        assert tool_secret not in no_flag.text
        assert body["tools"][0]["secret"] == "•••••"

        no_token = await client.get(f"/api/v1/agent/config/{call_id}")
        assert no_token.status_code == 401, no_token.text

    async with _make_app(
        engine,
        ai_per_org_keys=True,
        openai_api_key="env-openai",
        deepgram_api_key="env-deepgram",
        elevenlabs_api_key="env-eleven",
    ) as (client2, _):
        with_flag = await client2.get(
            f"/api/v1/agent/config/{call_id}",
            headers=worker_headers(
                worker_token(call_id=str(call_id), org_id=str(org_id))
            ),
        )
        assert with_flag.status_code == 200, with_flag.text
        body2 = with_flag.json()
        assert body2["keys"]["llm"] == "env-openai"
        assert body2["tools"][0]["secret"] == "•••••"


async def test_tool_endpoint_gating(builder, session):
    client, _ = builder
    token = await register_and_login(client, "builder-tools-gate@example.com")
    org = await create_org(client, token, "Tool Gate")
    org_id = uuid.UUID(org["id"])
    call_id = await _insert_call(session, org_id)
    wh = worker_headers(worker_token())
    call_payload = {"call_id": str(call_id), "arguments": {}}

    transfer = await client.post(
        "/api/v1/agent/tools/transfer", json=call_payload, headers=wh
    )
    assert transfer.status_code == 501, transfer.text

    nonsense = await client.post(
        "/api/v1/agent/tools/nonsense", json=call_payload, headers=wh
    )
    assert nonsense.status_code == 404, nonsense.text

    lookup = await client.post(
        "/api/v1/agent/tools/lookup_contact",
        json={"call_id": str(call_id), "arguments": {"e164": "+12145550100"}},
        headers=wh,
    )
    assert lookup.status_code == 200, lookup.text
    assert "result" in lookup.json()

    no_token = await client.post(
        "/api/v1/agent/tools/lookup_contact",
        json={"call_id": str(call_id), "arguments": {"e164": "+12145550100"}},
    )
    assert no_token.status_code == 401, no_token.text


async def test_profiles_are_tenant_scoped(builder):
    client, _ = builder
    token_a = await register_and_login(client, "builder-tenant-a@example.com")
    org_a = await create_org(client, token_a, "Tenant A")
    token_b = await register_and_login(client, "builder-tenant-b@example.com")
    org_b = await create_org(client, token_b, "Tenant B")

    profile_b = await _create_profile(client, token_b, org_b["id"], name="B Profile")
    headers_a = auth_headers(token_a, org_a["id"])

    patch = await client.patch(
        f"/api/v1/agent/profiles/{profile_b['id']}",
        json={"name": "Stolen"},
        headers=headers_a,
    )
    assert patch.status_code == 404, patch.text

    go_live = await client.post(
        f"/api/v1/agent/profiles/{profile_b['id']}/go-live", headers=headers_a
    )
    assert go_live.status_code == 404, go_live.text

    simulate = await client.post(
        f"/api/v1/agent/profiles/{profile_b['id']}/simulate",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=headers_a,
    )
    assert simulate.status_code == 404, simulate.text


async def test_builder_routes_require_settings_permission(builder, session):
    client, _ = builder
    token = await register_and_login(client, "builder-rbac-owner@example.com")
    org = await create_org(client, token, "Builder RBAC")
    org_id = uuid.UUID(org["id"])
    profile = await _create_profile(client, token, org["id"], name="Owner Profile")

    member_token = await _add_member_with_role(
        client,
        session,
        org_id,
        "builder-rbac-member@example.com",
        ["contacts:read"],
    )
    headers = auth_headers(member_token, org["id"])

    create = await client.post(
        "/api/v1/agent/profiles", json={"name": "Member Profile"}, headers=headers
    )
    assert create.status_code == 403, create.text

    go_live = await client.post(
        f"/api/v1/agent/profiles/{profile['id']}/go-live", headers=headers
    )
    assert go_live.status_code == 403, go_live.text

    simulate = await client.post(
        f"/api/v1/agent/profiles/{profile['id']}/simulate",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=headers,
    )
    assert simulate.status_code == 403, simulate.text
