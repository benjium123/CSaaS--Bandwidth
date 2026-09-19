from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.auth.security import create_access_token
from tests.conftest import TEST_JWT_SECRET, auth_headers, register_and_login


async def test_register_login_me(client):
    token = await register_and_login(client, "alice@example.com")
    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"
    # A self-serve signup now leaves registration owning a workspace (auth.py::register),
    # so /me reports exactly one membership where it used to report none. The count is the
    # assertion that matters: two would mean signup created a workspace on top of one the
    # account already had.
    assert len(r.json()["memberships"]) == 1
    assert r.json()["memberships"][0]["role_name"] == "owner"


async def test_wrong_password_is_401(client):
    await register_and_login(client, "bob@example.com")
    r = await client.post(
        "/api/v1/auth/login", json={"email": "bob@example.com", "password": "wrong-password"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


async def test_unknown_email_is_same_401(client):
    r = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-long"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


async def test_email_is_normalized(client):
    await register_and_login(client, "case@example.com")
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "CASE@Example.COM", "password": "correct-horse-battery"},
    )
    assert r.status_code == 200


async def test_duplicate_registration_is_409(client):
    await register_and_login(client, "dup@example.com")
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "dup@example.com", "password": "correct-horse-battery"},
    )
    assert r.status_code == 409


async def test_password_is_argon2(client, session):
    await register_and_login(client, "hash@example.com")
    from app.repositories import users as users_repo

    user = await users_repo.get_by_email(session, "hash@example.com")
    assert user.hashed_password.startswith("$argon2id$")


async def test_forged_jwt_rejected(client):
    other_secret = "a-completely-different-secret-also-32-bytes-long"
    forged = create_access_token(uuid.uuid4(), other_secret)
    r = await client.get("/api/v1/auth/me", headers=auth_headers(forged))
    assert r.status_code == 401


async def test_expired_jwt_rejected(client):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": int(past.timestamp())},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )
    r = await client.get("/api/v1/auth/me", headers=auth_headers(expired))
    assert r.status_code == 401


async def test_no_token_is_401(client):
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
