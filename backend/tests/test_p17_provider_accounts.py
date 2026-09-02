from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.errors import CarrierNotConfiguredError
from app.main import create_app
from app.models import OrgMembership, ProviderAccount, Role
from app.providers.registry import build_registry
from app.providers.registry_org import (
    CURRENT_ORG_ID,
    CarrierRegistryProxy,
    build_registry_for_org,
    prime_org_registry,
)
from app.repositories import users as users_repo
from app.services import credentials as credentials_svc
from app.services import provider_accounts as provider_accounts_svc
from app.providers.probes import ProbeResult
from tests.conftest import (
    FakeCarrier,
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
    webhook_auth_headers,
)

TELNYX_ALL = {
    "api_key": "original-secret",
    "public_key": "public-key",
    "messaging_profile_id": "mprofile",
    "voice_connection_id": "vconn",
}


@pytest.fixture
async def client_with_key(engine):
    key = Fernet.generate_key().decode()
    app_settings = make_settings(credentials_master_key=key)
    application = create_app(app_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app_settings


async def _post_telnyx(client: httpx.AsyncClient, headers: dict) -> str:
    r = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "telnyx", "credentials": TELNYX_ALL},
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


def test_credentials_round_trip_and_ciphertext_excludes_plaintext():
    key = Fernet.generate_key().decode()
    settings = make_settings(credentials_master_key=key)

    data = {"telnyx_api_key": "plain-secret-123", "messaging_profile_id": "mp1"}
    token = credentials_svc.encrypt(settings, data)

    assert "plain-secret-123" not in token
    assert credentials_svc.decrypt(settings, token) == data

    missing = make_settings()
    with pytest.raises(CarrierNotConfiguredError):
        credentials_svc.encrypt(missing, {"x": "y"})


async def test_missing_master_key_returns_503(client, session):
    token = await register_and_login(client, "p17-nokey@example.com")
    org = await create_org(client, token, "Org NoKey")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    listed = await client.get("/api/v1/provider-accounts", headers=headers)
    assert listed.status_code == 503

    created = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "x", "credentials": TELNYX_ALL},
        headers=headers,
    )
    assert created.status_code == 503

    # No master key means no account could ever have been created through the API - seed
    # one directly so PATCH/probe/DELETE are exercised against a real row too. The 503
    # guard on each of those endpoints must fire before any decrypt is attempted, so the
    # (unusable) ciphertext below is never actually read.
    set_org_context(session, org_id)
    row = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider="telnyx",
        label="seed",
        credentials_encrypted="not-a-real-fernet-token",
        status="unverified",
        created_by=None,
    )
    session.add(row)
    await session.commit()
    account_id = str(row.id)

    patched = await client.patch(
        f"/api/v1/provider-accounts/{account_id}", json={"label": "x"}, headers=headers
    )
    assert patched.status_code == 503

    probed = await client.post(
        f"/api/v1/provider-accounts/{account_id}/probe", headers=headers
    )
    assert probed.status_code == 503

    deleted = await client.delete(
        f"/api/v1/provider-accounts/{account_id}", headers=headers
    )
    assert deleted.status_code == 503


async def test_validation_missing_required_and_unknown_field(client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p17-val@example.com")
    org = await create_org(client, token, "Org Val")
    headers = auth_headers(token, org["id"])

    missing = await client.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "x",
            "credentials": {"messaging_profile_id": "mp"},
        },
        headers=headers,
    )
    assert missing.status_code == 422

    unknown = await client.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "x",
            "credentials": {**TELNYX_ALL, "bogus": "b"},
        },
        headers=headers,
    )
    assert unknown.status_code == 422


