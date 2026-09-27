"""Unit + HTTP tests for the ops console "Server" page (whole-host stats + per-app usage)."""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from app.services import server_stats
from tests.conftest import auth_headers, register_and_login
from tests.test_ops_console import _operator, ops, ops_settings  # noqa: F401


@pytest.fixture
async def server_client(engine, ops_settings):  # noqa: F811
    """The app with the server route guaranteed to be registered (idempotently)."""
    from app.api.routes import ops_server
    from app.main import create_app

    application = create_app(ops_settings)
    paths = {getattr(route, "path", None) for route in application.routes}
    if "/api/v1/ops/console/server" not in paths:
        application.include_router(ops_server.router)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _write_stat(
    proc_dir,
    *,
    user: int = 100,
    nice: int = 0,
    system: int = 100,
    idle: int = 800,
    iowait: int = 0,
    irq: int = 0,
    softirq: int = 0,
    steal: int = 0,
) -> None:
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "stat").write_text(
        f"cpu  {user} {nice} {system} {idle} {iowait} {irq} {softirq} {steal} 0 0\n"
        f"cpu0 {user} {nice} {system} {idle} {iowait} {irq} {softirq} {steal} 0 0\n"
        "intr 12345 0 0 0\n"
        "ctxt 67890\n",
        encoding="utf-8",
    )


def _write_proc(
    proc_dir,
    *,
    memtotal_kb: int = 2_000_000,
    memavail_kb: int = 500_000,
    load: str = "0.42 0.30 0.20 1/234 5678",
    uptime: str = "259200.00 1000000.00",
) -> None:
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "meminfo").write_text(
        f"MemTotal:       {memtotal_kb} kB\n"
        "MemFree:        123456 kB\n"
        f"MemAvailable:   {memavail_kb} kB\n"
        "Buffers:        10000 kB\n"
        "Cached:         20000 kB\n",
        encoding="utf-8",
    )
    _write_stat(proc_dir)
    (proc_dir / "loadavg").write_text(f"{load}\n", encoding="utf-8")
    (proc_dir / "uptime").write_text(f"{uptime}\n", encoding="utf-8")


