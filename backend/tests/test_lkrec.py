"""Tests for app/services/lkrec.py (self-hosted LiveKit recorder client)."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from pydantic import SecretStr

from app.services import calling_settings, lkrec
from app.storage.base import InMemoryObjectStore
from tests.test_p43_call_monitoring import (  # noqa: F401
    _finished_call,
    _org,
    mon_settings,
)


# asyncio_mode=auto is active in this repo; every async test that talks to the DB or
# to lkrec works with the existing conftest fixtures without an explicit marker. No
# blanket pytestmark here - test_role_for below is a plain sync test.


def _settings(mon_settings, tmp_path, **extra):
    update = {
        "monitor_recorder_url": "http://lkrec:9099",
        "monitor_recorder_dir": str(tmp_path),
        "elevenlabs_api_key": SecretStr("el-test"),
        "deepgram_api_key": SecretStr("dg-test"),
    }
    update.update(extra)
    return mon_settings.model_copy(update=update)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _entry(kind: str, identity: str, prefix: str) -> dict:
    return {"kind": kind, "identity": identity, "file": f"{prefix}__{identity}.ogg"}


def _store_keys(store) -> set[str]:
    for attr in ("objects", "data", "blobs", "_objects", "_data", "_blobs"):
        mapping = getattr(store, attr, None)
        if isinstance(mapping, dict):
            return set(mapping)
    raise AssertionError("cannot enumerate InMemoryObjectStore keys")


def _mock_el(monkeypatch, text: str) -> None:
    monkeypatch.setattr(
        calling_settings, "announcement_text_for", lambda *_a, **_k: text
    )


async def test_api_token_is_sha256_of_prefixed_secret(mon_settings, tmp_path):
    settings = _settings(
        mon_settings, tmp_path, livekit_api_secret=SecretStr("s3cret")
    )
    assert lkrec.api_token(settings) == hashlib.sha256(b"lkrec:s3cret").hexdigest()


def test_role_for():
    assert lkrec.role_for({"kind": "sip", "identity": "sip_1"}) == "customer"
    assert lkrec.role_for({"kind": "standard", "identity": "user-1"}) == "agent"
    assert lkrec.role_for({"kind": "agent", "identity": "bot-1"}) is None
    assert lkrec.role_for({"kind": "unknown", "identity": "sip_123"}) == "customer"
    assert lkrec.role_for({"kind": "unknown", "identity": "user-1"}) == "agent"


async def test_start_sends_bearer_and_body(mon_settings, tmp_path):
    settings = _settings(mon_settings, tmp_path)
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with _client(capture) as client:
        ok = await lkrec.start(
            settings, "call-1", announcement="a.ogg", resume=True, client=client
        )
        assert ok is True

    assert seen[-1].url.path == "/start"
    assert seen[-1].headers["authorization"] == f"Bearer {lkrec.api_token(settings)}"
    assert json.loads(seen[-1].content) == {
        "room": "call-1",
        "announcement": "a.ogg",
        "resume": True,
    }

    async with _client(lambda request: httpx.Response(500)) as client:
        assert await lkrec.start(settings, "call-1", client=client) is False

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(boom) as client:
        assert await lkrec.start(settings, "call-1", client=client) is False


async def test_ensure_announcement_renders_once_and_caches(
    session, mon_settings, tmp_path, monkeypatch
):
    org = await _org(session)
    settings = _settings(mon_settings, tmp_path)
    _mock_el(monkeypatch, "This call may be recorded.")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"OggS" + b"x" * 100)

    async with _client(handler) as client:
        name = await lkrec.ensure_announcement(settings, org, client=client)
        assert name.endswith(".ogg")
        cached = tmp_path / "announce" / name
        assert cached.read_bytes() == b"OggS" + b"x" * 100
        assert await lkrec.ensure_announcement(settings, org, client=client) == name
        assert len(requests) == 1

        _mock_el(monkeypatch, "   ")
        assert await lkrec.ensure_announcement(settings, org, client=client) == ""
        assert len(requests) == 1


async def test_ensure_announcement_rejects_non_ogg(
    session, mon_settings, tmp_path, monkeypatch
):
    org = await _org(session)
    settings = _settings(mon_settings, tmp_path)
    _mock_el(monkeypatch, "This call may be recorded.")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ID3" + b"y" * 100)

    async with _client(handler) as client:
        with pytest.raises(lkrec.AnnouncementUnavailable):
            await lkrec.ensure_announcement(settings, org, client=client)
    assert not (tmp_path / "announce").exists()


async def test_ingest_moves_files_into_store_and_is_idempotent(
    session, mon_settings, tmp_path
):
    org = await _org(session)
    call = await _finished_call(session, org)
    settings = _settings(mon_settings, tmp_path)
    store = InMemoryObjectStore()
    prefix = f"call-{call.id}__1790000000"
    manifest = {
        "room": f"call-{call.id}",
        "started_at": "2026-08-01T09:59:00Z",
        "ended_at": "2026-08-01T10:05:00Z",
        "files": [
            _entry("sip", "sip_1", prefix),
            _entry("standard", "user-1", prefix),
            _entry("agent", "bot-1", prefix),
        ],
    }

    def write_artifacts() -> None:
        (tmp_path / f"{prefix}__sip_1.ogg").write_bytes(b"OggS" + b"a" * 50)
        (tmp_path / f"{prefix}__user-1.ogg").write_bytes(b"OggS" + b"b" * 50)
        (tmp_path / f"{prefix}.json").write_text(json.dumps(manifest))

    write_artifacts()
    counts = await lkrec.ingest_tick(session, settings, store)
    assert counts == {"discarded": 0, "duplicate": 0, "ingested": 1}

    base = f"org/{call.org_id}/monitor/{call.id}/{prefix}"
    assert _store_keys(store) == {
        f"{base}/customer-sip-1.ogg",
        f"{base}/agent-user-1.ogg",
    }
    data = await store.get(f"{base}/customer-sip-1.ogg")
    assert data == b"OggS" + b"a" * 50

    await session.refresh(call)
    recordings = call.extra["monitor_recordings"]
    assert len(recordings) == 1
    assert len(recordings[0]["files"]) == 2
    assert {entry["role"] for entry in recordings[0]["files"]} == {
        "customer",
        "agent",
    }
    assert not (tmp_path / f"{prefix}.json").exists()
    assert not (tmp_path / f"{prefix}__sip_1.ogg").exists()
    assert not (tmp_path / f"{prefix}__user-1.ogg").exists()

    write_artifacts()
    counts = await lkrec.ingest_tick(session, settings, store)
    assert counts == {"discarded": 0, "duplicate": 1, "ingested": 0}
    await session.refresh(call)
    assert len(call.extra["monitor_recordings"]) == 1
    assert len(_store_keys(store)) == 2


async def test_ingest_discards_non_call_rooms(session, mon_settings, tmp_path):
    settings = _settings(mon_settings, tmp_path)
    store = InMemoryObjectStore()
    audio = tmp_path / "lkrec-5-1__1790000000.ogg"
    audio.write_bytes(b"OggS" + b"c" * 50)
    manifest_path = tmp_path / "lkrec-5-1__1790000000.json"
    manifest_path.write_text(
        json.dumps(
            {
                "room": "lkrec-5-1",
                "files": [{"kind": "sip", "identity": "sip_1", "file": audio.name}],
            }
        )
    )

    counts = await lkrec.ingest_tick(session, settings, store)
    assert counts == {"discarded": 1, "duplicate": 0, "ingested": 0}
    assert not audio.exists()
    assert not manifest_path.exists()
    assert _store_keys(store) == set()


async def test_ingest_rejects_path_traversal(session, mon_settings, tmp_path):
    org = await _org(session)
    call = await _finished_call(session, org)
    settings = _settings(mon_settings, tmp_path)
    store = InMemoryObjectStore()
    manifest_path = tmp_path / f"call-{call.id}__1790000000.json"
    manifest_path.write_text(
        json.dumps(
            {
                "room": f"call-{call.id}",
                "files": [
                    {"kind": "sip", "identity": "sip_1", "file": "../evil.ogg"}
                ],
            }
        )
    )

    counts = await lkrec.ingest_tick(session, settings, store)
    assert counts == {"discarded": 0, "duplicate": 0, "ingested": 1}
    assert _store_keys(store) == set()
    assert not (tmp_path.parent / "evil.ogg").exists()
    await session.refresh(call)
    assert call.extra["monitor_recordings"][0]["files"] == []
