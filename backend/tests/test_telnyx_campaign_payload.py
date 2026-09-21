"""Tests for the pure Telnyx campaignBuilder payload translation."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.errors import ValidationFailedError
from app.providers.telnyx.campaign_payload import build_campaign_payload

BRAND = "brand-123"
BASE_ASSERTIONS = {"autoRenewal": True, "termsAndConditions": True}


@dataclass
class FakeCampaign:
    use_case: str = "MIXED"
    description: str = "Weekly deals for opted-in shoppers"
    opt_in_process: str = "Text JOIN to 12345 to opt in"
    sample_messages: list[str] = field(default_factory=lambda: ["SAVE10 for 10% off"])
    help_message: str | None = None
    opt_out_message: str | None = None


def _build(campaign=None, *, brand=BRAND, assertions=BASE_ASSERTIONS):
    return build_campaign_payload(
        campaign or FakeCampaign(), telnyx_brand_id=brand, assertions=assertions
    )


def test_full_payload_includes_required_and_optional_fields():
    campaign = FakeCampaign(help_message="Reply HELP", opt_out_message="Reply STOP")
    payload = _build(campaign)
    assert payload["brandId"] == BRAND
    assert payload["usecase"] == "MIXED"
    assert payload["description"] == campaign.description
    assert payload["messageFlow"] == campaign.opt_in_process
    assert payload["sample1"] == "SAVE10 for 10% off"
    assert payload["autoRenewal"] is True
    assert payload["termsAndConditions"] is True
    assert payload["helpMessage"] == "Reply HELP"
    assert payload["optoutMessage"] == "Reply STOP"


def test_auto_renewal_false_is_preserved():
    payload = _build(assertions={"autoRenewal": False, "termsAndConditions": True})
    assert payload["autoRenewal"] is False


def test_missing_consent_rejected():
    with pytest.raises(ValidationFailedError):
        _build(assertions={"autoRenewal": True})


def test_missing_sample_rejected():
    with pytest.raises(ValidationFailedError):
        _build(FakeCampaign(sample_messages=[]))


def test_missing_brand_id_rejected():
    with pytest.raises(ValidationFailedError):
        _build(brand="   ")


def test_false_terms_rejected():
    with pytest.raises(ValidationFailedError):
        _build(assertions={"autoRenewal": True, "termsAndConditions": False})


def test_missing_renewal_rejected():
    with pytest.raises(ValidationFailedError):
        _build(assertions={"termsAndConditions": True})


def test_unknown_assertion_key_rejected_and_not_echoed():
    secret = "notAnAllowedKey"
    with pytest.raises(ValidationFailedError) as excinfo:
        _build(assertions={**BASE_ASSERTIONS, secret: True})
    assert secret not in str(excinfo.value)


def test_lowercase_use_case_rejected():
    with pytest.raises(ValidationFailedError):
        _build(FakeCampaign(use_case="mixed"))


def test_more_than_five_samples_rejected():
    with pytest.raises(ValidationFailedError):
        _build(FakeCampaign(sample_messages=[f"s{i}" for i in range(6)]))