async def test_get_masks_secrets(client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p17-mask@example.com")
    org = await create_org(client, token, "Org Mask")
    headers = auth_headers(token, org["id"])

    await _post_telnyx(client, headers)

    listed = await client.get("/api/v1/provider-accounts", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(x for x in listed.json() if x["provider"] == "telnyx")
    assert row["credentials"]["api_key"] == "•••••"
    assert row["credentials"]["public_key"] == "•••••"
    assert row["credentials"]["messaging_profile_id"] == "mprofile"
    assert "original-secret" not in listed.text


async def test_audit_rows_never_contain_secrets(client_with_key, session):
    client, _ = client_with_key
    token = await register_and_login(client, "p17-audit@example.com")
    org = await create_org(client, token, "Org Audit")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    account_id = await _post_telnyx(client, headers)
    patched = await client.patch(
        f"/api/v1/provider-accounts/{account_id}",
        json={"credentials": {"api_key": "rotated-secret"}},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    from app.models import AuditLogEntry

    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(AuditLogEntry).where(AuditLogEntry.target_id == account_id)
            )
        )
        .scalars()
        .all()
    )
    actions = {row.action for row in rows}
    assert "provider_accounts.create" in actions
    assert "provider_accounts.update" in actions
    for row in rows:
        blob = str(row.detail)
        assert "original-secret" not in blob
        assert "rotated-secret" not in blob
        # Secret field NAMES are withheld too, not just values - only the public
        # (non-secret) fields a mutation touched are ever named.
        assert "api_key" not in row.detail.get("fields", [])


