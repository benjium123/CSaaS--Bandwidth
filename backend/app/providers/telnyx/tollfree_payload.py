"""Pure, fail-closed builder for the Telnyx toll-free verification request body.

Maps operator-supplied values onto the documented request contract for
``POST /v2/messaging_tollfree/verification/requests``. Pure: no I/O, no state, no
logging (never logs PII) and no fabricated defaults - any missing, blank or
malformed required value raises ``ValidationFailedError`` before the transport in
``tollfree_verification.py`` can send anything.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from app.errors import ValidationFailedError

#: Documented ``messageVolume`` enum. Exact strings, never integers.
MESSAGE_VOLUMES = (
    "10", "100", "1,000", "10,000", "100,000", "250,000", "500,000", "750,000",
    "1,000,000", "5,000,000", "10,000,000+",
)

#: Documented ``useCase`` enum. Closed set - any other value is rejected.
USE_CASES = (
    "Mixed", "2FA", "General Marketing", "Appointments", "Conversational / Alerts",
)

#: Required plain-text request keys (camelCase, per the Telnyx contract).
_REQUIRED_TEXT_FIELDS = (
    "businessName", "corporateWebsite", "businessAddr1", "businessCity",
    "businessState", "businessZip", "businessContactFirstName", "businessContactLastName",
    "businessContactEmail", "businessContactPhone", "useCaseSummary",
    "productionMessageContent", "optInWorkflow", "additionalInformation",
)

#: Mandatory for business (non-sole-proprietor) filings per Feb 2026 guidance.
_REGISTRATION_FIELDS = (
    "businessRegistrationNumber", "businessRegistrationType", "businessRegistrationCountry",
)

#: Local use-case codes mapped to their Telnyx enum value.
_USE_CASE_ALIASES = {"MIXED": "Mixed"}

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
#: NANP toll-free: +1 then a toll-free area code and 7 digits.
_TOLLFREE = re.compile(r"^\+1(?:800|833|844|855|866|877|888)\d{7}$")
_IMAGE_SCHEMES = ("http", "https")


def _text(fields: Mapping[str, Any], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailedError(f"Telnyx toll-free verification requires {key}")
    return value.strip()


def _message_volume(value: Any) -> str:
    if not isinstance(value, str) or value not in MESSAGE_VOLUMES:
        raise ValidationFailedError(
            "Telnyx toll-free verification messageVolume must be one of: "
            + ", ".join(MESSAGE_VOLUMES)
        )
    return value


def _use_case(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailedError("Telnyx toll-free verification requires useCase")
    raw = value.strip()
    resolved = _USE_CASE_ALIASES.get(raw.upper(), raw)
    if resolved not in USE_CASES:
        raise ValidationFailedError(
            "Telnyx toll-free verification useCase must be one of: " + ", ".join(USE_CASES)
        )
    return resolved


def _image_urls(value: Any) -> list[dict[str, str]]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, Sequence) or not items:
        raise ValidationFailedError("Telnyx toll-free verification requires optInWorkflowImageURLs")
    urls = []
    for item in items:
        url = item.get("url") if isinstance(item, Mapping) else item
        if not isinstance(url, str) or not url.strip():
            raise ValidationFailedError("optInWorkflowImageURLs entries need a url")
        url = url.strip()
        parts = urlsplit(url)
        if parts.scheme not in _IMAGE_SCHEMES or not parts.netloc:
            raise ValidationFailedError(
                "optInWorkflowImageURLs entries must be http(s) URLs"
            )
        urls.append({"url": url})
    return urls


def build_tollfree_verification_payload(
    *,
    phone_number: str,
    fields: Mapping[str, Any],
    sole_proprietor: bool,
) -> dict[str, Any]:
    """Return a validated Telnyx toll-free verification body, or raise.

    ``fields`` carries operator-supplied request values under their documented
    camelCase names. ``sole_proprietor`` is required (no default) so business
    filings cannot silently omit the three registration fields.
    """
    if not isinstance(fields, Mapping):
        raise ValidationFailedError("Telnyx toll-free verification needs supplied fields")
    if "verificationStatus" in fields:
        raise ValidationFailedError("verificationStatus is response-only and cannot be sent")

    number = phone_number.strip() if isinstance(phone_number, str) else ""
    if not _E164.match(number):
        raise ValidationFailedError("Telnyx toll-free verification number must be E.164")
    if not _TOLLFREE.match(number):
        raise ValidationFailedError("Telnyx toll-free verification number must be toll-free")

    payload: dict[str, Any] = {key: _text(fields, key) for key in _REQUIRED_TEXT_FIELDS}
    payload["useCase"] = _use_case(fields.get("useCase"))
    payload["messageVolume"] = _message_volume(fields.get("messageVolume"))
    payload["phoneNumbers"] = [{"phoneNumber": number}]
    payload["optInWorkflowImageURLs"] = _image_urls(fields.get("optInWorkflowImageURLs"))
    if not sole_proprietor:
        for key in _REGISTRATION_FIELDS:
            payload[key] = _text(fields, key)
    return payload
