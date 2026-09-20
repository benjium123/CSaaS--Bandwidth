"""Pure mapping of Telnyx brand/campaign GET payloads to a normalized status.

Returns only "submitted", "approved" or "rejected". Malformed or unknown
input falls back to "submitted". Never logs payload contents or PII.
"""

from typing import Any

SUBMITTED = "submitted"
APPROVED = "approved"
REJECTED = "rejected"

_BRAND_APPROVED_IDENTITY = {"VERIFIED", "VETTED_VERIFIED"}
_BRAND_REJECTED_STATUS = {"REGISTRATION_FAILED"}

_CAMPAIGN_APPROVED_STATUS = {"MNO_PROVISIONED"}
_CAMPAIGN_REJECTED_STATUS = {
    "TCR_FAILED",
    "TELNYX_FAILED",
    "MNO_REJECTED",
    "MNO_PROVISIONING_FAILED",
}


def _enum(value: Any) -> str:
    """Normalize a payload enum value; non-strings become empty."""
    return value.strip().upper() if isinstance(value, str) else ""


def map_brand_status(payload: dict) -> str:
    """Map a Telnyx brand GET payload to submitted/approved/rejected."""
    if not isinstance(payload, dict):
        return SUBMITTED

    status = _enum(payload.get("status"))
    identity = _enum(payload.get("identityStatus"))

    if status in _BRAND_REJECTED_STATUS:
        return REJECTED
    if status == "OK" and identity in _BRAND_APPROVED_IDENTITY:
        return APPROVED
    return SUBMITTED


def map_campaign_status(payload: dict) -> str:
    """Map a Telnyx campaign GET payload to submitted/approved/rejected."""
    if not isinstance(payload, dict):
        return SUBMITTED

    campaign = _enum(payload.get("campaignStatus"))
    submission = _enum(payload.get("submissionStatus"))

    if campaign in _CAMPAIGN_REJECTED_STATUS:
        return REJECTED
    if campaign in _CAMPAIGN_APPROVED_STATUS and submission != "FAILED":
        return APPROVED
    return SUBMITTED
