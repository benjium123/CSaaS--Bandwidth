"""Build the Telnyx ``POST /10dlc/campaignBuilder`` body from a local Campaign.

Pure translation: no I/O, no persistence, no carrier calls. Consent and financial
attestations cannot come from a ``Campaign``, so they arrive through ``assertions``,
from explicit user input: we never invent a boolean, a sample, or consent.
``termsAndConditions`` must be an explicit True; ``autoRenewal`` must be present.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from app.errors import ValidationFailedError
from app.models.numbers import Campaign

ASSERTION_FIELDS: tuple[str, ...] = (
    "ageGated", "autoRenewal", "directLending", "embeddedLink", "embeddedPhone",
    "numberPool", "subscriberHelp", "subscriberOptin", "subscriberOptout",
    "termsAndConditions",
)
#: termsAndConditions is true-or-nothing; autoRenewal must be present either way.
REQUIRED_ASSERTIONS: tuple[str, ...] = ("autoRenewal", "termsAndConditions")
MAX_SAMPLES = 5
OPTIONAL_TEXT = {"helpMessage": "help_message", "optoutMessage": "opt_out_message"}


def _fail(field: str) -> NoReturn:
    """Name the field only - never a value, a message body, or a carrier id."""
    raise ValidationFailedError(f"campaign field {field!r} is missing or invalid")


def _text(campaign: Campaign, field: str, *, required: bool) -> str | None:
    raw = getattr(campaign, field, None)
    value = raw.strip() if isinstance(raw, str) else ""
    if value:
        return value
    if required:
        _fail(field)
    return None


def _usecase(campaign: Campaign) -> str:
    """Keep the stored code as-is; only assert it is an uppercase token."""
    value = _text(campaign, "use_case", required=True) or ""
    if value != value.upper() or not value.replace("_", "").isalnum() or not value.isascii():
        _fail("use_case")
    return value


def _samples(campaign: Campaign) -> list[str]:
    """One real sample minimum, five maximum - reject rather than silently drop."""
    raw = getattr(campaign, "sample_messages", None)
    items = list(raw) if isinstance(raw, (list, tuple)) else []
    if not 1 <= len(items) <= MAX_SAMPLES:
        _fail("sample_messages")
    if any(not isinstance(item, str) or not item.strip() for item in items):
        _fail("sample_messages")
    return [item.strip() for item in items]


def _assertions(assertions: Mapping[str, bool]) -> dict[str, bool]:
    """Allow-listed, explicit attestations only; never guess one for the user."""
    provided = dict(assertions or {})
    for name, value in provided.items():
        if name not in ASSERTION_FIELDS or not isinstance(value, bool):
            # Never echo a caller-supplied key: it is not ours and may carry PII.
            _fail("assertions")
    for name in REQUIRED_ASSERTIONS:
        if name not in provided:
            _fail(name)
    if provided["termsAndConditions"] is not True:
        _fail("termsAndConditions")
    return {name: provided[name] for name in ASSERTION_FIELDS if name in provided}


#: Telnyx /10dlc/enum/usecase: use cases that need sub-use-cases, as (min, max), and the
#: use cases allowed AS a sub-use-case (validSubUsecase=true).
SUB_USECASE_BOUNDS: dict[str, tuple[int, int]] = {
    "MIXED": (2, 5),
    "LOW_VOLUME": (1, 5),
    "SOLE_PROPRIETOR": (1, 5),
}
VALID_SUB_USECASES: frozenset[str] = frozenset(
    {
        "2FA",
        "ACCOUNT_NOTIFICATION",
        "CUSTOMER_CARE",
        "DELIVERY_NOTIFICATION",
        "FRAUD_ALERT",
        "HIGHER_EDUCATION",
        "MARKETING",
        "POLLING_VOTING",
        "PUBLIC_SERVICE_ANNOUNCEMENT",
        "SECURITY_ALERT",
    }
)


def _sub_usecases(usecase: str, sub_usecases: Sequence[str] | None) -> list[str] | None:
    """The carrier refuses MIXED / LOW_VOLUME / SOLE_PROPRIETOR without these."""
    chosen = list(dict.fromkeys(str(s).strip().upper() for s in (sub_usecases or []) if s))
    bounds = SUB_USECASE_BOUNDS.get(usecase)
    if bounds is None:
        return None
    low, high = bounds
    if not low <= len(chosen) <= high or any(s not in VALID_SUB_USECASES for s in chosen):
        _fail("sub_usecases")
    return chosen


def build_campaign_payload(
    campaign: Campaign,
    *,
    telnyx_brand_id: str,
    assertions: Mapping[str, bool],
    sub_usecases: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return the campaignBuilder body; raises ValidationFailedError on bad input."""
    brand_id = telnyx_brand_id.strip() if isinstance(telnyx_brand_id, str) else ""
    if not brand_id:
        _fail("telnyx_brand_id")
    payload: dict[str, Any] = {
        "brandId": brand_id,
        "description": _text(campaign, "description", required=True),
        "usecase": _usecase(campaign),
        "messageFlow": _text(campaign, "opt_in_process", required=True),
    }
    if (subs := _sub_usecases(payload["usecase"], sub_usecases)) is not None:
        payload["subUsecases"] = subs
    for index, sample in enumerate(_samples(campaign), 1):
        payload[f"sample{index}"] = sample
    for wire, field in OPTIONAL_TEXT.items():
        if text := _text(campaign, field, required=False):
            payload[wire] = text
    payload.update(_assertions(assertions))
    return payload
