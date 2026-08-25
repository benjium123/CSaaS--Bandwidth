from __future__ import annotations

import os
import uuid

import httpx
import pytest

import app.models  # noqa: F401 - populates Base.metadata
from app.config import Settings
from app.db.base import Base
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
IS_SQLITE = TEST_DB_URL.startswith("sqlite")

TEST_JWT_SECRET = "test-jwt-secret-not-a-real-one-padded-to-32+bytes"


def pytest_collection_modifyitems(config, items):
    """GUARD 3: pg_only tests are skipped unless the backend really is Postgres."""
    if IS_SQLITE:
        skip = pytest.mark.skip(reason="requires PostgreSQL (TEST_DATABASE_URL not set to pg)")
        for item in items:
            if "pg_only" in item.keywords:
                item.add_marker(skip)


def make_settings(**overrides) -> Settings:
    base = {
        "app_env": "test",
        "jwt_secret": TEST_JWT_SECRET,
        "session_secret": "test-session-secret",
        "database_url": TEST_DB_URL,
        "cors_origins": "http://localhost:5173",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def engine(settings: Settings):
    eng = init_engine(settings.database_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await dispose_engine()


@pytest.fixture
async def session(engine):
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def client(engine, settings: Settings):
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
async def register_and_login(
    client: httpx.AsyncClient, email: str, password: str = "correct-horse-battery"
) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth_headers(token: str, org_id: uuid.UUID | str | None = None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if org_id is not None:
        h["X-Org-Id"] = str(org_id)
    return h


async def create_org(client: httpx.AsyncClient, token: str, name: str) -> dict:
    r = await client.post("/api/v1/orgs", json={"name": name}, headers=auth_headers(token))
    assert r.status_code == 201, r.text
    return r.json()
