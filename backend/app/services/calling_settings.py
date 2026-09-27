"""P29 calling settings.

The two org-level calling settings live in one nullable JSON column on ``orgs``
(``org.calling_settings``). Migration 0031 is Fable-owned and adds only the recording
announcement columns; a single nullable JSON blob keeps the additive migration for
P29 to one column. ``calls.disposition`` is ``sa.String(32)``, which is where
``MAX_DISPOSITION_LEN`` comes from.
"""

from __future__ import annotations

from app.errors import ValidationFailedError

DEFAULT_DISPOSITIONS: tuple[str, ...] = (
    "Interested",
    "Not interested",
    "Callback",
    "Voicemail",
    "Wrong number",
    "No answer",
)
CHANNEL_LAYOUTS: frozenset[str] = frozenset({"mixed", "dual"})
DEFAULT_CHANNEL_LAYOUT = "mixed"
#: off | after_call (minutes after the call) | overnight (after calling hours, cheaper)
#: | live (captions during the call; the saved transcript comes from them)
TRANSCRIPTION_MODES: frozenset[str] = frozenset({"off", "after_call", "overnight", "live"})
#: Plain English, no legalese, no product name. Played before connecting.
DEFAULT_ANNOUNCEMENT_TEXT = "This call may be recorded for quality and training."
MAX_DISPOSITIONS = 25
MAX_DISPOSITION_LEN = 32     # calls.disposition is sa.String(32) — the cap IS the column
MAX_ANNOUNCEMENT_LEN = 500


def _stored_settings(org) -> dict:
    settings = getattr(org, "calling_settings", None)
    return settings if isinstance(settings, dict) else {}


def dispositions_for(org) -> list[str]:
    """Return the org's call result list, falling back to platform defaults."""
    raw = _stored_settings(org).get("dispositions")
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(item, str) for item in raw)
    ):
        return list(raw)
    return list(DEFAULT_DISPOSITIONS)


def channel_layout_for(org) -> str:
    """Return the org's recording channel layout, falling back to ``mixed``."""
    raw = _stored_settings(org).get("channel_layout")
    if isinstance(raw, str) and raw in CHANNEL_LAYOUTS:
        return raw
    return DEFAULT_CHANNEL_LAYOUT


def record_calls_for(org) -> bool:
    """Whether the org records every call placed or answered in the app (off by default)."""
    return _stored_settings(org).get("record_calls") is True


def transcription_mode_for(org) -> str:
    """How the org's recorded calls are transcribed, falling back to ``off``."""
    raw = _stored_settings(org).get("transcription_mode")
    return raw if isinstance(raw, str) and raw in TRANSCRIPTION_MODES else "off"


def normalize_transcription_mode(value) -> str:
    if not isinstance(value, str) or value not in TRANSCRIPTION_MODES:
        raise ValidationFailedError("Transcripts must be off, after_call, overnight or live")
    return value


def announcement_text_for(org) -> str:
    """Return the org's custom announcement, stripped, or the platform sentence."""
    raw = getattr(org, "recording_announcement_text", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_ANNOUNCEMENT_TEXT


def announcement_ops_off(org) -> bool:
    """Whether platform ops (super admins) switched the recording notice off for this org."""
    return _stored_settings(org).get("announcement_off") is True


def announcement_enabled(org) -> bool:
    """Whether the recording notice plays: on by default whenever the org records calls,
    unless a super admin switched it off for the org. The org cannot change it itself,
    and safety monitoring never plays it (the legacy recording_announcement column is
    no longer consulted)."""
    return record_calls_for(org) and not announcement_ops_off(org)


def normalize_dispositions(raw) -> list[str]:
    """Validate a caller-supplied call result list and return it stripped in order."""
    if not isinstance(raw, list):
        raise ValidationFailedError("Call results must be a list")
    if not raw:
        raise ValidationFailedError("Call results cannot be empty")
    if len(raw) > MAX_DISPOSITIONS:
        raise ValidationFailedError("You can add at most 25 call results")

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValidationFailedError("Every call result must be text")
        value = item.strip()
        if not value:
            raise ValidationFailedError("Call results cannot be blank")
        if len(value) > MAX_DISPOSITION_LEN:
            raise ValidationFailedError("A call result can be at most 32 characters")
        key = value.casefold()
        if key in seen:
            raise ValidationFailedError("Call results must be unique")
        seen.add(key)
        result.append(value)
    return result


def normalize_channel_layout(raw) -> str:
    """Validate that ``raw`` is one of the two recording channel layouts."""
    if not isinstance(raw, str) or raw not in CHANNEL_LAYOUTS:
        raise ValidationFailedError("Recording layout must be 'mixed' or 'dual'")
    return raw


def normalize_announcement_text(raw) -> str | None:
    """Return stripped custom announcement text, or None for the platform sentence."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationFailedError("Announcement must be text")
    text = raw.strip()
    if not text:
        return None
    if len(text) > MAX_ANNOUNCEMENT_LEN:
        raise ValidationFailedError(
            f"Announcement can be at most {MAX_ANNOUNCEMENT_LEN} characters"
        )
    return text


def apply(
    org, *, dispositions=None, channel_layout=None, record_calls=None, transcription_mode=None
) -> None:
    """Merge supplied calling settings into ``org.calling_settings``.

    SQLAlchemy JSON columns do not track in-place mutation of a dict, so assign a new
    dict instead of mutating the existing one.
    """
    next_settings = dict(_stored_settings(org))
    if dispositions is not None:
        next_settings["dispositions"] = dispositions
    if channel_layout is not None:
        next_settings["channel_layout"] = channel_layout
    if record_calls is not None:
        next_settings["record_calls"] = bool(record_calls)
    if transcription_mode is not None:
        next_settings["transcription_mode"] = transcription_mode

    if (
        dispositions is not None
        or channel_layout is not None
        or record_calls is not None
        or transcription_mode is not None
    ):
        org.calling_settings = next_settings


def as_dict(org) -> dict:
    """Return the full calling-settings payload for the settings page."""
    return {
        "recording_announcement": announcement_enabled(org),
        # Only platform ops can switch the notice; the settings page shows it read-only.
        "recording_announcement_locked": True,
        "recording_announcement_text": getattr(org, "recording_announcement_text", None),
        "announcement_text_effective": announcement_text_for(org),
        "channel_layout": channel_layout_for(org),
        "record_calls": record_calls_for(org),
        "transcription_mode": transcription_mode_for(org),
        "dispositions": dispositions_for(org),
    }
