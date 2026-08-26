from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

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


# ----------------------------------------------------------------------------------
# FROZEN COMPLIANCE CLOCK  (P3 DR-4)
# ----------------------------------------------------------------------------------
# Quiet hours are evaluated in the RECIPIENT's local time, so without this every send
# test in the suite would pass or fail depending on the wall-clock hour CI happened to
# run at. 2026-06-15 18:00Z is 13:00 CDT / 14:00 EDT / 11:00 PDT / 08:00 HST - inside the
# allowed window in every zone the fixtures use.
FROZEN_NOW = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_compliance_clock(monkeypatch):
    from app.compliance import quiet_hours

    monkeypatch.setattr(quiet_hours, "_now", lambda: FROZEN_NOW)
    return FROZEN_NOW


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
        # The sweeper is an interim in-process loop; tests drive its functions directly.
        "sweeper_enabled": False,
        "media_store_backend": "memory",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def engine(settings: Settings):
    eng = init_engine(settings.database_url)
    if IS_SQLITE:
        # SQLite ignores foreign keys unless asked. Without this, ON DELETE SET NULL /
        # CASCADE silently do nothing locally while working on Postgres - so the local
        # suite would pass on referential behaviour it never actually exercised.
        from sqlalchemy import event

        @event.listens_for(eng.sync_engine, "connect")
        def _fk_on(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

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


# ==================================================================================
# P1 additions — fixtures, FakeCarrier, webhook helpers
# ==================================================================================
import json  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402

from app.providers.domain import (  # noqa: E402
    CarrierCapabilities,
    OutboundMessage,
    SendResult,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bandwidth"

WEBHOOK_USER = "bw-hook-user"
WEBHOOK_PASS = "bw-hook-pass"


def load_fixture(name: str) -> list | dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def webhook_auth_headers(user: str = WEBHOOK_USER, password: str = WEBHOOK_PASS) -> dict:
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


@dataclass
class FakeCarrier:
    """Records every OutboundMessage and returns scripted SendResults."""

    name: str = "bandwidth"
    capabilities: CarrierCapabilities = field(default_factory=CarrierCapabilities)
    sent: list = field(default_factory=list)
    scripted: list = field(default_factory=list)
    default_result: SendResult = field(
        default_factory=lambda: SendResult("accepted", "1755000000000-outbound-bbbb", None)
    )

    async def send_message(self, msg: OutboundMessage) -> SendResult:
        self.sent.append(msg)
        if self.scripted:
            return self.scripted.pop(0)
        if len(self.sent) == 1:
            # The FIRST send keeps the fixture id so the webhook fixtures match it.
            return self.default_result
        # Subsequent sends get unique ids, like a real carrier - otherwise they collide
        # on uq_messages_provider_id.
        base = self.default_result
        return SendResult(base.status, f"{base.provider_message_id}-{len(self.sent)}", None)

    def verify_webhook(self, headers, raw_body) -> bool:  # pragma: no cover - unused
        return True

    def parse_webhook(self, raw_body):  # pragma: no cover - unused
        from app.providers.bandwidth import webhooks

        return webhooks.parse(raw_body)


@pytest.fixture
def webhook_settings() -> Settings:
    return make_settings(
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
    )


@pytest.fixture
async def app_with_carrier(engine, webhook_settings):
    """App wired with a FakeCarrier. Returns (client, fake_carrier)."""
    application = create_app(webhook_settings)
    fake = FakeCarrier()
    application.state.carrier = fake
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


async def make_org_with_number(
    client: httpx.AsyncClient, email: str, org_name: str, e164: str
) -> tuple[str, dict, dict]:
    """register -> login -> create org -> add a number. Returns (token, org, number)."""
    token = await register_and_login(client, email)
    org = await create_org(client, token, org_name)
    r = await client.post(
        "/api/v1/numbers", json={"e164": e164}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    return token, org, r.json()


# ==================================================================================
# P2 additions — query counter, loopback carrier, contact/inbox helpers
# ==================================================================================
from app.providers.loopback import LoopbackCarrier  # noqa: E402


class QueryCounter:
    """Counts SQL statements. The N+1 gate depends on this being honest."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)

    def reset(self) -> None:
        self.statements.clear()


@pytest.fixture
def query_counter(engine):
    from sqlalchemy import event

    counter = QueryCounter()

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter.statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _before)
    yield counter
    event.remove(engine.sync_engine, "before_cursor_execute", _before)


@pytest.fixture
async def app_with_loopback(engine, webhook_settings):
    """App wired with a deterministic LoopbackCarrier (auto=False → drive via drain())."""
    application = create_app(webhook_settings)
    carrier = LoopbackCarrier(auto=False)
    application.state.carrier = carrier
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, carrier, application


async def create_contact(
    client: httpx.AsyncClient, token: str, org_id, name: str, phones: list[str] | None = None
) -> dict:
    body: dict = {"display_name": name, "phones": []}
    for i, p in enumerate(phones or []):
        body["phones"].append({"e164": p, "label": "mobile", "is_primary": i == 0})
    r = await client.post("/api/v1/contacts", json=body, headers=auth_headers(token, org_id))
    assert r.status_code == 201, r.text
    return r.json()


async def create_tag(client: httpx.AsyncClient, token: str, org_id, name: str) -> dict:
    r = await client.post("/api/v1/tags", json={"name": name}, headers=auth_headers(token, org_id))
    assert r.status_code == 201, r.text
    return r.json()
