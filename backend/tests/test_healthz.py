from __future__ import annotations


async def test_healthz_green(client, settings):
    r = await client.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    # Bugfix ledger 4.23: an unauthenticated liveness probe must not leak deployment
    # fingerprinting detail - env/version are gone from the response.
    assert "env" not in body
    assert "version" not in body
    assert r.headers.get("X-Request-Id")


async def test_request_id_is_echoed_when_supplied(client):
    r = await client.get("/healthz", headers={"X-Request-Id": "abc-123"})
    assert r.headers["X-Request-Id"] == "abc-123"


async def test_healthz_needs_no_auth(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
