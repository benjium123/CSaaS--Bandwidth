"""Plivo voice: document-return, same family as Bandwidth (BXML) - Plivo XML instead.

Two genuine friction points against the frozen CAL (providers/voice.py), both worth
flagging rather than papering over:

1. `create_call`'s POST /Call/ response carries Plivo's `request_uuid`, NOT the `CallUUID`
   that later webhooks key events on - they are different ids. `CreateCallResult` has one
   id slot; we put `request_uuid` in it because it's the only correlation Plivo gives
   synchronously, but a caller cannot use it to match the first webhook without extra
   reconciliation the CAL has no place to express.
2. `StopRecording` has no XML verb here. Bandwidth's BXML has `<StopRecording/>`; Plivo's
   `<Record/>` only ends on its own max-length/silence timeout or via a live call-control
   API call - there is nothing to *render*. We drop it (debug log), same shape as Bandwidth
   dropping Pause-equivalent gaps, but for a reason that is Plivo's, not a choice we made.

`Gather.action_tag` also has no native carrier field on Plivo's `<GetDigits>` (no
client_state/tag concept the way Bandwidth's `tag` or Telnyx's `client_state` provide it) -
threaded through the callback URL's query string instead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

import httpx
import structlog

from app.errors import FeatureUnavailableError
from app.providers.plivo import webhooks as msg_webhooks
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

#: Plivo's CallStatus -> our canonical vocabulary (app/providers/voice.py VOICE_EVENT_TYPES).
_CALL_STATUS_TO_EVENT = {
    "ringing": "call_ringing",
    "in-progress": "call_answered",
    "completed": "call_hungup",
    "busy": "call_hungup",
    "failed": "call_hungup",
    "no-answer": "call_hungup",
}
#: Fallback when Plivo posts `Event` instead of (or alongside) `CallStatus`.
_EVENT_TO_EVENT = {
    "startapp": "call_initiated",
    "hangup": "call_hungup",
}


def _plivo_voice_event_id(event_type: str, call_uuid: str, raw_time: str) -> str:
    digest = hashlib.sha256(f"{event_type}:{call_uuid}:{raw_time}".encode()).hexdigest()
    return f"plivo-voice-{digest}"


class PlivoVoiceMixin:
    """Requires `_get_client`, `_auth`, `_auth_token`, `base_url`, `_webhook_url` from the
    composing class (see adapter.py)."""

    def _callback_url(self, path: str, tag: str = "") -> str:
        base = getattr(self, "_webhook_url", "").rstrip("/")
        url = f"{base}/{path}"
        if tag:
            url = f"{url}?tag={quote(tag, safe='')}"
        return url

    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        machine_detection: str = "off",
        tag: str = "",
    ) -> CreateCallResult:
        body: dict[str, object] = {
            "from": from_,
            "to": to,
            "answer_url": self._callback_url("answer", tag),
            "answer_method": "POST",
            "hangup_url": self._callback_url("hangup"),
        }
        if machine_detection == "async":
            body["machine_detection"] = "true"
            body["machine_detection_url"] = self._callback_url("amd")

        client = await self._get_client()
        try:
            response = await client.post(f"{self.base_url}/Call/", json=body, auth=self._auth)
        except httpx.HTTPError as exc:
            logger.warning("plivo_create_call_transport_error", error=str(exc))
            return CreateCallResult("rejected", None, str(exc)[:255])

        if response.status_code not in (200, 201, 202):
            detail = response.text[:255]
            logger.warning(
                "plivo_create_call_rejected",
                status_code=response.status_code,
                detail=detail,
            )
            return CreateCallResult("rejected", None, detail)

        try:
            payload = response.json()
        except Exception:
            payload = {}
        provider_call_id = payload.get("request_uuid") if isinstance(payload, dict) else None
        return CreateCallResult("accepted", provider_call_id)

    def render_commands(self, commands: list[VoiceCommand]) -> str:
        parts = ['<?xml version="1.0" encoding="utf-8"?><Response>']

        for command in commands:
            if isinstance(command, Speak):
                parts.append(f"<Speak>{escape(command.text)}</Speak>")
            elif isinstance(command, Play):
                parts.append(f"<Play>{escape(command.url)}</Play>")
            elif isinstance(command, Gather):
                action_url = self._callback_url("gather", command.action_tag)
                attrs = (
                    f'numDigits="{command.max_digits}" '
                    f"finishOnKey={quoteattr(command.terminating_digit)} "
                    f'timeout="{command.timeout_seconds}" '
                    f"action={quoteattr(action_url)}"
                )
                if command.prompt is None:
                    parts.append(f"<GetDigits {attrs}/>")
                else:
                    parts.append(f"<GetDigits {attrs}>")
                    prompt = command.prompt
                    if isinstance(prompt, Speak):
                        parts.append(f"<Speak>{escape(prompt.text)}</Speak>")
                    elif isinstance(prompt, Play):
                        parts.append(f"<Play>{escape(prompt.url)}</Play>")
                    else:
                        raise ValueError(f"Unsupported gather prompt {type(prompt).__name__}")
                    parts.append("</GetDigits>")
            elif isinstance(command, StartRecording):
                parts.append("<Record/>")
            elif isinstance(command, StopRecording):
                logger.debug("plivo_voice_stop_recording_has_no_verb")
                continue
            elif isinstance(command, Transfer):
                parts.append(
                    f"<Dial callerId={quoteattr(command.from_)}>"
                    f"<Number>{escape(command.to)}</Number></Dial>"
                )
            elif isinstance(command, Hangup):
                parts.append("<Hangup/>")
            elif isinstance(command, Pause):
                parts.append(f'<Wait length="{int(round(command.seconds))}"/>')
            else:
                raise ValueError(f"Unsupported voice command type {type(command).__name__}")

        parts.append("</Response>")
        return "".join(parts)

    async def execute_commands(
        self, provider_call_id: str, commands: list[VoiceCommand]
    ) -> None:
        raise FeatureUnavailableError(
            "Plivo delivers voice commands in the webhook response (Plivo XML); "
            "mid-call changes need the update-call path (POST /Call/{call_uuid}/)"
        )

    def verify_voice_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return msg_webhooks.verify(headers, self._auth_token, self._webhook_url)

    def parse_voice_webhook(self, raw_body: bytes) -> list[VoiceEvent]:
        fields = msg_webhooks.parse_form(raw_body)
        if fields is None:
            return []

        call_uuid = fields.get("CallUUID", "")
        if not call_uuid:
            return []

        canonical: str | None = None
        digits = ""
        hangup_cause = ""
        recording_url = ""
        provider_recording_id = ""

        if "Digits" in fields:
            canonical = "dtmf_received"
            digits = fields.get("Digits", "")
        elif fields.get("RecordUrl") or fields.get("RecordingID"):
            canonical = "recording_ready"
            recording_url = fields.get("RecordUrl", "")
            provider_recording_id = fields.get("RecordingID", "")
        elif "MachineDetection" in fields or "Machine" in fields:
            raw_machine = (fields.get("MachineDetection") or fields.get("Machine") or "")
            canonical = (
                "machine_detected"
                if raw_machine.strip().lower() in ("true", "machine", "1", "yes")
                else "human_detected"
            )
        else:
            status = fields.get("CallStatus", "")
            event_field = fields.get("Event", "")
            canonical = _CALL_STATUS_TO_EVENT.get(status.strip().lower()) or _EVENT_TO_EVENT.get(
                event_field.strip().lower()
            )
            if canonical == "call_hungup":
                hangup_cause = status or event_field

        if canonical is None:
            logger.warning(
                "plivo_unknown_voice_event",
                call_status=fields.get("CallStatus"),
                call_event=fields.get("Event"),
            )
            return []

        raw_time = fields.get("EventTime", "") or fields.get("Time", "")
        provider_event_id = _plivo_voice_event_id(canonical, call_uuid, raw_time)

        return [
            VoiceEvent(
                event_type=canonical,
                provider_call_id=call_uuid,
                provider_event_id=provider_event_id,
                to=fields.get("To", ""),
                from_=fields.get("From", ""),
                digits=digits,
                recording_url=recording_url,
                provider_recording_id=provider_recording_id,
                hangup_cause=hangup_cause,
                tag=fields.get("tag", ""),
                occurred_at=None,
                raw=fields,
            )
        ]

    def recording_auth(self, url: str) -> tuple[str, str] | None:
        try:
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        if host == "plivo.com" or host.endswith(".plivo.com"):
            return self._auth
        return None
