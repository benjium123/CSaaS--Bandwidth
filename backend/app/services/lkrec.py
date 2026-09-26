"""Self-hosted LiveKit call recorder client and sweeper jobs (lkrec)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Call, CallReview
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services import calling_settings

log = structlog.get_logger("lkrec")


class AnnouncementUnavailable(Exception): ...


ANNOUNCE_MODEL = "eleven_flash_v2_5"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
ANNOUNCE_MAX_BYTES = 2 * 1024 * 1024
ANNOUNCE_TIMEOUT_SECONDS = 20.0
RECORDER_TIMEOUT_SECONDS = 5.0


def enabled(settings) -> bool:
    url = settings.monitor_recorder_url or ""
    return bool(url.strip())


def api_token(settings) -> str:
    secret = settings.livekit_api_secret.get_secret_value()
    return hashlib.sha256(f"lkrec:{secret}".encode("utf-8")).hexdigest()


def room_call_id(room: str) -> uuid.UUID | None:
    if not isinstance(room, str) or not room.startswith("call-"):
        return None
    try:
        return uuid.UUID(room[5:])
    except ValueError:
        return None


def _safe_join(directory: Path, name: str) -> Path | None:
    if not name or "/" in name or ".." in name:
        return None
    return directory / name


def _safe_identity(identity: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]", "-", identity or "")
    return safe[:64] or "-"


def _safe_unlink(directory: Path, name: str) -> None:
    path = _safe_join(directory, name)
    if path is None:
        log.warning("lkrec_unsafe_file_name", file_name=name)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("lkrec_unlink_failed", path=str(path))


def _delete_manifest_files(directory: Path, manifest_path: Path, data: dict) -> None:
    files = data.get("files")
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            file_name = file_entry.get("file")
            if isinstance(file_name, str):
                _safe_unlink(directory, file_name)
    try:
        manifest_path.unlink(missing_ok=True)
    except OSError:
        log.warning("lkrec_manifest_unlink_failed", manifest=str(manifest_path))


def _delete_old_manifest(manifest_path: Path, now: datetime) -> None:
    try:
        mtime = datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return
    if now - mtime > timedelta(days=1):
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError:
            log.warning("lkrec_manifest_unlink_failed", manifest=str(manifest_path))


async def ensure_announcement(
    settings, org, *, client: httpx.AsyncClient | None = None
) -> str:
    """File name of the org's announcement as Ogg/Opus in <dir>/announce, rendering + caching it."""
    text = calling_settings.announcement_text_for(org).strip()
    if not text:
        return ""

    voice = settings.elevenlabs_voice_id or DEFAULT_VOICE_ID
    api_key_setting = getattr(settings, "elevenlabs_api_key", None)
    api_key = api_key_setting.get_secret_value() if api_key_setting else ""
    if not api_key:
        raise AnnouncementUnavailable("ElevenLabs API key is not configured.")

    digest = hashlib.sha256(
        f"{voice}|{ANNOUNCE_MODEL}|{text}".encode("utf-8")
    ).hexdigest()
    name = digest[:32] + ".ogg"
    directory = Path(settings.monitor_recorder_dir) / "announce"
    path = directory / name
    if path.exists():
        return name

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=opus_48000_64"
    headers = {
        "xi-api-key": api_key,
        "content-type": "application/json",
        "accept": "audio/ogg",
    }
    json_body = {"text": text, "model_id": ANNOUNCE_MODEL}

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=ANNOUNCE_TIMEOUT_SECONDS)
    try:
        response = await http.post(
            url,
            headers=headers,
            json=json_body,
            timeout=ANNOUNCE_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise AnnouncementUnavailable(
                "ElevenLabs announcement synthesis failed with an HTTP error."
            )
        data = response.content
        if not data.startswith(b"OggS"):
            raise AnnouncementUnavailable("ElevenLabs returned non-Ogg audio.")
        if len(data) > ANNOUNCE_MAX_BYTES:
            raise AnnouncementUnavailable("ElevenLabs announcement audio is too large.")
    except AnnouncementUnavailable:
        raise
    except httpx.RequestError as exc:
        raise AnnouncementUnavailable(
            "Could not reach the voice provider."
        ) from exc
    finally:
        if owns_client:
            await http.aclose()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp_path = directory / f".{name}.{uuid.uuid4().hex}.tmp"
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except OSError as exc:
        raise AnnouncementUnavailable(
            "Could not cache announcement audio."
        ) from exc
    return name


async def start(
    settings,
    room: str,
    *,
    announcement: str = "",
    resume: bool = False,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Ask the recorder to record `room`; never raises."""
    if not enabled(settings):
        return False
    url = settings.monitor_recorder_url.rstrip("/") + "/start"
    headers = {"Authorization": f"Bearer {api_token(settings)}"}
    body = {"room": room, "announcement": announcement, "resume": resume}
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=RECORDER_TIMEOUT_SECONDS)
    try:
        response = await http.post(
            url, headers=headers, json=body, timeout=RECORDER_TIMEOUT_SECONDS
        )
        if response.status_code == 200:
            return True
        log.warning(
            "lkrec_start_failed", room=room, status=response.status_code
        )
        return False
    except Exception:
        log.exception("lkrec_start_failed", room=room)
        return False
    finally:
        if owns_client:
            await http.aclose()


async def active_rooms(
    settings, *, client: httpx.AsyncClient | None = None
) -> set[str] | None:
    """Rooms the recorder is recording now; None when it cannot be reached."""
    if not enabled(settings):
        return None
    url = settings.monitor_recorder_url.rstrip("/") + "/health"
    headers = {"Authorization": f"Bearer {api_token(settings)}"}
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=RECORDER_TIMEOUT_SECONDS)
    try:
        response = await http.get(
            url, headers=headers, timeout=RECORDER_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            log.warning("lkrec_health_failed", status=response.status_code)
            return None
        payload = response.json()
        rooms = payload.get("rooms")
        if not isinstance(rooms, list):
            log.warning("lkrec_health_invalid_payload")
            return None
        return {str(room) for room in rooms if isinstance(room, str)}
    except Exception:
        log.warning("lkrec_health_failed", exc_info=True)
        return None
    finally:
        if owns_client:
            await http.aclose()


def role_for(file: dict) -> str | None:
    kind = file.get("kind")
    if kind == "sip":
        return "customer"
    if kind == "standard":
        return "agent"
    if kind == "agent":
        return None
    if kind == "unknown":
        identity = file.get("identity") or ""
        return "customer" if identity.startswith("sip_") else "agent"
    return None


async def ingest_tick(session, settings, store, *, limit: int = 50) -> dict:
    counts: dict[str, int] = {"discarded": 0, "duplicate": 0, "ingested": 0}
    if not enabled(settings):
        return counts
    directory = Path(settings.monitor_recorder_dir)
    if not directory.is_dir():
        return counts

    now = datetime.now(timezone.utc)
    processed = 0
    for manifest_path in sorted(directory.glob("*.json"), key=lambda p: p.name):
        if manifest_path.name.startswith("."):
            continue
        if processed >= limit:
            break
        processed += 1

        prefix = manifest_path.stem
        manifest_name = manifest_path.name
        try:
            data = json.loads(manifest_path.read_text())
            if not isinstance(data, dict):
                _delete_old_manifest(manifest_path, now)
                continue
        except Exception:
            _delete_old_manifest(manifest_path, now)
            continue

        room = data.get("room")
        files = data.get("files")
        if not isinstance(room, str) or not isinstance(files, list):
            _delete_old_manifest(manifest_path, now)
            continue

        call_id = room_call_id(room)
        if call_id is None:
            _delete_manifest_files(directory, manifest_path, data)
            counts["discarded"] += 1
            continue

        # JUSTIFIED allow_unscoped: the sweeper inspects recordings for any call in the shared directory.
        call = (
            await session.execute(
                sa.select(Call)
                .where(Call.id == call_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if call is None:
            _delete_manifest_files(directory, manifest_path, data)
            counts["discarded"] += 1
            continue

        set_org_context(session, call.org_id)
        extra = dict(call.extra or {})
        recs = list(extra.get("monitor_recordings") or [])
        if any(
            isinstance(rec, dict) and rec.get("prefix") == prefix for rec in recs
        ):
            _delete_manifest_files(directory, manifest_path, data)
            counts["duplicate"] += 1
            continue

        uploaded_files = []
        try:
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    continue
                role = role_for(file_entry)
                if role is None:
                    continue
                file_name = file_entry.get("file")
                if not isinstance(file_name, str):
                    continue
                local_path = _safe_join(directory, file_name)
                if local_path is None:
                    log.warning(
                        "lkrec_unsafe_file_name",
                        manifest=manifest_name,
                        file=file_name,
                    )
                    continue
                if not local_path.is_file() or local_path.stat().st_size <= 0:
                    continue
                identity = file_entry.get("identity") or ""
                if not isinstance(identity, str):
                    identity = str(identity)
                safe_identity = _safe_identity(identity)
                key = f"org/{call.org_id}/monitor/{call.id}/{prefix}/{role}-{safe_identity}.ogg"
                data_bytes = local_path.read_bytes()
                await store.put(key, data_bytes, "audio/ogg")
                uploaded_files.append(
                    {
                        "role": role,
                        "identity": identity,
                        "key": key,
                        "bytes": len(data_bytes),
                        "duration_ms": file_entry.get("duration_ms"),
                    }
                )

            rec = {
                "prefix": prefix,
                "started_at": data.get("started_at"),
                "ended_at": data.get("ended_at"),
                "announced": bool(data.get("announced", False)),
                "announce_error": data.get("announce_error"),
                "recovered": bool(data.get("recovered", False)),
                "files": uploaded_files,
            }
            recs.append(rec)
            extra["monitor_recordings"] = recs
            call.extra = extra
            await session.commit()
        except Exception:
            log.exception(
                "lkrec_ingest_failed",
                manifest=manifest_name,
                call_id=str(call.id),
            )
            await session.rollback()
            continue

        # Only after commit: remove the local files and manifest.
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            file_name = file_entry.get("file")
            if isinstance(file_name, str):
                _safe_unlink(directory, file_name)
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "lkrec_manifest_unlink_failed", manifest=str(manifest_path)
            )
        counts["ingested"] += 1

    return counts


async def purge_tick(
    session,
    settings,
    store,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.monitor_recording_keep_days)

    # JUSTIFIED allow_unscoped: the sweeper purges monitor recordings across workspaces.
    reviews = (
        (
            await session.execute(
                sa.select(CallReview)
                .where(
                    sa.or_(
                        sa.and_(
                            CallReview.status == "reviewed",
                            CallReview.verdict == "ok",
                            CallReview.reviewed_at < cutoff,
                        ),
                        sa.and_(
                            CallReview.status == "skipped",
                            CallReview.updated_at < cutoff,
                        ),
                    )
                )
                .limit(limit)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )

    purged = 0
    for review in reviews:
        set_org_context(session, review.org_id)
        call = await session.get(Call, review.call_id)
        if call is None:
            continue
        extra = dict(call.extra or {})
        if "monitor_recordings" not in extra or extra.get(
            "monitor_recordings_purged"
        ):
            continue

        try:
            for recording in extra.get("monitor_recordings") or []:
                if not isinstance(recording, dict):
                    continue
                for file_entry in recording.get("files", []) or []:
                    if not isinstance(file_entry, dict):
                        continue
                    key = file_entry.get("key")
                    if isinstance(key, str) and key:
                        await store.delete(key)
            extra["monitor_recordings_purged"] = True
            call.extra = extra
            await session.commit()
            purged += 1
        except Exception:
            log.exception(
                "lkrec_purge_failed",
                review_id=str(review.id),
                call_id=str(review.call_id),
            )
            await session.rollback()

    return purged


async def resume_tick(
    session,
    settings,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=6)
    rooms = await active_rooms(settings, client=client)
    if rooms is None:
        return 0

    # JUSTIFIED allow_unscoped: the sweeper restarts recorders for live calls across workspaces.
    calls = (
        (
            await session.execute(
                sa.select(Call)
                .where(
                    Call.created_at >= cutoff,
                    Call.status.not_in(TERMINAL_CALL_STATUSES),
                )
                .limit(200)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )

    restarted = 0
    for call in calls:
        extra = dict(call.extra or {})
        recorder = extra.get("recorder")
        if extra.get("via") != "livekit" or not isinstance(recorder, dict):
            continue
        room = recorder.get("room")
        if not isinstance(room, str) or not room:
            continue
        if room in rooms:
            continue
        # Skip the announcement only when it has surely played already: the phone
        # answered well before the restart. Otherwise announce (again) before recording.
        answered = call.answered_at
        if answered is not None and answered.tzinfo is None:
            answered = answered.replace(tzinfo=timezone.utc)
        played = answered is not None and now - answered > timedelta(seconds=30)
        announcement = "" if played else str(recorder.get("announcement") or "")
        if await start(settings, room, announcement=announcement, resume=played, client=client):
            restarted += 1

    return restarted


async def load_sides(store, call) -> list[tuple[str, bytes, int]]:
    """[(role, ogg bytes, offset_ms), ...] for every monitor recording of the call, in order.
    A call recorded in parts (recorder restarted mid-call) has several recordings; offset_ms
    places each part on the call's timeline (ms after the first part started)."""
    extra = call.extra or {}
    if extra.get("monitor_recordings_purged"):
        return []
    recordings = [r for r in (extra.get("monitor_recordings") or []) if isinstance(r, dict)]
    first = _parse_ts(recordings[0].get("started_at")) if recordings else None
    sides: list[tuple[str, bytes, int]] = []
    for rec in recordings:
        started = _parse_ts(rec.get("started_at"))
        offset_ms = int((started - first).total_seconds() * 1000) if started and first else 0
        for file_entry in rec.get("files", []) or []:
            if not isinstance(file_entry, dict):
                continue
            role, key = file_entry.get("role"), file_entry.get("key")
            if not role or not key:
                continue
            try:
                data = await store.get(key)
            except KeyError:
                continue
            sides.append((role, data, max(offset_ms, 0)))
    return sides


def _parse_ts(value) -> datetime | None:  # noqa: ANN001
    if not isinstance(value, str) or not value:
        return None
    try:
        # Go writes RFC 3339 with nanoseconds; Python accepts up to microseconds.
        head, _, frac = value.partition(".")
        if frac:
            digits = frac[: len(frac) - len(frac.lstrip("0123456789"))]
            tz = frac[len(digits):]
            value = f"{head}.{digits[:6]}{tz}"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
