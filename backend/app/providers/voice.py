"""The carrier-neutral voice interface: event in → command out, async (ARCHITECTURE D2).

This file is the voice counterpart of `providers/domain.py` and it is the AUTHORITY the
adapters implement against. The shape is Telnyx's, deliberately: commands serialize DOWN
onto Bandwidth (the adapter renders the emitted commands as one BXML document returned in
the webhook response), while the reverse - a document-return abstraction - would force
holding HTTP responses open on Telnyx.

The accepted consequence, stated here so nobody is surprised by it later: the Bandwidth
adapter can only express commands that fit "what to do in reply to this one event."
Anything genuinely mid-stream (barge-in `clear`) needs the media WebSocket (P7) or a
second round-trip via update-call/Redirect. Design around that; do not fight it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.errors import FeatureUnavailableError

# ----------------------------------------------------------------------------------
# Commands - frozen, carrier-neutral. Adapters translate; they never interpret.
# ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Speak:
    text: str
    voice: str = "julie"  # a neutral default both carriers can map


@dataclass(frozen=True)
class Play:
    url: str


@dataclass(frozen=True)
class Gather:
    """Collect DTMF. `action_tag` comes back on the dtmf_received event so the service can
    tell WHICH gather this answer belongs to - a call can gather more than once."""

    max_digits: int = 1
    terminating_digit: str = "#"
    timeout_seconds: int = 10
    prompt: Speak | Play | None = None
    action_tag: str = ""


@dataclass(frozen=True)
class StartRecording:
    channels: str = "dual"  # dual keeps the parties separable for later transcription


@dataclass(frozen=True)
class StopRecording:
    pass


@dataclass(frozen=True)
class Transfer:
    """Blind transfer: dial `to` and bridge. The service layer models this as a NEW leg;
    the command merely asks the carrier to place it."""

    to: str
    from_: str


@dataclass(frozen=True)
class Hangup:
    pass


@dataclass(frozen=True)
class Pause:
    seconds: float = 1.0


VoiceCommand = Speak | Play | Gather | StartRecording | StopRecording | Transfer | Hangup | Pause


# ----------------------------------------------------------------------------------
# Events - canonical, already translated from carrier vocabulary by the adapter.
# ----------------------------------------------------------------------------------

#: The full canonical vocabulary. Adapters map INTO this set and drop what has no mapping
#: (logging it); the service layer never sees a carrier-specific event name.
VOICE_EVENT_TYPES = frozenset(
    {
        "call_initiated",
        "call_ringing",
        "call_answered",
        "call_bridged",
        "call_hungup",
        "machine_detected",
        "human_detected",
        "dtmf_received",
        "recording_ready",
        "transfer_completed",
    }
)


@dataclass(frozen=True)
class VoiceEvent:
    event_type: str
    #: The carrier's id for the LEG this event happened on (both carriers' "call id" is a
    #: per-leg id; our Call is the thing they don't have).
    provider_call_id: str
    provider_event_id: str
    to: str = ""
    from_: str = ""
    digits: str = ""
    #: For recording_ready only - the CARRIER's URL, to be fetched with carrier auth and
    #: never exposed further.
    recording_url: str = ""
    provider_recording_id: str = ""
    duration_seconds: int | None = None
    hangup_cause: str = ""
    #: Echo of the tag supplied at call creation / gather time.
    tag: str = ""
    occurred_at: datetime | None = None
    raw: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class CreateCallResult:
    status: str  # "accepted" | "rejected"
    provider_call_id: str | None = None
    error_detail: str = ""


# ----------------------------------------------------------------------------------
# The carrier protocol
# ----------------------------------------------------------------------------------


@runtime_checkable
class VoiceCarrier(Protocol):
    """What a voice-capable adapter provides. Separate from MessagingCarrier for the same
    reason NumberProvider is (P4): capabilities differ per account, and one combined
    interface forces adapters to implement what they cannot honour."""

    name: str

    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        machine_detection: str = "off",  # "off" | "async"
        tag: str = "",
    ) -> CreateCallResult: ...

    def verify_voice_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool: ...

    def parse_voice_webhook(self, raw_body: bytes) -> list[VoiceEvent]: ...

    def render_commands(self, commands: list[VoiceCommand]) -> str | None:
        """Bandwidth: the BXML document to return as the webhook response body.
        Telnyx: None - commands go out-of-band via execute_commands instead."""
        ...

    async def execute_commands(
        self, provider_call_id: str, commands: list[VoiceCommand]
    ) -> None:
        """Telnyx: issue API actions against the live call.
        Bandwidth: only usable for the update-call/Redirect path; the adapter raises
        FeatureUnavailableError for commands it cannot deliver mid-stream."""
        ...


def as_voice_carrier(carrier: object) -> VoiceCarrier:
    """Narrow a registry entry to its voice capability, or say clearly that it has none.

    Same pattern as numbers.as_provider: capability is DECLARED by implementing the
    protocol, never discovered by a failing API call.
    """
    if isinstance(carrier, VoiceCarrier):
        return carrier
    name = getattr(carrier, "name", type(carrier).__name__)
    raise FeatureUnavailableError(
        f"Carrier {name!r} has no voice support in this deployment."
    )
