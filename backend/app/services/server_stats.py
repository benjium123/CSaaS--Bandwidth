"""Whole-host resource stats for the ops console "Server" page.

The api container runs WITHOUT extra privileges and there is no lxcfs, so inside it
``/proc/meminfo``, ``/proc/stat``, ``/proc/loadavg``, ``/proc/uptime`` and the disk usage of
``/`` already describe the WHOLE physical host - read them directly. Never use the docker
socket, psutil or subprocess.

Per-container usage comes from a host job that every 15 s writes the output of
``docker stats --no-stream --format '{{json .}}'`` (NDJSON: one JSON object per line) to a file
mounted READ-ONLY into the api container.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone

PROC_ROOT = os.environ.get("SERVER_STATS_PROC_ROOT", "/proc")
DISK_PATH = os.environ.get("SERVER_STATS_DISK_PATH", "/")
HOST_STATS_PATH = os.environ.get("SERVER_STATS_HOST_FILE", "/app/var/host_stats/host_stats.json")
HOST_STATS_STALE_SECONDS = 60

# (monotonic_time, idle_all, total) of the last CPU sample we took, so a frequent caller (the
# ops console polls every 5 s) does not pay a fresh 0.25 s sleep on every request.
_LAST_CPU_SAMPLE: tuple[float, int, int] | None = None

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)?\s*$")
_PERCENT_RE = re.compile(r"^\s*(-?[0-9]+(?:\.[0-9]+)?)\s*%\s*$")
_BINARY_UNITS = {"KI": 1024, "MI": 1024**2, "GI": 1024**3, "TI": 1024**4, "PI": 1024**5}
_DECIMAL_UNITS = {"K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5}


def _first_int(text: str) -> int | None:
    match = re.search(r"-?[0-9]+", text)
    return int(match.group()) if match else None


def _proc_root(proc_root: str | None) -> str:
    return PROC_ROOT if proc_root is None else proc_root


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def read_memory(proc_root: str | None = None) -> dict | None:
    """Parse ``meminfo`` (kB) into bytes; used = total - MemAvailable."""
    text = _read_text(os.path.join(_proc_root(proc_root), "meminfo"))
    if text is None:
        return None
    total_kb = None
    available_kb = None
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key == "MemTotal":
            total_kb = _first_int(rest)
        elif key == "MemAvailable":
            available_kb = _first_int(rest)
    if total_kb is None or available_kb is None or total_kb <= 0:
        return None
    total_bytes = total_kb * 1024
    available_bytes = available_kb * 1024
    used_bytes = total_bytes - available_bytes
    return {
        "total_bytes": total_bytes,
        "available_bytes": available_bytes,
        "used_bytes": used_bytes,
        "percent": round(used_bytes / total_bytes * 100, 1),
    }


def read_cpu_times(proc_root: str | None = None) -> tuple[int, int] | None:
    """(idle_all, total) from the first ``cpu `` line of ``stat``.

    idle_all = idle + iowait; total = sum of user, nice, system, idle, iowait, irq, softirq,
    steal.
    """
    text = _read_text(os.path.join(_proc_root(proc_root), "stat"))
    if text is None:
        return None
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        try:
            values = [int(part) for part in parts[1:9]]
        except ValueError:
            return None
        if len(values) < 8:
            return None
        idle_all = values[3] + values[4]
        return idle_all, sum(values)
    return None


def cpu_percent_between(a: tuple[int, int], b: tuple[int, int]) -> float | None:
    """Busy percentage between two (idle_all, total) samples; None when the clock did not tick."""
    delta_total = b[1] - a[1]
    if delta_total <= 0:
        return None
    delta_idle = b[0] - a[0]
    percent = 100.0 * (1.0 - delta_idle / delta_total)
    return round(max(0.0, min(100.0, percent)), 1)


async def sample_cpu_percent(proc_root: str | None = None) -> float | None:
    """CPU percentage for the whole host.

    Reuses the module-level last sample when it is 1..60 s old (cheap for a polling caller);
    otherwise takes two samples 0.25 s apart. Always stores the newest sample.
    """
    global _LAST_CPU_SAMPLE
    now = time.monotonic()
    previous = _LAST_CPU_SAMPLE
    if previous is not None and 1.0 <= now - previous[0] <= 60.0:
        current = read_cpu_times(proc_root)
        if current is None:
            return None
        _LAST_CPU_SAMPLE = (now, current[0], current[1])
        return cpu_percent_between((previous[1], previous[2]), current)

    first = read_cpu_times(proc_root)
    if first is None:
        return None
    await asyncio.sleep(0.25)
    second = read_cpu_times(proc_root)
    if second is None:
        return None
    _LAST_CPU_SAMPLE = (time.monotonic(), second[0], second[1])
    return cpu_percent_between(first, second)


def read_load(proc_root: str | None = None) -> dict | None:
    text = _read_text(os.path.join(_proc_root(proc_root), "loadavg"))
    if text is None:
        return None
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        one, five, fifteen = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
    cores = os.cpu_count() or 1
    return {
        "one": one,
        "five": five,
        "fifteen": fifteen,
        "cores": cores,
        "percent": round(one / cores * 100, 1),
    }


def read_uptime(proc_root: str | None = None) -> float | None:
    text = _read_text(os.path.join(_proc_root(proc_root), "uptime"))
    if text is None:
        return None
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def read_disk(path: str | None = None) -> dict | None:
    target = DISK_PATH if path is None else path
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    total = usage.total
    used = usage.used
    percent = round(used / total * 100, 1) if total > 0 else 0.0
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": usage.free,
        "percent": percent,
    }


def parse_size(text: object) -> int | None:
    """``100.5MiB`` / ``1.922GiB`` / ``512KiB`` / ``1.2MB`` / ``0B`` / ``12kB`` -> bytes.

    Binary units Ki..Pi = 1024^n, decimal units K..P = 1000^n, ``B`` = 1.
    """
    if not isinstance(text, str):
        return None
    match = _SIZE_RE.match(text)
    if match is None:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    if unit == "B":
        factor = 1
    elif unit.endswith("IB"):
        factor = _BINARY_UNITS.get(unit[:-1])
        if factor is None:
            return None
    elif unit.endswith("B"):
        factor = _DECIMAL_UNITS.get(unit[:-1])
        if factor is None:
            return None
    else:
        return None
    return int(round(number * factor))


def parse_percent(text: object) -> float | None:
    """``1.23%`` -> 1.23; ``--`` or garbage -> None."""
    if not isinstance(text, str):
        return None
    match = _PERCENT_RE.match(text)
    if match is None:
        return None
    return float(match.group(1))


def _split_pair(text: object) -> tuple[int | None, int | None]:
    if not isinstance(text, str) or "/" not in text:
        return (None, None)
    left, _, right = text.partition("/")
    return (parse_size(left.strip()), parse_size(right.strip()))


def _parse_int(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _container_from_row(row: dict) -> dict | None:
    name = row.get("Name")
    if not name:
        return None
    mem_used, mem_limit = _split_pair(row.get("MemUsage"))
    return {
        "name": str(name),
        "cpu_percent": parse_percent(row.get("CPUPerc")),
        "mem_used_bytes": mem_used,
        "mem_limit_bytes": mem_limit,
        "mem_percent": parse_percent(row.get("MemPerc")),
        "pids": _parse_int(row.get("PIDs")),
    }


def _parse_host_rows(text: str) -> list[dict]:
    """Accept either NDJSON (one object per line) or a single JSON array of objects."""
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    rows: list[dict] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _cpu_sort_key(container: dict) -> tuple[bool, float]:
    cpu = container.get("cpu_percent")
    return (cpu is None, -(cpu if cpu is not None else 0.0))


def read_apps(path: str | None = None, now: float | None = None) -> dict:
    """Read the host job's ``docker stats`` dump; never raises."""
    if now is None:
        now = time.time()
    target = HOST_STATS_PATH if path is None else path
    result: dict = {
        "available": False,
        "stale": False,
        "updated_at": None,
        "age_seconds": None,
        "containers": [],
    }
    try:
        stat = os.stat(target)
    except OSError:
        return result
    result["available"] = True
    age = now - stat.st_mtime
    result["age_seconds"] = round(age, 1)
    result["updated_at"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    result["stale"] = age > HOST_STATS_STALE_SECONDS
    text = _read_text(target)
    if text is None:
        return result
    containers = []
    for row in _parse_host_rows(text):
        container = _container_from_row(row)
        if container is not None:
            containers.append(container)
    containers.sort(key=_cpu_sort_key)
    result["containers"] = containers
    return result


async def snapshot() -> dict:
    """Everything the ops console "Server" page needs, reading the module constants at call
    time so tests can monkeypatch ``PROC_ROOT`` / ``DISK_PATH`` / ``HOST_STATS_PATH``."""
    cpu_percent = await sample_cpu_percent(PROC_ROOT)
    return {
        "scope": "host",
        "label": "Whole server (shared with other apps)",
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {"percent": cpu_percent, "cores": os.cpu_count() or 1},
        "memory": read_memory(PROC_ROOT),
        "disk": read_disk(DISK_PATH),
        "load": read_load(PROC_ROOT),
        "uptime_seconds": read_uptime(PROC_ROOT),
        "apps": read_apps(HOST_STATS_PATH),
        "thresholds": {"warn": 80, "critical": 90},
    }