async def test_patch_omitted_secret_keeps_stored(client_with_key, session):
    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-patch@example.com")
    org = await create_org(client, token, "Org Patch")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    account_id = await _post_telnyx(client, headers)

    patched = await client.patch(
        f"/api/v1/provider-accounts/{account_id}",
        json={"credentials": {"messaging_profile_id": "new-profile"}},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["credentials"]["api_key"] == "•••••"

    set_org_context(session, org_id)
    row = await session.get(ProviderAccount, uuid.UUID(account_id))
    assert row is not None
    data = credentials_svc.decrypt(app_settings, row.credentials_encrypted)
    assert data["api_key"] == "original-secret"
    assert data["messaging_profile_id"] == "new-profile"


async def test_probe_success_and_failure(monkeypatch, client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p17-probe@example.com")
    org = await create_org(client, token, "Org Probe")
    headers = auth_headers(token, org["id"])
    account_id = await _post_telnyx(client, headers)

    async def fake_ok(name, settings, *, client=None):
        return ProbeResult(name, True, "Credentials accepted.", f"test://{name}")

    monkeypatch.setattr(provider_accounts_svc.probes, "probe", fake_ok)
    ok = await client.post(
        f"/api/v1/provider-accounts/{account_id}/probe", headers=headers
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "active"
    assert ok.json()["last_probe_detail"] == "Credentials accepted."

    async def fake_fail(name, settings, *, client=None):
        return ProbeResult(name, False, "Bad key", f"test://{name}")

    monkeypatch.setattr(provider_accounts_svc.probes, "probe", fake_fail)
    failed = await client.post(
        f"/api/v1/provider-accounts/{account_id}/probe", headers=headers
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert failed.json()["last_probe_detail"] == "Bad key"


async def test_settings_read_cannot_mutate(client_with_key, session):
    client, _ = client_with_key
    owner_token = await register_and_login(client, "p17-owner-rbac@example.com")
    org = await create_org(client, owner_token, "Org RBAC")
    org_id = uuid.UUID(org["id"])
    owner_headers = auth_headers(owner_token, org["id"])

    account_id = await _post_telnyx(client, owner_headers)

    readonly_token = await _add_member_with_role(
        client, session, org_id, "p17-readonly@example.com", ["settings:read"]
    )
    readonly_headers = auth_headers(readonly_token, org["id"])

    listed = await client.get("/api/v1/provider-accounts", headers=readonly_headers)
    assert listed.status_code == 200

    created = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "x", "credentials": TELNYX_ALL},
        headers=readonly_headers,
    )
    assert created.status_code == 403

    patched = await client.patch(
        f"/api/v1/provider-accounts/{account_id}",
        json={"label": "x"},
        headers=readonly_headers,
    )
    assert patched.status_code == 403

    probe = await client.post(
        f"/api/v1/provider-accounts/{account_id}/probe", headers=readonly_headers
    )
    assert probe.status_code == 403

    deleted = await client.delete(
        f"/api/v1/provider-accounts/{account_id}", headers=readonly_headers
    )
    assert deleted.status_code == 403


async def test_tenant_isolation(client_with_key):
    client, _ = client_with_key

    token_a = await register_and_login(client, "p17-tenant-a@example.com")
    org_a = await create_org(client, token_a, "Org A")
    h_a = auth_headers(token_a, org_a["id"])
    account_id = await _post_telnyx(client, h_a)

    token_b = await register_and_login(client, "p17-tenant-b@example.com")
    org_b = await create_org(client, token_b, "Org B")
    h_b = auth_headers(token_b, org_b["id"])

    listed = await client.get("/api/v1/provider-accounts", headers=h_b)
    assert listed.status_code == 200
    assert listed.json() == []

    hijack = await client.patch(
        f"/api/v1/provider-accounts/{account_id}", json={"label": "hijack"}, headers=h_b
    )
    assert hijack.status_code == 404


async def test_registry_for_org_db_credentials_and_proxy(client_with_key, session):
    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-registry@example.com")
    org = await create_org(client, token, "Registry Org")
    org_id = uuid.UUID(org["id"])

    db_creds = {
        "api_key": "db-api-key",
        "public_key": "db-public-key",
        "messaging_profile_id": "db-profile",
        "voice_connection_id": "db-voice",
    }
    set_org_context(session, org_id)
    row = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider="telnyx",
        label="DB",
        credentials_encrypted=credentials_svc.encrypt(app_settings, db_creds),
        status="active",
        created_by=None,
    )
    session.add(row)
    await session.commit()

    db_registry, db_owned = build_registry_for_org(app_settings, [row])
    db_carrier = db_registry.get("telnyx")
    assert db_carrier is not None
    assert getattr(db_carrier, "api_key") == "db-api-key"
    assert getattr(db_carrier, "messaging_profile_id") == "db-profile"
    assert db_owned.get("telnyx") is db_carrier

    env_settings = make_settings(
        credentials_master_key=app_settings.credentials_master_key.get_secret_value(),
        telnyx_api_key="env-api-key",
        telnyx_enabled=True,
    )
    env_registry, no_db_owned = build_registry_for_org(env_settings, [])
    env_carrier = env_registry.get("telnyx")
    assert env_carrier is not None
    assert getattr(env_carrier, "api_key") == "env-api-key"
    assert no_db_owned == {}

    proxy = CarrierRegistryProxy(build_registry(env_settings))
    global_carrier = proxy.get("telnyx")
    assert global_carrier is not None
    assert getattr(global_carrier, "api_key") == "env-api-key"

    token_ctx = CURRENT_ORG_ID.set(org_id)
    try:
        await prime_org_registry(session, app_settings, org_id)
        db_carrier = proxy.get("telnyx")
        assert db_carrier is not None
        assert getattr(db_carrier, "api_key") == "db-api-key"
    finally:
        CURRENT_ORG_ID.reset(token_ctx)

    after_reset = proxy.get("telnyx")
    assert after_reset is not None
    assert getattr(after_reset, "api_key") == "env-api-key"


async def test_registry_proxy_wired_through_real_requests(client_with_key, monkeypatch):
    """End-to-end: the auth dependency (app/auth/deps.py) primes the org registry and
    sets the CURRENT_ORG_ID contextvar on its own, with no special-casing in the route -
    GET /api/v1/routing/carriers reads request.app.state.carriers exactly as every other
    route does. Also proves disabling an account invalidates the org's cached registry."""
    client, _ = client_with_key

    token_a = await register_and_login(client, "p17-live-a@example.com")
    org_a = await create_org(client, token_a, "Live Org A")
    headers_a = auth_headers(token_a, org_a["id"])

    token_b = await register_and_login(client, "p17-live-b@example.com")
    org_b = await create_org(client, token_b, "Live Org B")
    headers_b = auth_headers(token_b, org_b["id"])

    account_id = await _post_telnyx(client, headers_a)

    async def fake_ok(name, settings, *, client=None):
        return ProbeResult(name, True, "Credentials accepted.", f"test://{name}")

    monkeypatch.setattr(provider_accounts_svc.probes, "probe", fake_ok)
    probed = await client.post(
        f"/api/v1/provider-accounts/{account_id}/probe", headers=headers_a
    )
    assert probed.status_code == 200, probed.text
    assert probed.json()["status"] == "active"

    carriers_a = await client.get("/api/v1/routing/carriers", headers=headers_a)
    assert carriers_a.status_code == 200, carriers_a.text
    assert "telnyx" in {row["name"] for row in carriers_a.json()}

    # Org B has no DB account and no env credentials for telnyx - it must never see org
    # A's carrier through the shared proxy.
    carriers_b = await client.get("/api/v1/routing/carriers", headers=headers_b)
    assert carriers_b.status_code == 200, carriers_b.text
    assert "telnyx" not in {row["name"] for row in carriers_b.json()}

    disabled = await client.delete(
        f"/api/v1/provider-accounts/{account_id}", headers=headers_a
    )
    assert disabled.status_code == 204

    carriers_a_after = await client.get("/api/v1/routing/carriers", headers=headers_a)
    assert carriers_a_after.status_code == 200, carriers_a_after.text
    assert "telnyx" not in {row["name"] for row in carriers_a_after.json()}


async def test_bandwidth_webhook_falls_back_to_db_account(client_with_key, session):
    """app/api/routes/webhooks.py: with no bandwidth webhook creds in the environment at
    all, an active DB provider_account for bandwidth is tried as a verification fallback
    - this is what lets a webhook go live for an org purely through the Providers UI, no
    .env edit or restart."""
    from app.api.routes import webhooks as webhooks_routes

    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-webhook@example.com")
    org = await create_org(client, token, "Webhook Org")
    org_id = uuid.UUID(org["id"])

    creds = {
        "account_id": "acct-1",
        "api_username": "api-user",
        "api_password": "api-pass",
        "messaging_application_id": "msg-app",
        "voice_application_id": "voice-app",
        "webhook_username": "hook-user",
        "webhook_password": "hook-pass",
    }
    set_org_context(session, org_id)
    row = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider="bandwidth",
        label="Hook",
        credentials_encrypted=credentials_svc.encrypt(app_settings, creds),
        status="active",
        created_by=None,
    )
    session.add(row)
    await session.commit()

    # Bypass the short TTL cache from any earlier test in this run.
    webhooks_routes._WEBHOOK_ACCOUNTS_CACHE.clear()

    wrong = await client.post(
        "/api/v1/webhooks/bandwidth/messaging",
        content=b"[]",
        headers=webhook_auth_headers("hook-user", "wrong-pass"),
    )
    assert wrong.status_code == 401

    ok = await client.post(
        "/api/v1/webhooks/bandwidth/messaging",
        content=b"[]",
        headers=webhook_auth_headers("hook-user", "hook-pass"),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"status": "ok", "events": 0}


