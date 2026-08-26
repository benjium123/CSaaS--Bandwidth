from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime

import httpx
import structlog

from app.providers.telnyx import webhooks as msg_webhooks
from app.providers.voice import (
    CreateCallResult,
    Gather,
    Hangup,
    Pause,
    Play,
    Speak,
    StartRecording,
    StopRecording,
    Transfer,
    VoiceCommand,
    VoiceEvent,
)

logger = structlog.get_logger(__name__)


def _parse_telnyx_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decode_client_state(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return ""


class TelnyxVoiceMixin:
    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        machine_detection: str = "off",
        tag: str = "",
    ) -> CreateCallResult:
        body = {
            "connection_id": getattr(self, "voice_connection_id", ""),
            "to": to,
            "from": from_,
        }
        if tag:
            body["client_state"] = base64.b64encode(tag.encode()).decode()
        if machine_detection == "async":
            body["answering_machine_detection"] = "detect"

        client = await self._get_client()
        url = f"{self.base_url}/calls"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("telnyx_create_call_transport_error", error=str(exc))
            return CreateCallResult("rejected", None, str(exc)[:255])

        if response.status_code < 200 or response.status_code >= 300:
            detail = response.text[:255]
            logger.warning(
                "telnyx_create_call_rejected",
                status_code=response.status_code,
                detail=detail,
            )
            return CreateCallResult("rejected", None, detail)

        try:
            payload = response.json()
            provider_call_id = payload["data"]["call_control_id"]
        except Exception:
            provider_call_id = None
        return CreateCallResult("accepted", provider_call_id)

    def render_commands(self, commands: list[VoiceCommand]) -> str | None:
        return None

    async def execute_commands(
        self, provider_call_id: str, commands: list[VoiceCommand]
    ) -> None:
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self.api_key}"}

        for command in commands:
            action = ""
            body: dict[str, object] = {}

            if isinstance(command, Speak):
                action = "speak"
                body = {"payload": command.text, "voice": "female", "language": "en-US"}
            elif isinstance(command, Play):
                action = "playback_start"
                body = {"audio_url": command.url}
            elif isinstance(command, Gather):
                body = {
                    "maximum_digits": command.max_digits,
                    "terminating_digit": command.terminating_digit,
                    "timeout_millis": int(command.timeout_seconds * 1000),
                }
                if command.action_tag:
                    body["client_state"] = base64.b64encode(
                        command.action_tag.encode()
                    ).decode()
                if isinstance(command.prompt, Speak):
                    action = "gather_using_speak"
                    body.update(
                        {
                            "payload": command.prompt.text,
                            "voice": "female",
                            "language": "en-US",
                        }
                    )
                else:
                    action = "gather"
            elif isinstance(command, StartRecording):
                action = "record_start"
                body = {
                    "format": "mp3",
                    "channels": "dual" if command.channels == "dual" else "single",
                }
            elif isinstance(command, StopRecording):
                action = "record_stop"
                body = {}
            elif isinstance(command, Transfer):
                action = "transfer"
                body = {"to": command.to, "from": command.from_}
            elif isinstance(command, Hangup):
                action = "hangup"
                body = {}
            elif isinstance(command, Pause):
                logger.debug("telnyx_voice_pause_skipped", provider_call_id=provider_call_id)
                continue
            else:
                logger.warning(
                    "unknown_telnyx_voice_command",
                    command_type=type(command).__name__,
                )
                continue

            url = f"{self.base_url}/calls/{provider_call_id}/actions/{action}"
            try:
                response = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning(
                    "telnyx_voice_command_transport_error",
                    action=action,
                    error=str(exc),
                )
                continue

            if response.status_code < 200 or response.status_code >= 300:
                logger.warning(
                    "telnyx_voice_command_rejected",
                    action=action,
                    status_code=response.status_code,
                    detail=response.text[:255],
                )

    def verify_voice_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return msg_webhooks.verify(headers, self._public_key, raw_body)

    def parse_voice_webhook(self, raw_body: bytes) -> list[VoiceEvent]:
        try:
            obj = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(obj, dict):
            return []

        data = obj.get("data", {})
        if not isinstance(data, dict):
            return []
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        event_type_raw = data.get("event_type", "")
        if not isinstance(event_type_raw, str):
            return []

        canonical_mapping = {
            "call.initiated": "call_initiated",
            "call.answered": "call_answered",
            "call.bridged": "call_bridged",
            "call.hangup": "call_hungup",
            "call.dtmf.received": "dtmf_received",
            "call.gather.ended": "dtmf_received",
            "call.machine.detection.ended": (
                "machine_detected"
                if payload.get("result") in ("machine", "fax")
                else "human_detected"
            ),
            "call.recording.saved": "recording_ready",
        }
        canonical = canonical_mapping.get(event_type_raw)
        if canonical is None:
            logger.warning("telnyx_unknown_voice_event", event_type=event_type_raw)
            return []

        digits = ""
        hangup_cause = ""
        recording_url = ""
        provider_recording_id = ""

        if canonical == "dtmf_received":
            if event_type_raw == "call.dtmf.received":
                digits = payload.get("digit", "")
            else:
                digits = payload.get("digits", "")
        elif canonical == "call_hungup":
            hangup_cause = payload.get("hangup_cause", "")
        elif canonical == "recording_ready":
            recording_urls = payload.get("recording_urls", {})
            if isinstance(recording_urls, dict):
                recording_url = recording_urls.get("mp3", "")
            provider_recording_id = payload.get("recording_id", "")

        return [
            VoiceEvent(
                event_type=canonical,
                provider_call_id=payload.get("call_control_id", ""),
                provider_event_id=data.get("id", ""),
                to=payload.get("to", ""),
                from_=payload.get("from", ""),
                digits=digits,
                recording_url=recording_url,
                provider_recording_id=provider_recording_id,
                hangup_cause=hangup_cause,
                tag=_decode_client_state(payload.get("client_state")),
                occurred_at=_parse_telnyx_datetime(data.get("occurred_at")),
                raw=obj,
            )
        ]

    def recording_auth(self, url: str) -> tuple[str, str] | None:
        return None
