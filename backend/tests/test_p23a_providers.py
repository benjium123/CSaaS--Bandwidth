from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.main import create_app
from app.models import Org, OrgMembership, Role
from app.models.ai_providers import AiProviderAccount
from app.repositories import users as users_repo
from app.services import ai_providers as ai_providers_svc
from app.services import credentials as credential_svc
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client_with_key(engine):
    key = Fernet.generate_key().decode()
    app_settings = make_settings(credentials_master_key=key)
    application = create_app(app_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app_settings


async def _post_ai(
    client: httpx.AsyncClient,
    headers: dict,
    *,
    kind: str = "llm",
    provider: str = "openai",
    label: str = "primary",
    credentials: dict | None = None,
) -> str:
    creds = credentials if credentials is not None else {"api_key": "sk-super-secret-123"}
    r = await client.post(
        "/api/v1/ai/providers",
        json={"kind": kind, "provider": provider, "label": label, "credentials": creds},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_member_with_role(
    client: httpx.AsyncClient, session, org_id: uuid.UUID, email: str, permissions: list[str]
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


async def test_ai_provider_create_encrypts_and_never_echoes_secret(client_with_key, session):
    client, settings = client_with_key
    token = await register_and_login(client, "p23a-create@example.com")
    org = await create_org(client, token, "Org Create")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    secret = "sk-super-secret-123"
    r = await client.post(
        "/api/v1/ai/providers",
        json={
            "kind": "llm",
            "provider": "openai",
            "label": "main",
            "credentials": {"api_key": secret},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert secret not in r.text
    body = r.json()
    assert body["credentials"]["api_key"] == ai_providers_svc.MASKED_SECRET_VALUE

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(AiProviderAccount).where(AiProviderAccount.id == body["id"])
        )
    ).scalar_one()
    assert secret not in row.credentials_encrypted
    assert credential_svc.decrypt(settings, row.credentials_encrypted)["api_key"] == secret

    listed = await client.get("/api/v1/ai/providers", headers=headers)
    assert listed.status_code == 200
    assert secret not in listed.text


async def test_ai_provider_create_rejects_unknown_provider_and_missing_field(client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-create-invalid@example.com")
    org = await create_org(client, token, "Org Invalid")
    headers = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/ai/providers",
        json={
            "kind": "llm",
            "provider": "not-a-real-provider",
            "label": "x",
            "credentials": {"api_key": "sk"},
        },
        headers=headers,
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/ai/providers",
        json={
            "kind": "tts",
            "provider": "openai",
            "label": "x",
            "credentials": {"api_key": "sk"},
        },
        headers=headers,
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/ai/providers",
        json={"kind": "llm", "provider": "openai", "label": "x", "credentials": {}},
        headers=headers,
    )
    assert r.status_code == 422


async def test_probe_failure_sets_failed_and_success_sets_active(monkeypatch, client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-probe@example.com")
    org = await create_org(client, token, "Org Probe")
    headers = auth_headers(token, org["id"])
    account_id = await _post_ai(
        client, headers, credentials={"api_key": "sk-probe-secret"}
    )

    async def fake_fail(provider, credentials, *, client=None):
        return ai_providers_svc.AiProbeResult(
            provider=provider, ok=False, detail="That key was rejected."
        )

    monkeypatch.setattr(ai_providers_svc, "probe", fake_fail)
    failed = await client.post(
        f"/api/v1/ai/providers/{account_id}/probe", headers=headers
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert failed.json()["last_probe_detail"] == "That key was rejected."
    # The binding contract for the probe endpoint is {status, detail}.
    assert failed.json()["detail"] == "That key was rejected."
    assert failed.json()["last_probe_at"] is not None
    assert "sk-probe-secret" not in failed.text

    async def fake_ok(provider, credentials, *, client=None):
        return ai_providers_svc.AiProbeResult(
            provider=provider, ok=True, detail="Connection works."
        )

    monkeypatch.setattr(ai_providers_svc, "probe", fake_ok)
    ok = await client.post(f"/api/v1/ai/providers/{account_id}/probe", headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "active"


async def test_probe_is_idempotent(monkeypatch, client_with_key, session):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-probe-idempotent@example.com")
    org = await create_org(client, token, "Org Idempotent")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])
    account_id = await _post_ai(client, headers)

    async def fake_ok(provider, credentials, *, client=None):
        return ai_providers_svc.AiProbeResult(
            provider=provider, ok=True, detail="Connection works."
        )

    monkeypatch.setattr(ai_providers_svc, "probe", fake_ok)
    first = await client.post(f"/api/v1/ai/providers/{account_id}/probe", headers=headers)
    second = await client.post(f"/api/v1/ai/providers/{account_id}/probe", headers=headers)
    assert first.json()["status"] == "active"
    assert second.json()["status"] == "active"

    set_org_context(session, org_id)
    count = (
        await session.execute(sa.select(sa.func.count(AiProviderAccount.id)))
    ).scalar_one()
    assert count == 1


async def test_probe_transport_error_is_failed_not_500(monkeypatch, client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-probe-transport@example.com")
    org = await create_org(client, token, "Org Transport")
    headers = auth_headers(token, org["id"])
    account_id = await _post_ai(client, headers)

    real_probe = ai_providers_svc.probe

    async def fake_probe(provider, credentials, *, client=None):
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom"))
        )
        async with httpx.AsyncClient(transport=transport) as http:
            return await real_probe(provider, credentials, client=http)

    monkeypatch.setattr(ai_providers_svc, "probe", fake_probe)
    r = await client.post(f"/api/v1/ai/providers/{account_id}/probe", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert "Could not reach openai" in r.json()["last_probe_detail"]


async def test_probe_uses_the_right_endpoint_per_provider():
    calls: list[tuple[httpx.URL, httpx.Headers]] = []

    def handler(request):
        calls.append((request.url, request.headers))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        specs = [
            ("openai", "k1", "api.openai.com", "Authorization", "Bearer k1"),
            ("elevenlabs", "k2", "api.elevenlabs.io", "xi-api-key", "k2"),
            ("deepgram", "k3", "api.deepgram.com", "Authorization", "Token k3"),
        ]
        for provider, key, _host, _header_name, _header_value in specs:
            result = await ai_providers_svc.probe(
                provider, {"api_key": key}, client=client
            )
            assert result.ok is True

    for _provider, key, host, header_name, header_value in specs:
        url, headers = calls.pop(0)
        assert url.host == host
        assert headers.get(header_name) == header_value
        assert key not in str(url)


async def test_patch_merges_credentials_and_resets_status_to_unverified(
    monkeypatch, client_with_key, session
):
    client, settings = client_with_key
    token = await register_and_login(client, "p23a-patch@example.com")
    org = await create_org(client, token, "Org Patch")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])
    account_id = await _post_ai(client, headers, credentials={"api_key": "sk-old"})

    async def fake_ok(provider, credentials, *, client=None):
        return ai_providers_svc.AiProbeResult(
            provider=provider, ok=True, detail="Connection works."
        )

    monkeypatch.setattr(ai_providers_svc, "probe", fake_ok)
    probed = await client.post(f"/api/v1/ai/providers/{account_id}/probe", headers=headers)
    assert probed.json()["status"] == "active"

    patched = await client.patch(
        f"/api/v1/ai/providers/{account_id}",
        json={"credentials": {"api_key": "sk-rotated"}},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "unverified"
    assert patched.json()["last_probe_at"] is None

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(AiProviderAccount).where(AiProviderAccount.id == account_id)
        )
    ).scalar_one()
    assert credential_svc.decrypt(settings, row.credentials_encrypted)["api_key"] == "sk-rotated"

    label_only = await client.patch(
        f"/api/v1/ai/providers/{account_id}", json={"label": "renamed"}, headers=headers
    )
    assert label_only.status_code == 200
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(AiProviderAccount).where(AiProviderAccount.id == account_id)
        )
    ).scalar_one()
    assert credential_svc.decrypt(settings, row.credentials_encrypted)["api_key"] == "sk-rotated"

    masked_roundtrip = await client.patch(
        f"/api/v1/ai/providers/{account_id}",
        json={"credentials": {"api_key": ai_providers_svc.MASKED_SECRET_VALUE}},
        headers=headers,
    )
    assert masked_roundtrip.status_code == 200
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(AiProviderAccount).where(AiProviderAccount.id == account_id)
        )
    ).scalar_one()
    assert credential_svc.decrypt(settings, row.credentials_encrypted)["api_key"] == "sk-rotated"