def test_credential_fields_match_frontend_snapshot():
    """The frontend renders its provider forms off a hand-maintained snapshot
    (frontend/src/api/providerFields.snapshot.json) rather than importing this backend
    module - this is the tripwire that fails CI the moment the two drift, instead of the
    frontend silently rendering the wrong fields (or hiding a newly-secret one)."""
    import json
    from pathlib import Path

    from app.models.provider_accounts import PROVIDER_CREDENTIAL_FIELDS

    snapshot_path = (
        Path(__file__).resolve().parents[2] / "frontend/src/api/providerFields.snapshot.json"
    )
    if not snapshot_path.exists():
        pytest.skip(f"frontend snapshot not present at {snapshot_path}")

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert PROVIDER_CREDENTIAL_FIELDS == snapshot


async def test_bandwidth_webhook_empty_env_creds_never_verify(client):
    """Security review finding #2: with blank BANDWIDTH_WEBHOOK_USERNAME/PASSWORD (the
    unconfigured default) and no DB account, a request presenting an equally-blank Basic
    ':' credential must NOT be treated as verified - "" == "" is still a match for a
    naive constant-time compare."""
    import base64

    empty_basic = base64.b64encode(b":").decode()
    resp = await client.post(
        "/api/v1/webhooks/bandwidth/messaging",
        content=b"[]",
        headers={
            "Authorization": f"Basic {empty_basic}",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_decrypt_with_rotated_key_raises_carrier_not_configured():
    """Security review finding #3: a key that no longer matches the stored ciphertext
    (rotated, or simply wrong) must raise the same CarrierNotConfiguredError as "no key
    configured", not let cryptography's InvalidToken/ValueError escape as a 500."""
    original = make_settings(credentials_master_key=Fernet.generate_key().decode())
    token = credentials_svc.encrypt(original, {"a": "b"})

    rotated = make_settings(credentials_master_key=Fernet.generate_key().decode())
    with pytest.raises(CarrierNotConfiguredError):
        credentials_svc.decrypt(rotated, token)

    malformed_key = make_settings(credentials_master_key="not-a-valid-fernet-key")
    with pytest.raises(CarrierNotConfiguredError):
        credentials_svc.decrypt(malformed_key, token)


async def test_rotated_key_returns_503_not_500(client_with_key):
    """End-to-end companion to the unit test above: GET after an operator rotates
    CREDENTIALS_MASTER_KEY out from under existing rows must 503, never 500.
    Settings is a plain mutable pydantic-settings object and app.state.settings holds
    this EXACT instance (app/main.py: `app.state.settings = settings`), so mutating it
    in place simulates an operator's key rotation against the already-running app."""
    from pydantic import SecretStr

    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-rotate@example.com")
    org = await create_org(client, token, "Org Rotate")
    headers = auth_headers(token, org["id"])

    await _post_telnyx(client, headers)

    app_settings.credentials_master_key = SecretStr(Fernet.generate_key().decode())

    listed = await client.get("/api/v1/provider-accounts", headers=headers)
    assert listed.status_code == 503


async def test_patch_with_get_masked_value_keeps_secret_unchanged(client_with_key, session):
    """Security review finding #4: a client that GETs an account then PATCHes the exact
    body straight back (a natural "save" bug - the form was pre-filled with the masked
    placeholder and the user never touched that field) must not overwrite the stored
    secret with the literal mask string."""
    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-maskpatch@example.com")
    org = await create_org(client, token, "Org MaskPatch")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    account_id = await _post_telnyx(client, headers)

    got = await client.get("/api/v1/provider-accounts", headers=headers)
    row = next(x for x in got.json() if x["id"] == account_id)
    assert row["credentials"]["api_key"] == "•••••"

    patched = await client.patch(
        f"/api/v1/provider-accounts/{account_id}",
        json={"label": "renamed", "credentials": row["credentials"]},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["label"] == "renamed"

    set_org_context(session, org_id)
    stored = await session.get(ProviderAccount, uuid.UUID(account_id))
    data = credentials_svc.decrypt(app_settings, stored.credentials_encrypted)
    assert data["api_key"] == "original-secret"
    assert data["messaging_profile_id"] == "mprofile"


async def test_create_rejects_masked_placeholder_as_a_real_secret(client_with_key):
    client, _ = client_with_key
    token = await register_and_login(client, "p17-maskcreate@example.com")
    org = await create_org(client, token, "Org MaskCreate")
    headers = auth_headers(token, org["id"])

    resp = await client.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "telnyx",
            "label": "x",
            "credentials": {**TELNYX_ALL, "api_key": "•••••"},
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_create_omits_optional_fields(client_with_key):
    """Security review finding #8: only the fields config.py's carrier_requirements()
    would also require for the env path are mandatory - telnyx needs just api_key."""
    client, _ = client_with_key
    token = await register_and_login(client, "p17-optional@example.com")
    org = await create_org(client, token, "Org Optional")
    headers = auth_headers(token, org["id"])

    resp = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "x", "credentials": {"api_key": "only-key"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["credentials"]["public_key"] == ""


async def test_signalwire_space_url_ssrf_guard(client_with_key):
    """Security review finding #5: signalwire_space_url is interpolated straight into a
    request URL by the probe and the live adapter - only a bare *.signalwire.com
    hostname may go in, nothing that could redirect the request (or the org's own
    api_token) somewhere else this server can reach."""
    client, _ = client_with_key
    token = await register_and_login(client, "p17-ssrf@example.com")
    org = await create_org(client, token, "Org SSRF")
    headers = auth_headers(token, org["id"])

    base_creds = {"project_id": "proj", "api_token": "tok"}
    for bad_space_url in (
        "169.254.169.254",
        "internal.local",
        "evil.com/signalwire.com",
        "my-space.signalwire.com.evil.com",
        "my-space.signalwire.com/../metadata",
        "https://my-space.signalwire.com",
        "my-space.signalwire.com:8080",
    ):
        resp = await client.post(
            "/api/v1/provider-accounts",
            json={
                "provider": "signalwire",
                "label": "x",
                "credentials": {**base_creds, "space_url": bad_space_url},
            },
            headers=headers,
        )
        assert resp.status_code == 422, f"{bad_space_url!r} should be rejected: {resp.text}"

    good = await client.post(
        "/api/v1/provider-accounts",
        json={
            "provider": "signalwire",
            "label": "x",
            "credentials": {**base_creds, "space_url": "my-space.signalwire.com"},
        },
        headers=headers,
    )
    assert good.status_code == 201, good.text


async def test_create_conflicts_when_account_already_exists(client_with_key):
    """Security review finding #6: uq_provider_accounts_org_provider means a second
    POST for the same (org, provider) while the first is unverified/active/failed must
    409, not 500 on the unique-constraint clash."""
    client, _ = client_with_key
    token = await register_and_login(client, "p17-conflict@example.com")
    org = await create_org(client, token, "Org Conflict")
    headers = auth_headers(token, org["id"])

    await _post_telnyx(client, headers)

    dup = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "y", "credentials": TELNYX_ALL},
        headers=headers,
    )
    assert dup.status_code == 409


async def test_create_revives_disabled_account(client_with_key, session):
    """Security review finding #6: disable_account never hard-deletes (status becomes
    "disabled"), so re-adding the same provider must revive that row - re-encrypting the
    new credentials and resetting to unverified - not 409 forever."""
    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-revive@example.com")
    org = await create_org(client, token, "Org Revive")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    account_id = await _post_telnyx(client, headers)
    deleted = await client.delete(f"/api/v1/provider-accounts/{account_id}", headers=headers)
    assert deleted.status_code == 204

    new_creds = {**TELNYX_ALL, "api_key": "revived-secret"}
    revived = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "revived", "credentials": new_creds},
        headers=headers,
    )
    assert revived.status_code == 201, revived.text
    # Same row revived, not a second row - the unique constraint allows exactly one.
    assert revived.json()["id"] == account_id
    assert revived.json()["status"] == "unverified"

    set_org_context(session, org_id)
    row = await session.get(ProviderAccount, uuid.UUID(account_id))
    data = credentials_svc.decrypt(app_settings, row.credentials_encrypted)
    assert data["api_key"] == "revived-secret"

    listed = await client.get("/api/v1/provider-accounts", headers=headers)
    assert len(listed.json()) == 1


