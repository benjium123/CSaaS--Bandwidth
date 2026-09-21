"""Tests for app.providers.telnyx.registration_status mapping helpers.

Pure mapping only: no network, no DB, no payload logging.
"""

import pytest

from app.providers.telnyx.registration_status import (
    map_brand_status,
    map_campaign_status,
)

BRAND_CASES = [
    ({"status": "OK", "identityStatus": "VERIFIED"}, "approved"),
    ({"status": "OK", "identityStatus": "VETTED_VERIFIED"}, "approved"),
    ({"status": "ok", "identityStatus": " vetted_verified "}, "approved"),
    ({"status": "OK", "identityStatus": "UNVERIFIED"}, "submitted"),
    ({"status": "OK", "identityStatus": "UNVETTED"}, "submitted"),
    ({"status": "OK"}, "submitted"),
    ({"status": "OK", "identityStatus": None}, "submitted"),
    ({"status": "PENDING", "identityStatus": "VERIFIED"}, "submitted"),
    ({"status": "REGISTRATION_FAILED"}, "rejected"),
    ({"status": "REGISTRATION_FAILED", "identityStatus": "VERIFIED"}, "rejected"),
    ({"status": "registration_failed"}, "rejected"),
    ({}, "submitted"),
]

CAMPAIGN_CASES = [
    ({"campaignStatus": "MNO_PROVISIONED"}, "approved"),
    ({"campaignStatus": "MNO_PROVISIONED", "submissionStatus": "COMPLETE"}, "approved"),
    ({"campaignStatus": "mno_provisioned", "submissionStatus": " complete "}, "approved"),
    ({"campaignStatus": "MNO_PROVISIONED", "submissionStatus": "FAILED"}, "submitted"),
    ({"campaignStatus": "TCR_ACCEPTED"}, "submitted"),
    ({"campaignStatus": "MNO_PENDING"}, "submitted"),
    ({"campaignStatus": "TELNYX_ACCEPTED"}, "submitted"),
    ({"campaignStatus": "TCR_FAILED"}, "rejected"),
    ({"campaignStatus": "TELNYX_FAILED"}, "rejected"),
    ({"campaignStatus": "MNO_REJECTED"}, "rejected"),
    ({"campaignStatus": "MNO_PROVISIONING_FAILED"}, "rejected"),
    ({"campaignStatus": "MNO_PROVISIONING_FAILED", "submissionStatus": "FAILED"}, "rejected"),
    ({"campaignStatus": "UNKNOWN"}, "submitted"),
    ({}, "submitted"),
]

MALFORMED = [None, [], "OK", 42, {"status": 1}, {"campaignStatus": ["MNO_PROVISIONED"]}]

VALID_STATUSES = {"submitted", "approved", "rejected"}


@pytest.mark.parametrize("payload,expected", BRAND_CASES, ids=str)
def test_map_brand_status(payload, expected):
    assert map_brand_status(payload) == expected


@pytest.mark.parametrize("payload,expected", CAMPAIGN_CASES, ids=str)
def test_map_campaign_status(payload, expected):
    assert map_campaign_status(payload) == expected


@pytest.mark.parametrize("payload", MALFORMED, ids=str)
def test_malformed_payloads_map_to_submitted(payload):
    assert map_brand_status(payload) == "submitted"
    assert map_campaign_status(payload) == "submitted"


@pytest.mark.parametrize("mapper", [map_brand_status, map_campaign_status])
@pytest.mark.parametrize(
    "payload",
    [case[0] for case in BRAND_CASES + CAMPAIGN_CASES] + MALFORMED,
    ids=str,
)
def test_only_known_statuses_returned(mapper, payload):
    assert mapper(payload) in VALID_STATUSES
