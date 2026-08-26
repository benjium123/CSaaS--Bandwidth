from __future__ import annotations

import time

import pyotp
import pytest
from cryptography.fernet import Fernet

from tests.conftest import auth_headers, make_settings, register_and_login

FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def twofa_settings():
    return make_settings(credential_encryption_key=FERNET_KEY)


@pytest.fixture
async def twofa_client(engine, twofa_settings):
    import httpx

    from app.main import create_app

    application = create_app(twofa_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _enroll_and_activate(client, token) -> str:
    enroll = await client.post("/api/v1/auth/2fa/enroll", headers=auth_headers(token))
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    assert enroll.json()["provisioning_uri"].startswith("otpauth://totp/")

    code = pyotp.TOTP(secret).now()
    activate = await client.post(
        "/api/v1/auth/2fa/activate", json={"code": code}, headers=auth_headers(token)
    )
    assert activate.status_code == 200, activate.text
    assert activate.json()["totp_enabled"] is True
    return secret


async def test_enroll_activate_login_verify(twofa_client):
    client = twofa_client
    token = await register_and_login(client, "tf1@example.com")
    secret = await _enroll_and_activate(client, token)

    # Password alone is no longer a login.
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "tf1@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 200
    assert login.json()["requires_2fa"] is True
    assert login.json()["access_token"] is None
    pending = login.json()["pending_token"]
    assert pending

    # Wait for a fresh timestep so the activation code is not replayed.
    time.sleep(31 - (int(time.time()) % 30))
    verify = await client.post(
        "/api/v1/auth/2fa/verify",
        json={"pending_token": pending, "code": pyotp.TOTP(secret).now()},
    )
    assert verify.status_code == 200, verify.text
    real_token = verify.json()["access_token"]

    me = await client.get("/api/v1/auth/me", headers=auth_headers(real_token))
    assert me.status_code == 200
    assert me.json()["email"] == "tf1@example.com"


async def test_pending_token_is_not_an_access_token(twofa_client):
    """The whole point: 'password correct, second factor pending' must not be a login."""
    client = twofa_client
    token = await register_and_login(client, "tf2@example.com")
    await _enroll_and_activate(client, token)

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "tf2@example.com", "password": "correct-horse-battery"},
    )
    pending = login.json()["pending_token"]

    me = await client.get("/api/v1/auth/me", headers=auth_headers(pending))
    assert me.status_code == 401


async def test_wrong_code_rejected(twofa_client):
    client = twofa_client
    token = await register_and_login(client, "tf3@example.com")
    await _enroll_and_activate(client, token)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "tf3@example.com", "password": "correct-horse-battery"},
    )
    r = await client.post(
        "/api/v1/auth/2fa/verify",
        json={"pending_token": login.json()["pending_token"], "code": "000000"},
    )
    assert r.status_code == 401


async def test_code_replay_rejected(twofa_client):
    client = twofa_client
    token = await register_and_login(client, "tf4@example.com")
    secret = await _enroll_and_activate(client, token)

    time.sleep(31 - (int(time.time()) % 30))
    code = pyotp.TOTP(secret).now()

    first_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "tf4@example.com", "password": "correct-horse-battery"},
    )
    ok = await client.post(
        "/api/v1/auth/2fa/verify",
        json={"pending_token": first_login.json()["pending_token"], "code": code},
    )
    assert ok.status_code == 200

    second_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "tf4@example.com", "password": "correct-horse-battery"},
    )
    replay = await client.post(
        "/api/v1/auth/2fa/verify",
        json={"pending_token": second_login.json()["pending_token"], "code": code},
    )
    assert replay.status_code == 401, "a used code must not work twice"


async def test_secret_is_encrypted_at_rest(twofa_client, session):
    client = twofa_client
    token = await register_and_login(client, "tf5@example.com")
    secret = await _enroll_and_activate(client, token)

    from app.repositories import users as users_repo

    user = await users_repo.get_by_email(session, "tf5@example.com")
    await session.refresh(user)
    assert user.totp_secret
    assert user.totp_secret != secret, "the base32 secret must not be stored in the clear"
    assert Fernet(FERNET_KEY.encode()).decrypt(user.totp_secret.encode()).decode() == secret


async def test_enroll_without_fernet_key_is_503(client):
    """No plaintext fallback branch: without the key, the feature is simply unavailable."""
    token = await register_and_login(client, "tf6@example.com")
    r = await client.post("/api/v1/auth/2fa/enroll", headers=auth_headers(token))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "feature_unavailable"


async def test_disable_requires_a_valid_code(twofa_client):
    client = twofa_client
    token = await register_and_login(client, "tf7@example.com")
    secret = await _enroll_and_activate(client, token)

    bad = await client.post(
        "/api/v1/auth/2fa/disable", json={"code": "000000"}, headers=auth_headers(token)
    )
    assert bad.status_code == 401

    time.sleep(31 - (int(time.time()) % 30))
    good = await client.post(
        "/api/v1/auth/2fa/disable",
        json={"code": pyotp.TOTP(secret).now()},
        headers=auth_headers(token),
    )
    assert good.status_code == 200
    assert good.json()["totp_enabled"] is False

    # Login is a plain login again.
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "tf7@example.com", "password": "correct-horse-battery"},
    )
    assert login.json()["requires_2fa"] is False
    assert login.json()["access_token"]