def _host_line(
    name: str,
    *,
    cpu: str = "1.00%",
    mem: str = "10MiB / 1GiB",
    mem_pct: str = "1.00%",
    pids: str = "5",
) -> str:
    return json.dumps(
        {
            "BlockIO": "0B / 0B",
            "CPUPerc": cpu,
            "Container": name,
            "ID": name,
            "MemPerc": mem_pct,
            "MemUsage": mem,
            "Name": name,
            "NetIO": "0B / 0B",
            "PIDs": pids,
        }
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0B", 0),
        ("512B", 512),
        ("512KiB", 512 * 1024),
        ("100.5MiB", int(round(100.5 * 1024**2))),
        ("1.922GiB", int(round(1.922 * 1024**3))),
        ("12kB", 12 * 1000),
        ("1.2MB", int(round(1.2 * 1000**2))),
        ("1GB", 1_000_000_000),
        ("", None),
        ("nonsense", None),
        ("--", None),
        (None, None),
    ],
)
def test_parse_size(text, expected):
    assert server_stats.parse_size(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.23%", 1.23),
        ("5.10%", 5.1),
        ("0%", 0.0),
        ("--", None),
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_parse_percent(text, expected):
    assert server_stats.parse_percent(text) == expected


def test_read_memory(tmp_path):
    proc = tmp_path / "proc"
    _write_proc(proc, memtotal_kb=2_000_000, memavail_kb=500_000)
    mem = server_stats.read_memory(str(proc))
    assert mem is not None
    assert mem["total_bytes"] == 2_000_000 * 1024
    assert mem["available_bytes"] == 500_000 * 1024
    assert mem["used_bytes"] == 1_500_000 * 1024
    assert mem["percent"] == 75.0


def test_read_memory_missing(tmp_path):
    assert server_stats.read_memory(str(tmp_path / "nope")) is None


def test_cpu_percent_between():
    # 100 idle ticks out of 1000 total -> 90% busy.
    assert server_stats.cpu_percent_between((800, 1000), (900, 2000)) == 90.0
    # Nothing but idle -> 0% busy.
    assert server_stats.cpu_percent_between((0, 0), (1000, 1000)) == 0.0
    # Nothing but busy -> 100%.
    assert server_stats.cpu_percent_between((0, 0), (0, 1000)) == 100.0
    # Clock did not tick -> unknown.
    assert server_stats.cpu_percent_between((500, 1000), (500, 1000)) is None


def test_read_load_percent(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _write_proc(proc, load="1.00 0.50 0.25 1/234 5678")
    monkeypatch.setattr(server_stats.os, "cpu_count", lambda: 4)
    load = server_stats.read_load(str(proc))
    assert load is not None
    assert load["one"] == 1.0
    assert load["five"] == 0.5
    assert load["fifteen"] == 0.25
    assert load["cores"] == 4
    assert load["percent"] == 25.0


def test_read_apps_ndjson_sorted_and_skips_garbage(tmp_path):
    host = tmp_path / "host_stats.json"
    host.write_text(
        "\n".join(
            [
                _host_line(
                    "csaas-api-1",
                    cpu="1.23%",
                    mem="100.5MiB / 1.922GiB",
                    mem_pct="5.10%",
                    pids="12",
                ),
                "not json at all",
                _host_line(
                    "csaas-web-1", cpu="9.50%", mem="10MiB / 1GiB", mem_pct="1.00%", pids="7"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    apps = server_stats.read_apps(str(host))
    assert apps["available"] is True
    assert apps["stale"] is False
    assert apps["updated_at"] is not None
    assert apps["age_seconds"] is not None
    assert [c["name"] for c in apps["containers"]] == ["csaas-web-1", "csaas-api-1"]

    api = next(c for c in apps["containers"] if c["name"] == "csaas-api-1")
    assert api["cpu_percent"] == 1.23
    assert api["mem_used_bytes"] == int(round(100.5 * 1024**2))
    assert api["mem_limit_bytes"] == int(round(1.922 * 1024**3))
    assert api["mem_percent"] == 5.1
    assert api["pids"] == 12


def test_read_apps_missing_file(tmp_path):
    apps = server_stats.read_apps(str(tmp_path / "nope.json"))
    assert apps["available"] is False
    assert apps["stale"] is False
    assert apps["updated_at"] is None
    assert apps["age_seconds"] is None
    assert apps["containers"] == []


def test_read_apps_old_mtime_is_stale(tmp_path):
    host = tmp_path / "host_stats.json"
    host.write_text(_host_line("csaas-api-1") + "\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(host, (old, old))
    apps = server_stats.read_apps(str(host))
    assert apps["available"] is True
    assert apps["stale"] is True
    assert apps["age_seconds"] > server_stats.HOST_STATS_STALE_SECONDS


def test_read_apps_json_array_form(tmp_path):
    host = tmp_path / "host_stats.json"
    host.write_text(
        json.dumps(
            [
                {
                    "Name": "one",
                    "CPUPerc": "2.00%",
                    "MemUsage": "1MiB / 2MiB",
                    "MemPerc": "50.00%",
                    "PIDs": "3",
                },
                {
                    "Name": "two",
                    "CPUPerc": "1.00%",
                    "MemUsage": "1MiB / 2MiB",
                    "MemPerc": "50.00%",
                    "PIDs": "--",
                },
            ]
        ),
        encoding="utf-8",
    )
    apps = server_stats.read_apps(str(host))
    assert [c["name"] for c in apps["containers"]] == ["one", "two"]
    assert apps["containers"][0]["pids"] == 3
    assert apps["containers"][1]["pids"] is None


async def test_sample_cpu_percent(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _write_stat(proc, user=100, nice=0, system=100, idle=800, iowait=0, irq=0, softirq=0, steal=0)

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(server_stats.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(server_stats, "_LAST_CPU_SAMPLE", None)

    # No previous sample: with a no-op sleep the two reads see identical counters, so there is
    # nothing to compare and the helper reports unknown.
    assert await server_stats.sample_cpu_percent(str(proc)) is None

    # Age the stored baseline so the next call compares against it instead of paying for a real
    # sleep in the test.
    sampled_at, idle, total = server_stats._LAST_CPU_SAMPLE
    monkeypatch.setattr(server_stats, "_LAST_CPU_SAMPLE", (sampled_at - 5.0, idle, total))

    _write_stat(proc, user=200, nice=0, system=100, idle=900, iowait=0, irq=0, softirq=0, steal=0)
    percent = await server_stats.sample_cpu_percent(str(proc))
    assert isinstance(percent, float)
    assert 0.0 <= percent <= 100.0
    assert percent == 50.0


async def test_server_route_returns_snapshot(server_client, session, tmp_path, monkeypatch):
    token = await _operator(server_client, session, role="reviewer")
    proc = tmp_path / "proc"
    _write_proc(proc, memtotal_kb=2_000_000, memavail_kb=500_000)
    host = tmp_path / "host_stats.json"
    host.write_text(_host_line("csaas-api-1") + "\n", encoding="utf-8")

    monkeypatch.setattr(server_stats, "PROC_ROOT", str(proc))
    monkeypatch.setattr(server_stats, "HOST_STATS_PATH", str(host))
    monkeypatch.setattr(server_stats, "DISK_PATH", str(tmp_path))
    monkeypatch.setattr(server_stats, "_LAST_CPU_SAMPLE", None)

    r = await server_client.get("/api/v1/ops/console/server", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "Whole server (shared with other apps)"
    assert body["memory"]["percent"] == 75.0
    assert body["apps"]["available"] is True
    assert body["apps"]["containers"][0]["name"] == "csaas-api-1"


async def test_server_route_requires_operator(server_client):
    r = await server_client.get("/api/v1/ops/console/server")
    assert r.status_code in (401, 403), r.status_code

    token = await register_and_login(server_client, "plain@example.com")
    r = await server_client.get("/api/v1/ops/console/server", headers=auth_headers(token))
    assert r.status_code in (401, 403), r.status_code


async def test_server_route_missing_apps_file(server_client, session, tmp_path, monkeypatch):
    token = await _operator(server_client, session, email="reviewer2@example.com", role="reviewer")
    proc = tmp_path / "proc"
    _write_proc(proc)
    monkeypatch.setattr(server_stats, "PROC_ROOT", str(proc))
    monkeypatch.setattr(server_stats, "HOST_STATS_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(server_stats, "DISK_PATH", str(tmp_path))
    monkeypatch.setattr(server_stats, "_LAST_CPU_SAMPLE", None)

    r = await server_client.get("/api/v1/ops/console/server", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["apps"]["available"] is False
