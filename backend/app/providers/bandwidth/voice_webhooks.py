from __future__ import annotations

import base64
import hmac
import re

_ISO_DURATION_RE = re.compile(
    r"PT"
    r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
)


def parse_iso_duration_seconds(raw: str) -> int | None:
    if not raw:
        return None

    match = _ISO_DURATION_RE.fullmatch(raw.upper())
    if match is None:
        return None

    hours = match.group("hours")
    minutes = match.group("minutes")
    seconds = match.group("seconds")
    if hours is None and minutes is None and seconds is None:
        return None

    return int(float(hours or 0)) * 3600 + int(float(minutes or 0)) * 60 + int(
        float(seconds or 0)
    )


def basic_auth_matches(header_value: str, user: str, password: str) -> bool:
    if not header_value:
        return False

    parts = header_value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return False

    token = parts[1]
    token += "=" * (-len(token) % 4)
    try:
        decoded = base64.b64decode(token)
        received = decoded.decode("utf-8")
    except Exception:
        return False

    expected = f"{user}:{password}"
    return hmac.compare_digest(received, expected)