async def test_disabled_account_stops_verifying_webhooks_immediately(
    client_with_key, monkeypatch
):
    """Security review finding #10: disable_account must clear
    app/api/routes/webhooks.py's _WEBHOOK_ACCOUNTS_CACHE entry for that provider so a
    disabled account stops verifying webhooks on the very next delivery - not up to
    _WEBHOOK_ACCOUNTS_TTL_SECONDS (30s) later."""
    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-disable-webhook@example.com")
    org = await create_org(client, token, "Org DisableWebhook")
    headers = auth_headers(token, org["id"])

    creds = {
        "account_id": "acct-1",
        "api_username": "api-user",
        "api_password": "api-pass",
        "messaging_application_id": "msg-app",
        "webhook_username": "hook-user2",
        "webhook_password": "hook-pass2",
    }
    created = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "bandwidth", "label": "Hook2", "credentials": creds},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]

    async def fake_ok(name, settings, *, client=None):
        return ProbeResult(name, True, "Credentials accepted.", f"test://{name}")

    monkeypatch.setattr(provider_accounts_svc.probes, "probe", fake_ok)
    probed = await client.post(
        f"/api/v1/provider-accounts/{account_id}/probe", headers=headers
    )
    assert probed.status_code == 200, probed.text
    assert probed.json()["status"] == "active"

    from app.api.routes import webhooks as webhooks_routes

    webhooks_routes._WEBHOOK_ACCOUNTS_CACHE.clear()

    verifies = await client.post(
        "/api/v1/webhooks/bandwidth/messaging",
        content=b"[]",
        headers=webhook_auth_headers("hook-user2", "hook-pass2"),
    )
    assert verifies.status_code == 200, verifies.text

    disabled = await client.delete(
        f"/api/v1/provider-accounts/{account_id}", headers=headers
    )
    assert disabled.status_code == 204

    # No TTL wait, no manual cache clear here - disable_account must have already
    # invalidated the provider's cached account list.
    no_longer_verifies = await client.post(
        "/api/v1/webhooks/bandwidth/messaging",
        content=b"[]",
        headers=webhook_auth_headers("hook-user2", "hook-pass2"),
    )
    assert no_longer_verifies.status_code == 401


