"""Phase 14 DR-8: the public, unauthenticated status surface.

Component NAMES and up/degraded/down/unconfigured ONLY - no version, no counts, no
hostnames, no carrier account detail (that stays authenticated, at
GET /api/v1/routing/carriers). Cheap probes, cached in-process for CACHE_SECONDS.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.api.routes import status as status_routes
from app.main import create_app
from app.providers.domain import CarrierError
from app.providers.health import FAILURE_THRESHOLD, HealthRegistry
from app.providers.registry import CarrierRegistry
from tests.conftest import FakeCarrier, make_settings

ALLOWED_STATUSES = {"up", "degraded", "down", "unconfigured"}


@pytest.fixture
async def client(engine, settings):
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, application


# ==================================================================================
# Shape, auth, secrecy
# ==================================================================================
async def test_status_is_unauthenticated(client):
    c, _ = client
    r = await c.get("/status")  # no Authorization / X-Org-Id headers at all
    assert r.status_code == 200, r.text


async def test_status_shape_is_stable(client):
    c, _ = client
    r = await c.get("/status")
    body = r.json()
    assert set(body.keys()) == {"status", "components"}
    assert body["status"] in ("ok", "degraded", "down")
    components = body["components"]
    assert set(components.keys()) == {"api", "db", "redis", "carriers", "media_plane"}
    assert components["api"] == "up"
    for key in ("db", "redis", "media_plane"):
        assert components[key] in ALLOWED_STATUSES, f"{key}={components[key]!r}"
    assert isinstance(components["carriers"], dict)
    for name, state in components["carriers"].items():
        assert state in ALLOWED_STATUSES, f"carrier {name}={state!r}"


async def test_status_never_leaks_a_secret_ish_field(client):
    c, _ = client
    r = await c.get("/status")
    blob = json.dumps(r.json()).lower()
    for forbidden in ("secret", "token", "password", "api_key", "apikey", "version"):
        assert forbidden not in blob, f"leaked {forbidden!r}: {blob}"


async def test_status_reports_media_plane_unconfigured_by_default(client):
    """No LIVEKIT_URL/LIVEKIT_API_SECRET -> app.state.livekit is None -> unconfigured, not
    a probed failure."""
    c, _ = client
    r = await c.get("/status")
    assert r.json()["components"]["media_plane"] == "unconfigured"


async def test_status_reports_redis_unconfigured_when_no_redis_url(engine):
    settings = make_settings(redis_url="")
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/status")
    assert r.json()["components"]["redis"] == "unconfigured"


# ==================================================================================
# Degraded when a breaker is open
# ==================================================================================
async def test_status_degrades_when_a_carrier_breaker_is_open(engine, webhook_settings):
    application = create_app(webhook_settings)
    bandwidth = FakeCarrier(name="bandwidth")
    telnyx = FakeCarrier(name="telnyx")
    health = HealthRegistry()
    registry = CarrierRegistry(
        {"bandwidth": bandwidth, "telnyx": telnyx}, primary="bandwidth", health=health
    )
    application.state.carriers = registry

    breaker = health.breaker("telnyx")
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure(CarrierError("carrier_transient", None, retryable=True))
    assert breaker.state() == "open"

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/status")
    body = r.json()
    assert body["components"]["carriers"]["telnyx"] == "down"
    assert body["components"]["carriers"]["bandwidth"] == "up"
    assert body["status"] == "degraded", "one open breaker degrades overall, but does not down it"


async def test_status_carriers_empty_when_no_registry(client):
    c, _ = client
    r = await c.get("/status")
    assert r.json()["components"]["carriers"] == {}


# ==================================================================================
# Overall aggregation (pure function - exercised directly, no need to break a real DB)
# ==================================================================================
def test_overall_is_down_only_when_db_is_down():
    assert status_routes._overall("down", "up", {}, "up") == "down"
    assert (
        status_routes._overall("down", "unconfigured", {"bandwidth": "down"}, "unconfigured")
        == "down"
    )


def test_overall_is_degraded_on_any_non_db_trouble():
    assert status_routes._overall("up", "down", {}, "up") == "degraded"
    assert status_routes._overall("up", "up", {}, "down") == "degraded"
    assert status_routes._overall("up", "up", {"bandwidth": "degraded"}, "up") == "degraded"


def test_overall_is_ok_when_everything_up_or_unconfigured():
    assert status_routes._overall("up", "unconfigured", {"bandwidth": "up"}, "unconfigured") == "ok"
    assert status_routes._overall("up", "up", {}, "up") == "ok"


# ==================================================================================
# In-process cache
# ==================================================================================
async def test_status_response_is_cached_for_the_window(client, monkeypatch):
    c, application = client
    calls = {"n": 0}
    real_probe_db = status_routes._probe_db

    async def counting_probe_db():
        calls["n"] += 1
        return await real_probe_db()

    monkeypatch.setattr(status_routes, "_probe_db", counting_probe_db)

    first = await c.get("/status")
    second = await c.get("/status")
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1, "the second call within the cache window must not re-probe"

    # Cache lives on app.state, not a module global - it must not survive a fresh app.
    cache = getattr(application.state, "_status_cache", None)
    assert cache is not None and cache["expires_at"] > 0


async def test_status_cache_does_not_leak_across_apps(engine, settings):
    """The cache is per-app-instance (app.state), never a module-level global - two
    separate app instances must never share a status result."""
    app_a = create_app(settings)
    app_b = create_app(settings)
    transport_a = httpx.ASGITransport(app=app_a)
    transport_b = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport_a, base_url="http://test") as ca:
        await ca.get("/status")
    assert getattr(app_a.state, "_status_cache", None) is not None
    assert getattr(app_b.state, "_status_cache", None) is None
    async with httpx.AsyncClient(transport=transport_b, base_url="http://test") as cb:
        r = await cb.get("/status")
    assert r.status_code == 200


# ==================================================================================
# Single-flight (Opus review): a cache MISS must not let concurrent requests each fire
# their own probe set.
# ==================================================================================
async def test_status_single_flights_concurrent_probes_on_a_cache_miss(client, monkeypatch):
    c, _application = client
    calls = {"n": 0}
    real_probe_db = status_routes._probe_db

    async def slow_counting_probe_db():
        calls["n"] += 1
        await asyncio.sleep(0.05)  # widen the race window so both requests overlap
        return await real_probe_db()

    monkeypatch.setattr(status_routes, "_probe_db", slow_counting_probe_db)

    first, second = await asyncio.gather(c.get("/status"), c.get("/status"))
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1, "two concurrent requests on a cache miss must probe only once"


# ==================================================================================
# Serve stale on a probe error (Opus review).
# ==================================================================================
async def test_status_serves_stale_body_when_a_fresh_probe_errors(client, monkeypatch):
    c, application = client

    first = await c.get("/status")
    assert first.status_code == 200
    stale_body = first.json()

    # Force the cache to look expired, then make the NEXT probe attempt blow up.
    application.state._status_cache["expires_at"] = 0.0

    def boom(*_a, **_kw):
        raise RuntimeError("simulated probe crash")

    monkeypatch.setattr(status_routes, "_carrier_states", boom)

    second = await c.get("/status")
    assert second.status_code == 200, "a probe bug must never turn into a 500 here"
    assert second.json() == stale_body, "must serve the last-known-good body, not crash"


async def test_status_reraises_when_there_is_no_stale_cache_to_fall_back_on(
    engine, settings, monkeypatch
):
    """No prior successful call ever happened, so there is nothing stale to serve - this
    is the one case where a probe bug is allowed to surface as a real error."""
    application = create_app(settings)

    def boom(*_a, **_kw):
        raise RuntimeError("simulated probe crash")

    monkeypatch.setattr(status_routes, "_carrier_states", boom)

    # raise_app_exceptions=False: let the app's OWN global exception handler convert this
    # into a 500 response (the default True makes httpx's test transport re-raise server
    # errors instead, which is useful for debugging but not what this test is checking).
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/status")
    assert r.status_code == 500


# ==================================================================================
# Redis probe: any RESP reply (success OR error) means reachable.
# ==================================================================================
async def test_redis_probe_treats_a_resp_error_reply_as_up():
    """A redis that demands auth we did not send answers "-NOAUTH ..." - that is a real,
    reachable redis, not a down one. Only a connection failure/timeout is down."""

    async def handle(reader, writer):
        await reader.readline()
        writer.write(b"-NOAUTH Authentication required.\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    async with server:
        result = await status_routes._probe_redis(f"redis://{host}:{port}/0")
    assert result == "up"


async def test_redis_probe_treats_pong_as_up():
    async def handle(reader, writer):
        await reader.readline()
        writer.write(b"+PONG\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    async with server:
        result = await status_routes._probe_redis(f"redis://{host}:{port}/0")
    assert result == "up"


async def test_redis_probe_is_down_on_connection_failure():
    # Nothing listening on this port - a connection attempt must fail fast, not hang.
    result = await status_routes._probe_redis("redis://127.0.0.1:1/0")
    assert result == "down"
