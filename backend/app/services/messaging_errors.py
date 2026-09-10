from __future__ import annotations

GENERIC_REASON = "We couldn't deliver this message. Try again in a little while."

MMS_FALLBACK_REASON = (
    "Sent as a text with a link (this number can't receive pictures)"
)

_LANDLINE_REASON = "This number can't receive text messages."
_INVALID_REASON = "That phone number doesn't look like a working number."
_OPTED_OUT_REASON = "This person has asked not to receive messages from you."
_SPAM_BLOCKED_REASON = (
    "The message looked like spam to the phone network and was blocked."
)
_OVER_LIMIT_REASON = (
    "You're sending faster than this number is allowed to right now."
)
_OUTAGE_REASON = "The phone network had a temporary problem. We'll try again."
_UNREGISTERED_REASON = "This number isn't approved to send messages yet."
_ATTACHMENT_REASON = (
    "The attachment was too large or in a format this phone can't open."
)

_CATEGORY_REASONS = {
    "invalid_request": _INVALID_REASON,
    "unregistered": _UNREGISTERED_REASON,
    "rate_limited": _OVER_LIMIT_REASON,
    "carrier_transient": _OUTAGE_REASON,
    "carrier_unreachable": _OUTAGE_REASON,
    "auth": "This number's sending setup needs attention. Contact support.",
}

_CODE_REASONS = {
    "4700": _INVALID_REASON,
    "4701": _INVALID_REASON,
    "4702": _INVALID_REASON,
    "4711": _INVALID_REASON,
    "4432": _OPTED_OUT_REASON,
    "4434": _OPTED_OUT_REASON,
    "5106": _OPTED_OUT_REASON,
    "4720": _LANDLINE_REASON,
    "4721": _LANDLINE_REASON,
    "4750": _SPAM_BLOCKED_REASON,
    "4751": _SPAM_BLOCKED_REASON,
    "4752": _SPAM_BLOCKED_REASON,
    "4753": _SPAM_BLOCKED_REASON,
    "4754": _SPAM_BLOCKED_REASON,
    "4770": _SPAM_BLOCKED_REASON,
    "4771": _SPAM_BLOCKED_REASON,
    "4772": _SPAM_BLOCKED_REASON,
    "4773": _SPAM_BLOCKED_REASON,
    "4774": _SPAM_BLOCKED_REASON,
    "4775": _SPAM_BLOCKED_REASON,
    "4405": _OVER_LIMIT_REASON,
    "4406": _OVER_LIMIT_REASON,
    "4407": _OVER_LIMIT_REASON,
    "5100": _OVER_LIMIT_REASON,
    "4401": _OUTAGE_REASON,
    "4403": _OUTAGE_REASON,
    "5000": _OUTAGE_REASON,
    "5001": _OUTAGE_REASON,
    "4451": _UNREGISTERED_REASON,
    "4452": _UNREGISTERED_REASON,
    "4470": _UNREGISTERED_REASON,
    "4471": _UNREGISTERED_REASON,
    "4480": _UNREGISTERED_REASON,
    "4740": _ATTACHMENT_REASON,
    "4741": _ATTACHMENT_REASON,
}

# Exact carrier-specific mappings. Kept separate so a provider exception always wins
# over the generic code table, matching the required resolution order.
_CARRIER_CODE_REASONS: dict[tuple[str, str], str] = {}

# Family match covers codes the provider publishes as ranges, e.g. 4750-4754.
_CODE_FAMILY_RANGES: tuple[tuple[int, int, str], ...] = (
    (4750, 4754, _SPAM_BLOCKED_REASON),
    (4770, 4775, _SPAM_BLOCKED_REASON),
)

_FAILURE_STATUSES = frozenset({"failed", "rejected", "undelivered"})

_ALL_REASONS = {
    GENERIC_REASON,
    MMS_FALLBACK_REASON,
    _LANDLINE_REASON,
    _INVALID_REASON,
    _OPTED_OUT_REASON,
    _SPAM_BLOCKED_REASON,
    _OVER_LIMIT_REASON,
    _OUTAGE_REASON,
    _UNREGISTERED_REASON,
    _ATTACHMENT_REASON,
}
_ALL_REASONS.update(_CATEGORY_REASONS.values())

ALL_REASONS: frozenset[str] = frozenset(_ALL_REASONS)


def public_reason(
    *,
    carrier: str | None = None,
    error_code: str | None = None,
    category: str | None = None,
    detail: str | None = None,
) -> str:
    """Return the plain-English sentence for one failed message (<= 255 chars).

    Resolution order is deliberate: the most specific match wins. A carrier-specific
    code is a stronger signal than a code that happens to be reused by another carrier.
    """
    code = str(error_code).strip() if error_code else ""
    if code:
        if carrier:
            carrier_reason = _CARRIER_CODE_REASONS.get((carrier, code))
            if carrier_reason is not None:
                return carrier_reason[:255]

        exact_reason = _CODE_REASONS.get(code)
        if exact_reason is not None:
            return exact_reason[:255]

        try:
            numeric_code = int(code)
        except (TypeError, ValueError):
            numeric_code = None
        if numeric_code is not None:
            for start, end, family_reason in _CODE_FAMILY_RANGES:
                if start <= numeric_code <= end:
                    return family_reason[:255]

        # `messages.error_code` is written as `carrier_code or category` (see
        # services/messaging.py::_dispatch_to_carrier), so a row whose provider gave no
        # numeric code carries the CATEGORY in this column. Trying it as a category here
        # is what makes reason_for_message work on those rows at all.
        code_as_category = _CATEGORY_REASONS.get(code)
        if code_as_category is not None:
            return code_as_category[:255]

    if category:
        category_reason = _CATEGORY_REASONS.get(category)
        if category_reason is not None:
            return category_reason[:255]

    return GENERIC_REASON


def reason_for_message(message) -> str | None:
    """Derive the public sentence from a Message row, or None if it is not failing."""
    if getattr(message, "status", None) not in _FAILURE_STATUSES:
        return None

    return public_reason(
        carrier=getattr(message, "carrier", None),
        error_code=getattr(message, "error_code", None),
        detail=getattr(message, "error_detail", None),
    )


#: Codes that mean "this recipient could not take the picture", as opposed to "this
#: message could not be delivered at all". Deliberately NARROW: falling back to a plain
#: text with a link is the right answer only when the ATTACHMENT was the problem. A
#: landline (4720/4721) cannot take the text either, so it is not on this list.
MMS_UNSUPPORTED_CODES: frozenset[str] = frozenset({"4740", "4741"})


def is_mms_problem(error_code: str | None, detail: str | None = None) -> bool:
    """Was this rejection about the ATTACHMENT rather than the message?

    The code table is the reliable signal; the detail string is a secondary one, matched
    only on the whole word "mms" so a phrase like "commscore" cannot trigger a fallback.
    """
    code = str(error_code).strip() if error_code else ""
    if code in MMS_UNSUPPORTED_CODES:
        return True
    words = (detail or "").lower().replace("-", " ").replace("_", " ").split()
    return "mms" in words