async def test_carrier_registry_proxy_transparent_dunders_and_live_send(engine, webhook_settings):
    """Security review finding #1: CarrierRegistryProxy must be fully transparent -
    len()/`in`/iter/bool must all behave exactly as they do against a raw CarrierRegistry,
    because app/routing/router.py (len(registry), `c in registry`) and
    app/api/routes/numbers.py (len(registry)) call those directly on
    request.app.state.carriers. No existing carrier-routing test ever caught a break here:
    conftest.py's _install() helper (every FakeCarrier-backed fixture, e.g. `multi` in
    test_carrier_routing.py) replaces app.state.carriers with a raw CarrierRegistry,
    never the CarrierRegistryProxy app/main.py actually installs."""
    from app.providers.registry import CarrierRegistry

    application = create_app(webhook_settings)
    bandwidth = FakeCarrier(name="bandwidth")
    raw_registry = CarrierRegistry({"bandwidth": bandwidth}, primary="bandwidth")
    proxy = CarrierRegistryProxy(raw_registry)
    application.state.carriers = proxy
    application.state.carrier = bandwidth

    # Direct dunder assertions, no org context bound - matches every caller today (the
    # sweeper, webhooks, and any route before an authenticated request binds one).
    assert len(proxy) == 1
    assert "bandwidth" in proxy
    assert "telnyx" not in proxy
    assert list(proxy) == ["bandwidth"]
    assert bool(proxy) is True
    assert bool(CarrierRegistryProxy(CarrierRegistry({}))) is False

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _number = await make_org_with_number(
            client, "p17-proxy@example.com", "Org Proxy", "+12145550199"
        )
        h = auth_headers(token, org["id"])
        r = await client.post(
            "/api/v1/messages", json={"to": "+19725559999", "body": "hi"}, headers=h
        )
        assert r.status_code == 201, r.text

    assert len(bandwidth.sent) == 1


