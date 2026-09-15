"""SignalWire voice - the document-return side of the CAL, because LaML is TwiML.

SignalWire's Compatibility API is Twilio's, so this adapter has the same shape as Plivo's:
commands become XML rendered into the webhook response, there is no live command channel we
can drive - with Twilio's vocabulary instead of Plivo's. Four differences are worth naming:

1. `create_call`'s `sid` IS the CallSid every later webhook keys on. Plivo's create response
   returns a `request_uuid` no webhook ever repeats, so its `CreateCallResult` id can only
   correlate; here it matches the first status callback directly.
2. There is no out-of-band command API worth the name - the Compatibility surface exposes
   exactly one mid-call mutation (update this call: `Url=` to redirect, `Status=completed` to
   end it). `execute_commands` refuses everything else rather than pretending the CAL's
   richer vocabulary is deliverable.
3. `StopRecording` has no verb here for the same reason it has none on Plivo: `<Record>` ends
   on its own maxLength/silence timeout, and LaML's `<Stop>` only stops verbs opened by
   `<Start>` - there is nothing to render for a `<Record>` already running.
4. `tag` and `Gather.action_tag` have no native LaML field, so both ride the callback URL's
   query string, which LaML echoes back on the following callback.

AMD also arrives as `AnsweredBy` on the ordinary callback, and the CAL has no `amd_result`
event, so a machine answer folds into the same answer event as a human one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode
from xml.sax.saxutils import escape, quoteattr

import httpx
import structlog

from app.errors import FeatureUnavailableError
from app.providers.domain import CarrierError
from app.providers.voice import (
    VOICE_EVENT_TYPES,
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

logger = structlog.get_logger("carrier.signalwire.voice")

#: Twilio's CallStatus -> our canonical vocabulary (app/providers/voice.py). "queued" is the
#: status a call holds before it rings - the closest LaML gets to the "initiated" callback we
#: ask for - so it maps to call_initiated rather than being dropped on the floor.
_CALL_STATUS_TO_EVENT = {
    "queued": "call_initiated",
    "ringing": "call_ringing",
    "in-progress": "call_answered",
    "completed": "call_hungup",
    "busy": "call_hungup",
    "failed": "call_hungup",
    "no-answer": "call_hungup",
    "canceled": "call_hungup",
}

#: Both spellings: SignalWire sends its own header, Twilio sends X-Twilio-Signature, and the
#: Compatibility API has been observed doing either depending on the space.
_SIGNATURE_HEADERS = ("x-twilio-signature", "x-signalwire-signature")

#: StatusCallbackEvent repeats once per value, which is why create_call builds its form as a
#: list of pairs instead of a dict.
_STATUS_CALLBACK_EVENTS = ("initiated", "ringing", "answered", "completed")

#: The CAL's neutral default voice is Plivo's "julie", which LaML has never heard of. An
#: adapter that was never told a voice gets LaML's own default instead of a rejected document
#: at the moment the call connects.
_VOICE_FALLBACK = {"julie": "alice"}

#: Named rather than inlined: this is Twilio's own default, and it is the only thing capping a
#: recording that no command of ours can stop.
_MAX_RECORDING_SECONDS = 3600


def _classify(status_code: int, body: object) -> CarrierError:
    """Deferred import: adapter.py mixes this class in, so importing its `classify` at module
    scope would close an import cycle. One taxonomy for messaging and voice either way."""
    from app.providers.signalwire.adapter import classify

    return classify(status_code, body)


def _escaped_voice(voice: str) -> str:
    return quoteattr(_VOICE_FALLBACK.get(voice, voice))


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _signalwire_voice_event_id(event_type: str, call_sid: str, discriminator: str) -> str:
    digest = hashlib.sha256(f"{event_type}:{call_sid}:{discriminator}".encode()).hexdigest()
    return f"signalwire-voice-{digest}"


class SignalWireVoiceMixin:
    """Requires `_get_client`, `project_id`, `_api_token` and `base_url` from the composing
    class (see adapter.py); reuses that adapter's `classify` for every failure taxonomy."""

    #: The registry wires these AFTER construction, so they need class-level defaults - a
    #: mixin that raises AttributeError before wiring is a mixin nobody can exercise.
    voice_webhook_url: str = ""
    voice_status_callback_url: str = ""
    voice_signing_url: str = ""

    def _prompt_verb(self, prompt: Speak | Play) -> str:
        """The two verbs that can stand alone OR nest inside <Gather>."""
        if isinstance(prompt, Speak):
            return f"<Say voice={_escaped_voice(prompt.voice)}>{escape(prompt.text)}</Say>"
        if isinstance(prompt, Play):
            return f"<Play>{escape(prompt.url)}</Play>"
        raise ValueError(f"Unsupported gather prompt {type(prompt).__name__}")

    def _transfer_url(self, command: Transfer) -> str:
        """LaML can only redirect to a document, so the new leg is described in the query
        string of our own webhook URL; that document is what emits the <Dial>."""
        separator = "&" if "?" in self.voice_webhook_url else "?"
        query = urlencode({"action": "transfer", "to": command.to, "from": command.from_})
        return f"{self.voice_webhook_url}{separator}{query}"

    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        machine_detection: str = "off",
        #: Accepted for protocol compatibility and deliberately NOT sent: see the
        #: StatusCallback comment below. CallSid is what correlates on this carrier.
        tag: str = "",
    ) -> CreateCallResult:
        form: list[tuple[str, str]] = [
            ("To", to),
            ("From", from_),
            ("Url", self.voice_webhook_url),
            # NO query string, deliberately. SignalWire signs the URL it called, and
            # verify_voice_webhook only knows the configured URL - a "?tag=" we cannot
            # reconstruct at verify time would make every callback fail closed. The
            # CallSid returned here is the same id every later callback carries, so
            # correlation never needed the tag. Same fixed-URL rule as the Plivo adapter.
            ("StatusCallback", self.voice_status_callback_url),
            ("StatusCallbackMethod", "POST"),
        ]
        form.extend(("StatusCallbackEvent", event) for event in _STATUS_CALLBACK_EVENTS)
        if machine_detection == "async":
            # Sent only for "async": the field is what opts the call into AMD, and every
            # opted-in call pays its detection latency whether or not we read the result.
            form.append(("MachineDetection", "Enable"))
            form.append(("MachineDetectionTimeout", "30"))

        client = await self._get_client()
        try:
            response = await client.post(
                f"{self.base_url}/Calls.json",
                content=urlencode(form),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=(self.project_id, self._api_token),
            )
        except httpx.TransportError as exc:
            logger.warning("carrier_unreachable", error=str(exc))
            detail = str(exc)[:255]
            # Retryable by construction: nothing about the request was judged, it just never
            # arrived. This object is what feeds the breaker.
            return CreateCallResult(
                status="rejected",
                error_detail=detail,
                error=CarrierError("carrier_unreachable", None, retryable=True, detail=detail),
            )

        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:255]}

        if response.status_code in (200, 201, 202):
            sid = payload.get("sid") if isinstance(payload, dict) else None
            return CreateCallResult(status="accepted", provider_call_id=str(sid) if sid else None)

        # A carrier saying no is a routing decision, not a bug: report it, never raise it.
        error = _classify(response.status_code, payload)
        logger.warning("carrier_rejected", status=response.status_code, category=error.category)
        return CreateCallResult(status="rejected", error_detail=error.detail, error=error)

    def render_commands(self, commands: list[VoiceCommand]) -> str:
        parts = ['<?xml version="1.0" encoding="utf-8"?><Response>']

        for command in commands:
            if isinstance(command, (Speak, Play)):
                parts.append(self._prompt_verb(command))
            elif isinstance(command, Gather):
                # The prompt is nested INSIDE <Gather> so a keypress barges in on it instead
                # of queueing behind the whole prompt.
                action = self.voice_webhook_url
                attrs = (
                    f'numDigits="{command.max_digits}" '
                    f"finishOnKey={quoteattr(command.terminating_digit)} "
                    f'timeout="{command.timeout_seconds}" '
                    f"action={quoteattr(action)} "
                    f'method="POST"'
                )
                if command.prompt is None:
                    parts.append(f"<Gather {attrs}/>")
                else:
                    parts.append(f"<Gather {attrs}>{self._prompt_verb(command.prompt)}</Gather>")
            elif isinstance(command, StartRecording):
                # action points at the status callback: LaML reports the finished recording
                # through RecordingUrl/RecordingSid on that same URL.
                attrs = (
                    f"action={quoteattr(self.voice_status_callback_url)} "
                    f'maxLength="{_MAX_RECORDING_SECONDS}" '
                    f"channels={quoteattr(command.channels)}"
                )
                parts.append(f"<Record {attrs}/>")
            elif isinstance(command, StopRecording):
                # Nothing to render: <Record> ends on its own maxLength/silence timeout, and
                # LaML offers no way to reach into one that a previous document started.
                logger.debug("signalwire_voice_stop_recording_has_no_verb")
                continue
            elif isinstance(command, Transfer):
                parts.append(
                    f"<Dial callerId={quoteattr(command.from_)}>"
                    f"<Number>{escape(command.to)}</Number></Dial>"
                )
            elif isinstance(command, Hangup):
                parts.append("<Hangup/>")
            elif isinstance(command, Pause):
                # LaML rejects length="0"; a sub-second pause rounds up to a full second.
                parts.append(f'<Pause length="{max(1, int(round(command.seconds)))}"/>')
            else:
                raise ValueError(f"Unsupported voice command type {type(command).__name__}")

        parts.append("</Response>")
        return "".join(parts)

    async def execute_commands(
        self, provider_call_id: str, commands: list[VoiceCommand]
    ) -> None:
        client = await self._get_client()

        for command in commands:
            if isinstance(command, Hangup):
                form: list[tuple[str, str]] = [("Status", "completed")]
            elif isinstance(command, Transfer):
                form = [("Url", self._transfer_url(command))]
            else:
                raise FeatureUnavailableError(
                    "SignalWire changes a live call only by redirecting it to new LaML "
                    "(Url=) or ending it (Status=completed); speaking, playing, gathering "
                    "and recording have to be returned as LaML in a webhook response"
                )

            try:
                response = await client.post(
                    f"{self.base_url}/Calls/{provider_call_id}.json",
                    content=urlencode(form),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    auth=(self.project_id, self._api_token),
                )
            except httpx.TransportError as exc:
                # CarrierError is a dataclass, not an exception: it describes a failure for
                # the breaker, it cannot BE the failure. Raise what callers already handle.
                logger.warning("carrier_unreachable", error=str(exc))
                raise FeatureUnavailableError(
                    "Could not reach SignalWire to change this call"
                ) from exc

            if response.status_code not in (200, 201, 202):
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = {"message": response.text[:255]}
                error = _classify(response.status_code, error_body)
                logger.warning(
                    "signalwire_call_control_rejected",
                    status=response.status_code,
                    category=error.category,
                    command=type(command).__name__,
                )
                raise FeatureUnavailableError(
                    "SignalWire refused this change to the live call"
                )

    def verify_voice_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        signing_url = self.voice_signing_url
        if not signing_url:
            # Fail CLOSED. With nothing to sign against there is no way to tell a real
            # callback from anyone who knows the webhook path, and guessing "true" is how a
            # webhook becomes an open command channel.
            logger.warning("signalwire_voice_signature_unverifiable")
            return False

        signature = ""
        for name, value in headers.items():
            if name.lower() in _SIGNATURE_HEADERS:
                signature = value
                break
        if not signature:
            logger.warning("signalwire_voice_signature_missing")
            return False

        params = parse_qsl(raw_body.decode("utf-8", "replace"), keep_blank_values=True)
        params.sort(key=lambda pair: pair[0])
        # Twilio's scheme: the URL, then each POST param as name immediately followed by its
        # value, in name order, with nothing at all between one pair and the next.
        payload = signing_url + "".join(name + value for name, value in params)
        digest = hmac.new(
            self._api_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
        ).digest()
        expected = base64.b64encode(digest).decode("ascii")
        # compare_digest, not ==: a short-circuiting comparison leaks how much of a guess was
        # right. Neither the signature nor the token is ever logged.
        if not hmac.compare_digest(expected, signature):
            logger.warning("signalwire_voice_signature_mismatch")
            return False
        return True

    def parse_voice_webhook(self, raw_body: bytes) -> list[VoiceEvent]:
        # Form-encoded, NOT JSON: the Compatibility API speaks urlencoded callbacks.
        fields = dict(parse_qsl(raw_body.decode("utf-8", "replace"), keep_blank_values=True))

        call_sid = fields.get("CallSid", "")
        if not call_sid:
            # Without a leg id there is nothing to attach an event to.
            return []

        status = fields.get("CallStatus", "")
        digits = fields.get("Digits", "")
        recording_url = fields.get("RecordingUrl", "")
        answered_by = fields.get("AnsweredBy", "")

        if recording_url:
            event_type = "recording_ready"
        elif digits:
            event_type = "dtmf_received"
        elif answered_by:
            # Same shape as Plivo and Bandwidth: AMD replaces the answer event rather than
            # riding alongside it, so the service layer reads one answer per leg.
            event_type = (
                "machine_detected"
                if answered_by.strip().lower().startswith("machine")
                else "human_detected"
            )
        else:
            event_type = _CALL_STATUS_TO_EVENT.get(status.strip().lower(), "")

        if event_type not in VOICE_EVENT_TYPES:
            logger.warning("signalwire_unknown_voice_event", call_status=status)
            return []

        duration = _int_or_none(
            fields.get("RecordingDuration") or fields.get("CallDuration") or ""
        )

        return [
            VoiceEvent(
                event_type=event_type,
                provider_call_id=call_sid,
                provider_event_id=_signalwire_voice_event_id(
                    event_type,
                    call_sid,
                    f"{status}:{digits}:{fields.get('RecordingSid', '')}",
                ),
                to=fields.get("To", ""),
                from_=fields.get("From", ""),
                digits=digits,
                recording_url=recording_url,
                provider_recording_id=fields.get("RecordingSid", ""),
                duration_seconds=duration,
                hangup_cause=status if event_type == "call_hungup" else "",
                tag=fields.get("tag", ""),
                occurred_at=None,
                raw=fields,
            )
        ]

    def recording_auth(self, url: str) -> tuple[str, str] | None:
        """Our credentials, but only for our own space's host.

        Exact host, not a "signalwire.com" suffix: a webhook payload is attacker-influenced,
        and a suffix match would hand the project token to any host on the parent domain.
        """
        try:
            own_host = httpx.URL(self.base_url).host or ""
            host = httpx.URL(url).host or ""
        except Exception:
            return None
        if own_host and host == own_host:
            return (self.project_id, self._api_token)
        return None
