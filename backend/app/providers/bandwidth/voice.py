from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

import httpx
import structlog

from app.errors import FeatureUnavailableError
from app.providers.bandwidth.voice_webhooks import basic_auth_matches, parse_iso_duration_seconds
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


def _parse_datetime(raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bandwidth_event_id(event_type: str, call_id: str, raw_time: object) -> str:
    raw_time_str = raw_time if isinstance(raw_time, str) else ""
    digest = hashlib.sha256(f"{event_type}:{call_id}:{raw_time_str}".encode()).hexdigest()
    return f"bw-voice-{digest}"


class BandwidthVoiceMixin:
    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        machine_detection: str = "off",
        tag: str = "",
    ) -> CreateCallResult:
        cb = getattr(self, "voice_callback_url", "").rstrip("/")
        application_id = getattr(self, "voice_application_id", "") or self.application_id
        body = {
            "to": to,
            "from": from_,
            "applicationId": application_id,
            "answerUrl": f"{cb}/answer",
            "disconnectUrl": f"{cb}/disconnect",
        }
        if tag:
            body["tag"] = tag
        if machine_detection == "async":
            body["machineDetection"] = {
                "mode": "async",
                "callbackUrl": f"{cb}/amd",
            }

        client = await self._get_client()
        url = f"https://voice.bandwidth.com/api/v2/accounts/{self.account_id}/calls"
        try:
            response = await client.post(
                url,
                json=body,
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            logger.warning("bandwidth_create_call_transport_error", error=str(exc))
            return CreateCallResult("rejected", None, str(exc)[:255])

        if response.status_code not in (200, 201, 202):
            detail = response.text[:255]
            logger.warning(
                "bandwidth_create_call_rejected",
                status_code=response.status_code,
                detail=detail,
            )
            return CreateCallResult("rejected", None, detail)

        try:
            payload = response.json()
        except Exception:
            payload = {}
        return CreateCallResult("accepted", payload.get("callId"))

    def render_commands(self, commands: list[VoiceCommand]) -> str:
        parts = ['<?xml version="1.0" encoding="UTF-8"?><Response>']

        for command in commands:
            if isinstance(command, Speak):
                parts.append(
                    f"<SpeakSentence voice={quoteattr(command.voice)}>"
                    f"{escape(command.text)}</SpeakSentence>"
                )
            elif isinstance(command, Play):
                parts.append(f"<PlayAudio>{escape(command.url)}</PlayAudio>")
            elif isinstance(command, Gather):
                attrs = (
                    f'maxDigits="{command.max_digits}" '
                    f'terminatingDigits="{command.terminating_digit}" '
                    f'firstDigitTimeout="{command.timeout_seconds}"'
                )
                if command.action_tag:
                    attrs += f" tag={quoteattr(command.action_tag)}"
                if command.prompt is None:
                    parts.append(f"<Gather {attrs}/>")
                else:
                    parts.append(f"<Gather {attrs}>")
                    prompt = command.prompt
                    if isinstance(prompt, Speak):
                        parts.append(
                            f"<SpeakSentence voice={quoteattr(prompt.voice)}>"
                            f"{escape(prompt.text)}</SpeakSentence>"
                        )
                    elif isinstance(prompt, Play):
                        parts.append(f"<PlayAudio>{escape(prompt.url)}</PlayAudio>")
                    else:
                        raise ValueError(f"Unsupported gather prompt {type(prompt).__name__}")
                    parts.append("</Gather>")
            elif isinstance(command, StartRecording):
                if command.channels == "dual":
                    parts.append('<StartRecording fileFormat="mp3" multiChannel="true"/>')
                else:
                    parts.append('<StartRecording fileFormat="mp3"/>')
            elif isinstance(command, StopRecording):
                parts.append("<StopRecording/>")
            elif isinstance(command, Transfer):
                parts.append(
                    f"<Transfer transferCallerId={quoteattr(command.from_)}>"
                    f"<PhoneNumber>{escape(command.to)}</PhoneNumber></Transfer>"
                )
            elif isinstance(command, Hangup):
                parts.append("<Hangup/>")
            elif isinstance(command, Pause):
                parts.append(f'<Pause duration="{command.seconds}"/>')
            else:
                raise ValueError(f"Unsupported voice command type {type(command).__name__}")

        parts.append("</Response>")
        return "".join(parts)

    async def execute_commands(
        self, provider_call_id: str, commands: list[VoiceCommand]
    ) -> None:
        raise FeatureUnavailableError(
            "Bandwidth delivers voice commands in webhook responses (BXML); "
            "mid-stream commands need the media WebSocket or a Redirect"
        )

    def verify_voice_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        user = getattr(self, "voice_webhook_username", "")
        password = getattr(self, "voice_webhook_password", "")
        if not user and not password:
            # FAIL CLOSED. A deployment that forgot to configure webhook credentials must
            # reject every voice webhook loudly, not accept forged ones silently - an
            # unauthenticated voice webhook can create calls and drive the recording
            # fetcher. config.validate() names the missing settings in production.
            return False

        auth_header = ""
        for key, value in headers.items():
            if key.lower() == "authorization":
                auth_header = value
                break
        return basic_auth_matches(auth_header, user, password)

    def parse_voice_webhook(self, raw_body: bytes) -> list[VoiceEvent]:
        try:
            obj = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(obj, dict):
            return []

        event_type_carrier = obj.get("eventType", "")
        if not isinstance(event_type_carrier, str):
            return []

        if event_type_carrier == "machineDetectionComplete":
            machine_result = obj.get("machineDetectionResult", {})
            machine_value = (
                str(machine_result.get("value", ""))
                if isinstance(machine_result, dict)
                else ""
            )
            canonical = (
                "machine_detected"
                if machine_value.startswith("answering-machine")
                else "human_detected"
            )
        else:
            canonical = {
                "initiate": "call_initiated",
                "answer": "call_answered",
                "disconnect": "call_hungup",
                "gather": "dtmf_received",
                "recordingAvailable": "recording_ready",
                "transferComplete": "transfer_completed",
                "transferAnswer": "call_bridged",
            }.get(event_type_carrier)

        if canonical is None:
            logger.warning(
                "bandwidth_unknown_voice_event",
                event_type=event_type_carrier,
            )
            return []

        call_id = str(obj.get("callId") or "")
        raw_time = obj.get("eventTime") or obj.get("startTime")
        provider_event_id = _bandwidth_event_id(event_type_carrier, call_id, raw_time)

        digits = ""
        hangup_cause = ""
        recording_url = ""
        provider_recording_id = ""
        duration_seconds = None

        if canonical == "call_hungup":
            hangup_cause = obj.get("cause", "")
            duration_seconds = parse_iso_duration_seconds(str(obj.get("duration", "") or ""))
        elif canonical == "dtmf_received":
            digits = obj.get("digits", "")
        elif canonical == "recording_ready":
            recording_url = obj.get("mediaUrl", "")
            provider_recording_id = obj.get("recordingId", "")
            duration_seconds = parse_iso_duration_seconds(str(obj.get("duration", "") or ""))

        return [
            VoiceEvent(
                event_type=canonical,
                provider_call_id=call_id,
                provider_event_id=provider_event_id,
                to=obj.get("to", ""),
                from_=obj.get("from", ""),
                digits=digits,
                recording_url=recording_url,
                provider_recording_id=provider_recording_id,
                duration_seconds=duration_seconds,
                hangup_cause=hangup_cause,
                tag=str(obj.get("tag", "") or ""),
                occurred_at=_parse_datetime(obj.get("startTime") or obj.get("eventTime")),
                raw=obj,
            )
        ]

    def recording_auth(self, url: str) -> tuple[str, str] | None:
        hostname = urlparse(url).hostname
        if hostname and hostname.endswith(".bandwidth.com"):
            return self._auth
        return None