async def test_patch_status_accepts_only_disabled_or_unverified(client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-status@example.com")
    org = await create_org(client, token, "Org Status")
    headers = auth_headers(token, org["id"])
    account_id = await _post_ai(client, headers)

    r = await client.patch(
        f"/api/v1/ai/providers/{account_id}", json={"status": "active"}, headers=headers
    )
    assert r.status_code == 422

    r = await client.patch(
        f"/api/v1/ai/providers/{account_id}", json={"status": "disabled"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disabled"


async def test_delete_removes_the_account(client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-delete@example.com")
    org = await create_org(client, token, "Org Delete")
    headers = auth_headers(token, org["id"])
    account_id = await _post_ai(client, headers)

    r = await client.delete(f"/api/v1/ai/providers/{account_id}", headers=headers)
    assert r.status_code == 204

    listed = await client.get("/api/v1/ai/providers", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []


async def test_ai_settings_byok_ready_and_missing_kinds(monkeypatch, client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-settings@example.com")
    org = await create_org(client, token, "Org Settings")
    headers = auth_headers(token, org["id"])

    async def fake_ok(provider, credentials, *, client=None):
        return ai_providers_svc.AiProbeResult(
            provider=provider, ok=True, detail="Connection works."
        )

    monkeypatch.setattr(ai_providers_svc, "probe", fake_ok)

    initial = await client.get("/api/v1/ai/settings", headers=headers)
    assert initial.status_code == 200
    body = initial.json()
    assert body["ai_key_mode"] == "platform"
    assert body["byok_ready"] is False
    assert body["missing_kinds"] == ["llm", "stt", "tts"]
    assert body["missing_kind_labels"] == [
        "language model",
        "speech recognition",
        "voice",
    ]

    llm_id = await _post_ai(client, headers, kind="llm", provider="openai", label="llm")
    tts_id = await _post_ai(
        client,
        headers,
        kind="tts",
        provider="elevenlabs",
        label="tts",
        credentials={"api_key": "sk-11", "default_voice_id": "voice-1"},
    )
    for account_id in (llm_id, tts_id):
        probed = await client.post(
            f"/api/v1/ai/providers/{account_id}/probe", headers=headers
        )
        assert probed.status_code == 200, probed.text
        assert probed.json()["status"] == "active"

    two_kinds = await client.get("/api/v1/ai/settings", headers=headers)
    assert two_kinds.status_code == 200
    assert two_kinds.json()["byok_ready"] is False
    assert two_kinds.json()["missing_kinds"] == ["stt"]
    assert two_kinds.json()["missing_kind_labels"] == ["speech recognition"]

    stt_id = await _post_ai(
        client,
        headers,
        kind="stt",
        provider="deepgram",
        label="stt",
        credentials={"api_key": "sk-dg"},
    )
    probed = await client.post(f"/api/v1/ai/providers/{stt_id}/probe", headers=headers)
    assert probed.status_code == 200, probed.text

    all_kinds = await client.get("/api/v1/ai/settings", headers=headers)
    assert all_kinds.status_code == 200
    assert all_kinds.json()["byok_ready"] is True
    assert all_kinds.json()["missing_kinds"] == []

    patched = await client.patch(
        "/api/v1/ai/settings", json={"ai_key_mode": "byok"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["ai_key_mode"] == "byok"

    bad = await client.patch(
        "/api/v1/ai/settings", json={"ai_key_mode": "nonsense"}, headers=headers
    )
    assert bad.status_code == 422


async def test_resolve_call_config_platform_mode_uses_env_keys(session):
    settings = make_settings(
        openai_api_key="env-openai",
        deepgram_api_key="env-deepgram",
        elevenlabs_api_key="env-eleven",
    )
    org = SimpleNamespace(id=uuid.uuid4(), ai_key_mode="platform")
    profile = SimpleNamespace(
        llm_provider="",
        llm_model="",
        stt_provider="",
        tts_provider="",
        voice_id="",
    )

    cfg = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=True
    )
    assert cfg["mode"] == "platform"
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["model"] == "gpt-4o-mini"
    assert cfg["stt"]["provider"] == "deepgram"
    assert cfg["tts"]["provider"] == "elevenlabs"
    assert cfg["keys"]["llm"] == "env-openai"
    assert cfg["keys"]["stt"] == "env-deepgram"
    assert cfg["keys"]["tts"] == "env-eleven"

    cfg_no_keys = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=False
    )
    assert cfg_no_keys["keys"] is None

    # Settings reads the ambient environment AND the repo's .env, so "no keys configured"
    # has to be stated explicitly - a developer with DEEPSEEK_API_KEY exported would
    # otherwise see a DIFFERENT llm provider resolve here and the assertion below would
    # fail for a reason that has nothing to do with the code under test.
    empty_settings = make_settings(
        **dict.fromkeys(ai_providers_svc.PLATFORM_KEY_ATTRS.values(), "")
    )
    cfg_missing = await ai_providers_svc.resolve_call_config(
        session, empty_settings, org=org, profile=profile, include_keys=False
    )
    assert "OPENAI_API_KEY" in cfg_missing["missing"]
    assert "DEEPGRAM_API_KEY" in cfg_missing["missing"]
    assert "ELEVENLABS_API_KEY" in cfg_missing["missing"]


async def test_resolve_call_config_byok_mode_uses_org_keys(session):
    key = Fernet.generate_key().decode()
    settings = make_settings(
        credentials_master_key=key,
        openai_api_key="env-openai",
        deepgram_api_key="env-deepgram",
        elevenlabs_api_key="env-eleven",
    )
    # ai_provider_accounts.org_id is a real FK to orgs.id and conftest turns SQLite's
    # foreign_keys pragma ON, so the org row has to exist before the accounts do.
    # (Org itself is deliberately NOT TenantScoped, so it needs no org context to insert.)
    org_row = Org(id=uuid.uuid4(), name="Byok Org", slug=f"byok-{uuid.uuid4().hex[:8]}")
    session.add(org_row)
    await session.commit()
    org_id = org_row.id
    set_org_context(session, org_id)

    llm = AiProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        kind="llm",
        provider="openai",
        label="llm",
        credentials_encrypted=credential_svc.encrypt(settings, {"api_key": "org-openai"}),
        status="active",
        created_by=None,
    )
    stt = AiProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        kind="stt",
        provider="deepgram",
        label="stt",
        credentials_encrypted=credential_svc.encrypt(settings, {"api_key": "org-deepgram"}),
        status="active",
        created_by=None,
    )
    tts = AiProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        kind="tts",
        provider="elevenlabs",
        label="tts",
        credentials_encrypted=credential_svc.encrypt(
            settings, {"api_key": "org-eleven", "default_voice_id": "voice-1"}
        ),
        status="active",
        created_by=None,
    )
    session.add_all([llm, stt, tts])
    await session.commit()

    org = SimpleNamespace(id=org_id, ai_key_mode="byok")
    profile = SimpleNamespace(
        llm_provider="",
        llm_model="",
        stt_provider="",
        tts_provider="",
        voice_id="",
    )

    cfg = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=True
    )
    assert cfg["mode"] == "byok"
    assert cfg["keys"]["llm"] == "org-openai"
    assert cfg["keys"]["stt"] == "org-deepgram"
    assert cfg["keys"]["tts"] == "org-eleven"
    assert cfg["missing"] == []

    tts.status = "disabled"
    await session.commit()

    cfg_two = await ai_providers_svc.resolve_call_config(
        session, settings, org=org, profile=profile, include_keys=True
    )
    assert cfg_two["missing"] == ["voice"]
    assert cfg_two["keys"]["tts"] == ""


async def test_ai_providers_are_tenant_scoped(client_with_key):
    client, _ = client_with_key
    token_a = await register_and_login(client, "p23a-tenant-a@example.com")
    org_a = await create_org(client, token_a, "Org A")
    token_b = await register_and_login(client, "p23a-tenant-b@example.com")
    org_b = await create_org(client, token_b, "Org B")

    headers_a = auth_headers(token_a, org_a["id"])
    headers_b = auth_headers(token_b, org_b["id"])
    id_a = await _post_ai(client, headers_a, label="A-only")
    id_b = await _post_ai(client, headers_b, label="B-only")

    listed_a = await client.get("/api/v1/ai/providers", headers=headers_a)
    assert listed_a.status_code == 200
    assert [row["id"] for row in listed_a.json()] == [id_a]

    # There is no single-account GET in the contract, so cross-tenant reach is proved on
    # every route that DOES take an id: each must answer 404 (never 403, which would
    # confirm the row exists in some other org).
    patch_b_under_a = await client.patch(
        f"/api/v1/ai/providers/{id_b}", json={"label": "stolen"}, headers=headers_a
    )
    assert patch_b_under_a.status_code == 404

    probe_b_under_a = await client.post(
        f"/api/v1/ai/providers/{id_b}/probe", headers=headers_a
    )
    assert probe_b_under_a.status_code == 404

    delete_b_under_a = await client.delete(
        f"/api/v1/ai/providers/{id_b}", headers=headers_a
    )
    assert delete_b_under_a.status_code == 404

    settings_a = await client.get("/api/v1/ai/settings", headers=headers_a)
    assert settings_a.status_code == 200
    assert settings_a.json()["byok_ready"] is False


async def test_ai_provider_routes_require_settings_permission(client_with_key, session):
    client, _ = client_with_key
    token = await register_and_login(client, "p23a-rbac-owner@example.com")
    org = await create_org(client, token, "Org RBAC")
    org_id = uuid.UUID(org["id"])
    # _add_member_with_role already registers+logs in this member; registering the SAME
    # email a second time would 409, so reuse the token it hands back.
    member_token = await _add_member_with_role(
        client, session, org_id, "p23a-rbac-member@example.com", ["contacts:read"]
    )
    headers = auth_headers(member_token, org["id"])

    listed = await client.get("/api/v1/ai/providers", headers=headers)
    assert listed.status_code == 403

    created = await client.post(
        "/api/v1/ai/providers",
        json={
            "kind": "llm",
            "provider": "openai",
            "label": "x",
            "credentials": {"api_key": "sk"},
        },
        headers=headers,
    )
    assert created.status_code == 403


async def test_missing_master_key_returns_503(client):
    token = await register_and_login(client, "p23a-nokey@example.com")
    org = await create_org(client, token, "Org NoKey")
    headers = auth_headers(token, org["id"])

    listed = await client.get("/api/v1/ai/providers", headers=headers)
    assert listed.status_code == 503

    created = await client.post(
        "/api/v1/ai/providers",
        json={
            "kind": "llm",
            "provider": "openai",
            "label": "x",
            "credentials": {"api_key": "sk"},
        },
        headers=headers,
    )
    assert created.status_code == 503
