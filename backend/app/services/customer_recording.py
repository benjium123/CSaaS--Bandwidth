"""Customer call recording finalizer.

Turns lkrec's per-side Ogg/Opus recordings into a stereo MP3 CallRecording
(agent left, customer right) so the existing recording player, access checks,
retention and erasure rules apply without changes.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Org
from app.models.voice import Call, CallRecording
from app.services import calling_settings, recordings
from app.services.lkrec import _parse_ts

log = structlog.get_logger("customer_recording")

PROVIDER_PREFIX = "lkrec:"
SETTLE_SECONDS = 10
GIVE_UP_SECONDS = 3600
FFMPEG_TIMEOUT_SECONDS = 180
SAMPLE_RATE = 16000
STEREO_BITRATE = "48k"
MONO_BITRATE = "32k"


class RenderError(Exception):
    """Raised when MP3 rendering cannot be completed."""


def provider_id(call_id: uuid.UUID) -> str:
    return f"{PROVIDER_PREFIX}{call_id}"


async def wanted(session, org) -> bool:
    if not calling_settings.record_calls_for(org):
        return False
    try:
        from app.services import entitlements
    except ImportError:
        return True
    return bool(await entitlements.has(session, org.id, "call_recording"))


def queue(session, call, *, leg_id=None) -> CallRecording:
    recording_id = uuid.uuid4()
    rec = CallRecording(
        id=recording_id,
        org_id=call.org_id,
        call_id=call.id,
        leg_id=leg_id,
        provider_recording_id=provider_id(call.id),
        storage_key=recordings.storage_key(call.org_id, recording_id),
        content_type="audio/mpeg",
        status="pending",
        channel_layout="mixed",
    )
    session.add(rec)
    call.extra = {**(call.extra or {}), "customer_recording": True}
    return rec


async def _default_runner(argv: list[str], timeout: float) -> None:
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg not found")

    cmd = ["nice", "-n", "10", *argv] if shutil.which("nice") else argv
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise RenderError("ffmpeg not found") from exc

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderError("ffmpeg timed out") from None

    if proc.returncode != 0:
        message = stderr.decode(errors="ignore")[:500]
        raise RenderError(f"ffmpeg exited with status {proc.returncode}: {message}")


async def render(parts, *, dual: bool, runner=None) -> tuple[bytes, dict[str, bytes]]:
    """Render per-side Ogg/Opus parts into stereo MP3 and optional dual mono sides."""
    if not parts:
        raise RenderError("no audio parts to render")

    clean_parts: list[tuple[str, bytes, int]] = []
    for role, data, offset_ms in parts:
        if role not in {"agent", "customer"}:
            raise RenderError(f"unknown role {role}")
        if not isinstance(data, bytes):
            raise RenderError("audio part data must be bytes")
        clean_parts.append((role, data, max(int(offset_ms or 0), 0)))

    if runner is None:
        runner = _default_runner

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_args: list[str] = []
        filters: list[str] = []
        role_indices: dict[str, list[int]] = {"agent": [], "customer": []}

        for i, (role, data, offset) in enumerate(clean_parts):
            part_path = tmp_path / f"part{i}.ogg"
            part_path.write_bytes(data)
            input_args.extend(["-i", str(part_path)])
            filters.append(
                f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono,"
                f"adelay={offset}:all=1[p{i}]"
            )
            role_indices[role].append(i)

        for role in ("agent", "customer"):
            indices = role_indices[role]
            if len(indices) > 1:
                joined = "".join(f"[p{i}]" for i in indices)
                filters.append(
                    f"{joined}amix=inputs={len(indices)}:normalize=0:duration=longest[{role}]"
                )
            elif len(indices) == 1:
                filters.append(f"[p{indices[0]}]anull[{role}]")
            else:
                filters.append(f"aevalsrc=0:s={SAMPLE_RATE}:d=0.5[{role}]")

        if dual:
            filters.append("[agent]asplit=2[agent_s][agent_o]")
            filters.append("[customer]asplit=2[customer_s][customer_o]")
            left_src = "[agent_s]"
            right_src = "[customer_s]"
        else:
            left_src = "[agent]"
            right_src = "[customer]"

        filters.append(f"{left_src}pan=stereo|c0=c0|c1=0*c0[L]")
        filters.append(f"{right_src}pan=stereo|c0=0*c0|c1=c0[R]")
        filters.append("[L][R]amix=inputs=2:normalize=0:duration=longest[st]")

        argv = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *input_args,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[st]",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "libmp3lame",
            "-b:a",
            STEREO_BITRATE,
            str(tmp_path / "stereo.mp3"),
        ]

        if dual:
            for role in ("agent", "customer"):
                argv.extend(
                    [
                        "-map",
                        f"[{role}_o]",
                        "-ac",
                        "1",
                        "-ar",
                        str(SAMPLE_RATE),
                        "-c:a",
                        "libmp3lame",
                        "-b:a",
                        MONO_BITRATE,
                        str(tmp_path / f"{role}.mp3"),
                    ]
                )

        await runner(argv, FFMPEG_TIMEOUT_SECONDS)

        stereo_path = tmp_path / "stereo.mp3"
        stereo = stereo_path.read_bytes()
        if not stereo:
            raise RenderError("ffmpeg produced empty stereo output")

        sides: dict[str, bytes] = {}
        if dual:
            agent_data = (tmp_path / "agent.mp3").read_bytes()
            customer_data = (tmp_path / "customer.mp3").read_bytes()
            if not agent_data or not customer_data:
                raise RenderError("ffmpeg produced empty side output")
            sides = {"agent": agent_data, "customer": customer_data}

        return stereo, sides


def _parts_from_call(call) -> list[tuple[str, str, int, int | None]]:
    extra = call.extra or {}
    if extra.get("monitor_recordings_purged"):
        return []

    recordings_list = [
        rec for rec in (extra.get("monitor_recordings") or []) if isinstance(rec, dict)
    ]
    if not recordings_list:
        return []

    first_started = _parse_ts(recordings_list[0].get("started_at"))
    parts: list[tuple[str, str, int, int | None]] = []

    for rec in recordings_list:
        started = _parse_ts(rec.get("started_at"))
        if first_started and started:
            offset_ms = int((started - first_started).total_seconds() * 1000)
        else:
            offset_ms = 0
        offset_ms = max(offset_ms, 0)

        for file_entry in rec.get("files", []) or []:
            if not isinstance(file_entry, dict):
                continue
            role = file_entry.get("role")
            key = file_entry.get("key")
            if role not in {"agent", "customer"} or not isinstance(key, str) or not key:
                continue
            duration_raw = file_entry.get("duration_ms")
            duration = duration_raw if isinstance(duration_raw, int) else None
            parts.append((role, key, offset_ms, duration))

    return parts


def duration_seconds(parts_meta) -> int | None:
    latest_ms: int | None = None
    for _, _, offset_ms, duration_ms in parts_meta:
        if isinstance(offset_ms, int) and isinstance(duration_ms, int):
            end_ms = offset_ms + duration_ms
            if latest_ms is None or end_ms > latest_ms:
                latest_ms = end_ms
    return latest_ms // 1000 if latest_ms is not None else None


async def finalize_tick(
    session,
    settings,
    store,
    *,
    now=None,
    limit: int = 10,
    runner=None,
) -> dict:
    counts: dict[str, int] = {"stored": 0, "waiting": 0, "failed": 0}
    now = now or datetime.now(timezone.utc)

    # Only calls that have ended and settled: live calls stay pending for their whole
    # length, and must never fill the batch ahead of calls that are ready.
    settled = now - timedelta(seconds=SETTLE_SECONDS)
    if session.get_bind().dialect.name == "sqlite":
        settled = settled.replace(tzinfo=None)
    stmt = (
        sa.select(CallRecording)
        .join(Call, Call.id == CallRecording.call_id)
        .where(
            CallRecording.status == "pending",
            CallRecording.provider_recording_id.startswith(PROVIDER_PREFIX),
            Call.ended_at.is_not(None),
            Call.ended_at <= settled,
        )
        .order_by(CallRecording.created_at)
        .limit(limit)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    rows = (await session.execute(stmt)).scalars().all()

    for row in rows:
        set_org_context(session, row.org_id)
        try:
            call = await session.get(Call, row.call_id)
            if call is None:
                row.status = "failed"
                counts["failed"] += 1
                await session.commit()
                continue

            ended_at = call.ended_at
            now_cmp = now
            if ended_at is not None and ended_at.tzinfo is None and now_cmp.tzinfo is not None:
                now_cmp = now_cmp.replace(tzinfo=None)

            if ended_at is None or now_cmp - ended_at < timedelta(seconds=SETTLE_SECONDS):
                counts["waiting"] += 1
                continue

            parts_meta = _parts_from_call(call)
            if not parts_meta:
                if now_cmp - ended_at > timedelta(seconds=GIVE_UP_SECONDS):
                    row.status = "failed"
                    counts["failed"] += 1
                else:
                    counts["waiting"] += 1
                await session.commit()
                continue

            loaded: list[tuple[str, bytes, int]] = []
            for role, key, offset_ms, _duration in parts_meta:
                try:
                    data = await store.get(key)
                except KeyError:
                    continue
                if data is None:
                    continue
                loaded.append((role, data, offset_ms))

            if not loaded:
                if now_cmp - ended_at > timedelta(seconds=GIVE_UP_SECONDS):
                    row.status = "failed"
                    counts["failed"] += 1
                else:
                    counts["waiting"] += 1
                await session.commit()
                continue

            org = await session.get(Org, call.org_id)
            dual = bool(
                org is not None and calling_settings.channel_layout_for(org) == "dual"
            )

            try:
                stereo, sides = await render(loaded, dual=dual, runner=runner)
            except RenderError as exc:
                log.warning(
                    "customer_recording_render_failed",
                    call_id=str(call.id),
                    error=str(exc),
                )
                row.status = "failed"
                counts["failed"] += 1
                await session.commit()
                continue

            if len(stereo) > recordings.MAX_RECORDING_BYTES or any(
                len(data) > recordings.MAX_RECORDING_BYTES for data in sides.values()
            ):
                row.status = "failed"
                counts["failed"] += 1
                await session.commit()
                continue

            await store.put(row.storage_key, stereo, "audio/mpeg")
            if dual:
                await store.put(
                    recordings.layout_storage_key(row, "agent"),
                    sides["agent"],
                    "audio/mpeg",
                )
                await store.put(
                    recordings.layout_storage_key(row, "customer"),
                    sides["customer"],
                    "audio/mpeg",
                )

            row.channel_layout = "dual" if dual else "mixed"
            row.content_type = "audio/mpeg"
            row.size_bytes = len(stereo)
            row.duration_seconds = duration_seconds(parts_meta)
            row.status = "stored"

            # Recording minutes, once per call (P46 P2b): $0 + a price_unset alert while unset.
            from app.services import telephony_billing

            await telephony_billing.charge_feature_minutes(
                session,
                row.org_id,
                "recording_min",
                row.duration_seconds or 0,
                reference=f"rec:{row.id}",
                note=f"{row.duration_seconds or 0}s call recording",
                feature="call_recording",
            )

            if not (call.extra or {}).get("monitor"):
                for _, key, _, _ in parts_meta:
                    try:
                        await store.delete(key)
                    except KeyError:
                        pass
                extra = dict(call.extra or {})
                extra["monitor_recordings_purged"] = True
                call.extra = extra

            # Queue the transcript the org pays for: minutes after the call, or overnight.
            from app.services import transcription

            if transcription.enabled(settings) and org is not None:
                mode = await transcription.wanted(session, org)
                # Live calls already have their transcript (from the captions).
                if mode in ("after_call", "overnight"):
                    tier = "soon" if mode == "after_call" else "night"
                    await transcription.enqueue(session, call, row, tier=tier, now=now)

            await session.commit()
            counts["stored"] += 1

        except Exception:
            log.exception("customer_recording_finalize_failed")
            await session.rollback()
            continue

    return counts