def test_build_registry_for_org_shares_health_identity_with_global():
    """Security review finding #7: the org registry must share the SAME HealthRegistry
    (and, for any provider this org has no DB account for, the SAME adapter object) as
    the global registry it was built from - not a fresh one - so a circuit-breaker trip
    against a shared/env-configured carrier is the identical fact whether observed
    through this org's registry or the global one."""
    settings = make_settings(
        credentials_master_key=Fernet.generate_key().decode(),
        telnyx_api_key="env-api-key",
        telnyx_enabled=True,
    )
    global_registry = build_registry(settings)
    org_registry, db_owned = build_registry_for_org(
        settings, [], global_registry=global_registry
    )

    assert org_registry.health is global_registry.health
    assert org_registry.get("telnyx") is global_registry.get("telnyx")
    assert db_owned == {}


async def test_carrier_for_account_closes_stale_adapter_on_version_change(client_with_key, session):
    """Security review finding #7: carrier_for_account caches per (account id, its org's
    current version) and must close the OLD adapter, not merely drop it, the moment that
    version moves on (a credential rotation, probe, or disable bumping the org's
    version) - otherwise a rebuilt-and-discarded adapter's own owned httpx client leaks."""
    from app.providers.registry_org import carrier_for_account

    client, app_settings = client_with_key
    token = await register_and_login(client, "p17-acctcache@example.com")
    org = await create_org(client, token, "Org AcctCache")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org["id"])

    account_id = await _post_telnyx(client, headers)

    set_org_context(session, org_id)
    row = await session.get(ProviderAccount, uuid.UUID(account_id))

    first = await carrier_for_account(app_settings, row)
    assert first is not None
    again = await carrier_for_account(app_settings, row)
    assert again is first  # unchanged version - same cached object, not rebuilt

    closed = {"called": False}

    async def spy_aclose():
        closed["called"] = True

    first.aclose = spy_aclose

    patched = await client.patch(
        f"/api/v1/provider-accounts/{account_id}",
        json={"credentials": {"messaging_profile_id": "bumped"}},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    rebuilt = await carrier_for_account(app_settings, row)
    assert rebuilt is not first
    assert closed["called"] is True


async def test_create_label_max_length_enforced(client_with_key):
    """Security review finding #12: label must carry the same max_length the DB column
    (String(127)) enforces, so an over-long label 422s instead of hitting the DB."""
    client, _ = client_with_key
    token = await register_and_login(client, "p17-labellen@example.com")
    org = await create_org(client, token, "Org LabelLen")
    headers = auth_headers(token, org["id"])

    resp = await client.post(
        "/api/v1/provider-accounts",
        json={"provider": "telnyx", "label": "x" * 128, "credentials": TELNYX_ALL},
        headers=headers,
    )
    assert resp.status_code == 422
