"""
Object storage seam for media attachments.

MMS attachments are <= 3.75 MB and production currently runs on a single Linux
VPS.  We deliberately expose an S3-free seam: writing an untestable S3 backend
today would be dead code, and the P5 recordings work will exercise it.

Keys are always tenant-prefixed by the caller (``org/{org_id}/media/{asset_id}``), which is
also the P13 metering and retention-tiering hook.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

_VALID_KEY_RE = re.compile(r"[A-Za-z0-9/_.-]+")


def validate_key(key: str) -> None:
    if not key:
        raise ValueError("Storage key cannot be empty")
    if key.startswith("/"):
        raise ValueError("Storage key cannot start with '/'")
    if _VALID_KEY_RE.fullmatch(key) is None:
        raise ValueError("Storage key contains invalid characters")
    for component in key.split("/"):
        if component in {".", ".."}:
            raise ValueError("Storage key cannot contain '.' or '..' path components")


@runtime_checkable
class ObjectStore(Protocol):
    name: str

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    name = "memory"

    def __init__(self) -> None:
        # Content type is preserved for tests even though the Protocol does not expose it.
        self._objects: dict[str, tuple[str, bytes]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        validate_key(key)
        self._objects[key] = (content_type, data)

    async def get(self, key: str) -> bytes:
        validate_key(key)
        try:
            return self._objects[key][1]
        except KeyError:
            raise KeyError(key) from None

    async def exists(self, key: str) -> bool:
        validate_key(key)
        return key in self._objects

    async def delete(self, key: str) -> None:
        validate_key(key)
        self._objects.pop(key, None)


class LocalFSObjectStore:
    name = "local"

    def __init__(self, root: str | Path) -> None:
        # Resolve once so every key check uses a stable, absolute root.
        self._root = Path(root).expanduser().resolve(strict=False)

    def _path_for_key(self, key: str) -> Path:
        validate_key(key)
        final_path = self._root / key
        # Symlinks under root can point outside, so resolve before checking the boundary.
        resolved_path = final_path.resolve(strict=False)
        try:
            resolved_path.relative_to(self._root)
        except ValueError:
            raise ValueError("Storage key escapes the object store root") from None
        return resolved_path

    def _atomic_write(self, final_path: Path, data: bytes) -> None:
        parent = final_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".tmp-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, final_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        final_path = self._path_for_key(key)
        # Blocking filesystem operations belong in a worker thread.
        await asyncio.to_thread(self._atomic_write, final_path, data)

    async def get(self, key: str) -> bytes:
        final_path = self._path_for_key(key)

        def read() -> bytes:
            try:
                return final_path.read_bytes()
            except FileNotFoundError:
                raise KeyError(key) from None

        return await asyncio.to_thread(read)

    async def exists(self, key: str) -> bool:
        final_path = self._path_for_key(key)
        return await asyncio.to_thread(lambda: final_path.exists())

    async def delete(self, key: str) -> None:
        final_path = self._path_for_key(key)

        def remove() -> None:
            try:
                final_path.unlink()
            except FileNotFoundError:
                pass

        await asyncio.to_thread(remove)


def build_store(backend: str, *, root: str = "var/media") -> ObjectStore:
    if backend == "memory":
        return InMemoryObjectStore()
    if backend == "local":
        return LocalFSObjectStore(root=root)
    if backend == "s3":
        raise ValueError(
            "S3 object store backend arrives in P5 (recordings); use 'local' or 'memory'"
        )
    raise ValueError(
        f"Unknown object store backend {backend!r}; valid options: 'memory', 'local'"
    )
