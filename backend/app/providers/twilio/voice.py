"""Twilio voice: TwiML render + call control.

Twilio is document-return, exactly like Bandwidth (`bandwidth/voice.py`): commands are
delivered by rendering a TwiML document and returning it as the webhook response body, not
by an out-of-band API call against a live leg. `execute_commands` therefore raises
`FeatureUnavailableError` for the same reason Bandwidth's does - mid-call changes need the
update-call/Redirect path, which is a second HTTP round-trip against an already-answered
call, not something this method can express.

`provider_event_id` is computed the same way Bandwidth's is (`_bandwidth_event_id`): Twilio
has no event id of its own, so a stable id is derived by hashing (CallSid, an event-specific
key, a timestamp) - deterministic so the same webhook delivered twice (Twilio retries)
produces the same id and downstream dedupe works.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode
from xml.sax.saxutils import escape, quoteattr

import httpx
import structlog

from app.errors import FeatureUnavailableError
from app.providers.twilio import webhooks as msg_webhooks
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

#: Twilio CallStatus -> our canonical event vocabulary. busy/failed/no-answer are terminal
#: statuses with no dedicated canonical event, so they map to call_hungup with the status
#: preserved as hangup_cause - the same choice Bandwidth makes for its `cause` field.
_CALL_STATUS_TO_EVENT = {
    "queued": "call_initiated",
    "ringing": "call_ringing",
    "in-progress": "call_answered",
    "completed": "call_hungup",
    "busy": "call_hungup",
    "failed": "call_hungup",
    "no-answer": "call_hungup",
}
_TERMINAL_CAUSES = {"busy", "failed", "no-answer"}


def _form(raw_body: bytes) -> dict[str, str]:
    return dict(parse_qsl(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True))


def _parse_twilio_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _twilio_voice_event_id(call_sid: str, event_key: str, timestamp: str) -> str:
    digest = hashlib.sha256(f"{call_sid}:{event_key}:{timestamp}".encode()).hexdigest()
    return f"twilio-voice-{digest}"


class TwilioVoiceMixin:
    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        machine_detection: str = "off",
        tag: str = "",
    ) -> CreateCallResult:
        webhook_url = getattr(self, "_voice_webhook_url", "") or getattr(
            self, "_webhook_url", ""
        )
        body: list[tuple[str, str]] = [
            ("To", to),
            ("From", from_),
            ("Url", webhook_url),
            ("StatusCallback", webhook_url),
        ]
        if machine_detection == "async":
            body.append(("MachineDetection", "Enable"))
            body.append(("AsyncAmd", "true"))

        client = await self._get_client()
        try:
            # Encoded by hand, not via `data=` - see the same note in adapter.py: a plain
            # list-of-tuples body is mishandled by this httpx version.
            response = await client.post(
                f"{self.base_url}/Calls.json",
                content=urlencode(body),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            logger.warning("twilio_create_call_transport_error", error=str(exc))
            return CreateCallResult("rejected", None, str(exc)[:255])

        if response.status_code not in (200, 201):
            detail = response.text[:255]
            logger.warning(
                "twilio_create_call_rejected",
                status_code=response.status_code,
                detail=detail,
            )
            return CreateCallResult("rejected", None, detail)

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return CreateCallResult("accepted", payload.get("sid"))

    def render_commands(self, commands: list[VoiceCommand]) -> str:
        parts = ['<?xml version="1.0" encoding="UTF-8"?><Response>']
        voice_webhook_url = getattr(self, "_voice_webhook_url", "") or getattr(
            self, "_webhook_url", ""
        )

        for command in commands:
            if isinstance(command, Speak):
                parts.append(
                    f"<Say voice={quoteattr(command.voice)}>{escape(command.text)}</Say>"
                )
            elif isinstance(command, Play):
                parts.append(f"<Play>{escape(command.url)}</Play>")
            elif isinstance(command, Gather):
                attrs = (
                    f'numDigits="{command.max_digits}" '
                    f'finishOnKey="{command.terminating_digit}" '
                    f'timeout="{command.timeout_seconds}"'
                )
                if voice_webhook_url:
                    attrs += f" action={quoteattr(voice_webhook_url)}"
                if command.prompt is None:
                    parts.append(f"<Gather {attrs}/>")
                else:
                    parts.append(f"<Gather {attrs}>")
                    prompt = command.prompt
                    if isinstance(prompt, Speak):
                        parts.append(
                            f"<Say voice={quoteattr(prompt.voice)}>"
                            f"{escape(prompt.text)}</Say>"
                        )
                    elif isinstance(prompt, Play):
                        parts.append(f"<Play>{escape(prompt.url)}</Play>")
                    else:
                        raise ValueError(
                            f"Unsupported gather prompt {type(prompt).__name__}"
                        )
                    parts.append("</Gather>")
            elif isinstance(command, StartRecording):
                parts.append("<Record/>")
            elif isinstance(command, StopRecording):
                # TwiML has no stop-recording verb - a live recording can only be stopped
                # via the REST API against the recording resource, which is not expressible
                # as a document command. Omitted, loudly, rather than silently dropped.
                logger.debug(
                    "twilio_stop_recording_has_no_twiml_verb",
                    detail="TwiML has no <Stop><Recording/></Stop> equivalent document verb",
                )
            elif isinstance(command, Transfer):
                parts.append(
                    f"<Dial callerId={quoteattr(command.from_)}>"
                    f"<Number>{escape(command.to)}</Number></Dial>"
                )
            elif isinstance(command, Hangup):
                parts.append("<Hangup/>")
            elif isinstance(command, Pause):
                parts.append(f'<Pause length="{int(command.seconds)}"/>')
            else:
                raise ValueError(f"Unsupported voice command type {type(command).__name__}")

        parts.append("</Response>")
        return "".join(parts)

    async def execute_commands(
        self, provider_call_id: str, commands: list[VoiceCommand]
    ) -> None:
        raise FeatureUnavailableError(
            "Twilio delivers voice commands in the webhook response (TwiML); "
            "mid-call changes need the update-call/Redirect path"
        )

    def verify_voice_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        webhook_url = getattr(self, "_voice_webhook_url", "") or getattr(
            self, "_webhook_url", ""
        )
        return msg_webhooks.verify(headers, self._auth_token, raw_body, webhook_url)

    def parse_voice_webhook(self, raw_body: bytes) -> list[VoiceEvent]:
        params = _form(raw_body)
        if not params:
            return []

        call_sid = params.get("CallSid", "")
        if not call_sid:
            return []

        timestamp = params.get("Timestamp", "")
        digits = params.get("Digits", "")
        recording_url = params.get("RecordingUrl", "")
        recording_sid = params.get("RecordingSid", "")
        answered_by = params.get("AnsweredBy", "")
        call_status = params.get("CallStatus", "")

        hangup_cause = ""
        if digits:
            canonical = "dtmf_received"
            event_key = f"digits:{digits}"
        elif recording_url or recording_sid:
            canonical = "recording_ready"
            event_key = f"recording:{recording_sid}"
        elif answered_by:
            canonical = (
                "machine_detected" if answered_by.startswith("machine") else "human_detected"
            )
            event_key = f"amd:{answered_by}"
        elif call_status:
            canonical = _CALL_STATUS_TO_EVENT.get(call_status)
            event_key = f"status:{call_status}"
            if canonical is None:
                logger.warning("twilio_unknown_voice_call_status", call_status=call_status)
                return []
            if call_status in _TERMINAL_CAUSES:
                hangup_cause = call_status
        else:
            logger.warning("twilio_voice_webhook_unrecognized", keys=list(params.keys()))
            return []

        return [
            VoiceEvent(
                event_type=canonical,
                provider_call_id=call_sid,
                provider_event_id=_twilio_voice_event_id(call_sid, event_key, timestamp),
                to=params.get("To", ""),
                from_=params.get("From", ""),
                digits=digits,
                recording_url=recording_url,
                provider_recording_id=recording_sid,
                hangup_cause=hangup_cause,
                tag="",
                occurred_at=_parse_twilio_timestamp(timestamp),
                raw=params,
            )
        ]

    def recording_auth(self, url: str) -> tuple[str, str] | None:
        try:
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        return self._auth if (host == "twilio.com" or host.endswith(".twilio.com")) else None
