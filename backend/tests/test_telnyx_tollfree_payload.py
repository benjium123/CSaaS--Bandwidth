"""Pure unit tests for the Telnyx toll-free verification payload builder.

``build_tollfree_verification_payload`` is a pure function: no transport, no network,
no database. Every assertion here is on its input/output contract, and each malformed
or missing input must fail closed with ``ValidationFailedError`` before any HTTP call
could be made.
"""

from __future__ import annotations

import pytest

from app.errors import ValidationFailedError
from app.providers.telnyx.tollfree_payload import (
    MESSAGE_VOLUMES,
    USE_CASES,
    build_tollfree_verification_payload,
)

#: Valid NANP toll-free number used as the default in most tests.
TOLLFREE_NUMBER = "+18005551234"

#: Documented required plain-text request keys (mirrors the contract).
REQUIRED_TEXT_FIELDS = (
    "businessName",
    "corporateWebsite",
    "businessAddr1",
    "businessCity",
    "businessState",
    "businessZip",
    "businessContactFirstName",
    "businessContactLastName",
    "businessContactEmail",
    "businessContactPhone",
    "useCaseSummary",
    "productionMessageContent",
    "optInWorkflow",
    "additionalInformation",
)

REGISTRATION_FIELDS = (
    "businessRegistrationNumber",
    "businessRegistrationType",
    "businessRegistrationCountry",
)


def base_fields(**overrides):
    """A fully-populated, valid set of operator-supplied fields."""
    fields = {
        "businessName": "Acme Widgets LLC",
        "corporateWebsite": "https://acme.example.com",
        "businessAddr1": "1 Market St",
        "businessCity": "San Francisco",
        "businessState": "CA",
        "businessZip": "94105",
        "businessContactFirstName": "Ada",
        "businessContactLastName": "Lovelace",
        "businessContactEmail": "ada@acme.example.com",
        "businessContactPhone": "+14155550123",
        "useCaseSummary": "Order and delivery updates for opted-in customers.",
        "productionMessageContent": "Your order has shipped.",
        "optInWorkflow": "Customers opt in at checkout.",
        "additionalInformation": "No additional information.",
        "useCase": "Mixed",
        "messageVolume": "10,000",
        "optInWorkflowImageURLs": ["https://acme.example.com/optin.png"],
    }
    fields.update(overrides)
    return fields


def business_fields(**overrides):
    """Valid fields plus the three business-registration values (non-sole-prop)."""
    fields = base_fields(**overrides)
    for key, value in (
        ("businessRegistrationNumber", "C1234567"),
        ("businessRegistrationType", "LLC"),
        ("businessRegistrationCountry", "US"),
    ):
        fields.setdefault(key, value)
    return fields


# ----------------------------------------------------------------------------------
# Happy paths
# ----------------------------------------------------------------------------------
def test_builds_complete_business_payload():
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=business_fields(),
        sole_proprietor=False,
    )

    for key in REQUIRED_TEXT_FIELDS:
        assert payload[key], key
    assert payload["businessName"] == "Acme Widgets LLC"
    assert payload["useCase"] == "Mixed"
    assert payload["messageVolume"] == "10,000"
    assert payload["phoneNumbers"] == [{"phoneNumber": TOLLFREE_NUMBER}]
    assert payload["optInWorkflowImageURLs"] == [
        {"url": "https://acme.example.com/optin.png"}
    ]
    assert payload["businessRegistrationNumber"] == "C1234567"
    assert payload["businessRegistrationType"] == "LLC"
    assert payload["businessRegistrationCountry"] == "US"


def test_builds_complete_sole_proprietor_payload():
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(),
        sole_proprietor=True,
    )

    assert payload["phoneNumbers"] == [{"phoneNumber": TOLLFREE_NUMBER}]
    assert payload["useCase"] == "Mixed"
    assert payload["messageVolume"] == "10,000"
    for key in REGISTRATION_FIELDS:
        assert key not in payload


def test_surrounding_whitespace_is_trimmed():
    payload = build_tollfree_verification_payload(
        phone_number="  +18005551234  ",
        fields=base_fields(businessName="  Acme Widgets LLC  "),
        sole_proprietor=True,
    )

    assert payload["businessName"] == "Acme Widgets LLC"
    assert payload["phoneNumbers"] == [{"phoneNumber": TOLLFREE_NUMBER}]


# ----------------------------------------------------------------------------------
# messageVolume
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("volume", MESSAGE_VOLUMES)
def test_exact_message_volume_strings(volume):
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(messageVolume=volume),
        sole_proprietor=True,
    )
    assert payload["messageVolume"] == volume


def test_message_volume_comma_is_preserved_exactly():
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(messageVolume="1,000"),
        sole_proprietor=True,
    )
    assert payload["messageVolume"] == "1,000"
    assert payload["messageVolume"] != "1000"


@pytest.mark.parametrize(
    "volume",
    [
        1000,
        10_000,
        250_000,
        None,
        "",
        "1000",
        "1,000,000,000",
        "1 000",
        "100,000.0",
    ],
)
def test_invalid_or_unlisted_message_volume_rejected(volume):
    with pytest.raises(ValidationFailedError, match="messageVolume"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(messageVolume=volume),
            sole_proprietor=True,
        )


# ----------------------------------------------------------------------------------
# phone number
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "number",
    [
        "8005551234",
        "+1 8005551234",
        "+1800555123a",
        "+08005551234",
        "+1800-555-1234",
        "not-a-number",
        "",
        "   ",
    ],
)
def test_non_e164_number_rejected(number):
    with pytest.raises(ValidationFailedError, match=r"E\.164"):
        build_tollfree_verification_payload(
            phone_number=number,
            fields=base_fields(),
            sole_proprietor=True,
        )


@pytest.mark.parametrize(
    "number",
    [
        "+14155551234",  # valid E.164, ordinary NANP area code
        "+442079460958",  # valid E.164, UK
        "+1800555123",  # +1 toll-free area code but one digit short
        "+180055512345678",  # +1 800 but too long
    ],
)
def test_non_tollfree_number_rejected(number):
    with pytest.raises(ValidationFailedError, match="toll-free"):
        build_tollfree_verification_payload(
            phone_number=number,
            fields=base_fields(),
            sole_proprietor=True,
        )


def test_all_tollfree_area_codes_accepted():
    for area in ("800", "833", "844", "855", "866", "877", "888"):
        payload = build_tollfree_verification_payload(
            phone_number=f"+1{area}5551234",
            fields=base_fields(),
            sole_proprietor=True,
        )
        assert payload["phoneNumbers"] == [{"phoneNumber": f"+1{area}5551234"}]


# ----------------------------------------------------------------------------------
# required text fields
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("missing", REQUIRED_TEXT_FIELDS)
def test_missing_required_field_rejected(missing):
    fields = base_fields()
    del fields[missing]
    with pytest.raises(ValidationFailedError, match=missing):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=fields,
            sole_proprietor=True,
        )


@pytest.mark.parametrize("value", ["", "   ", None, 123, []])
def test_blank_or_non_string_required_field_rejected(value):
    with pytest.raises(ValidationFailedError, match="businessName"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(businessName=value),
            sole_proprietor=True,
        )


# ----------------------------------------------------------------------------------
# useCase
# ----------------------------------------------------------------------------------
def test_missing_use_case_rejected():
    with pytest.raises(ValidationFailedError, match="useCase"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(useCase=""),
            sole_proprietor=True,
        )


@pytest.mark.parametrize(
    "use_case",
    ["Marketing", "2fa", "Conversational", "Mixed!", "general marketing"],
)
def test_unlisted_use_case_rejected(use_case):
    with pytest.raises(ValidationFailedError, match="useCase"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(useCase=use_case),
            sole_proprietor=True,
        )


@pytest.mark.parametrize("use_case", USE_CASES)
def test_documented_use_cases_passed_through(use_case):
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(useCase=use_case),
        sole_proprietor=True,
    )
    assert payload["useCase"] == use_case


@pytest.mark.parametrize("raw", ["MIXED", "mixed", "Mixed", "  MIXED  "])
def test_mixed_alias_maps_to_enum(raw):
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(useCase=raw),
        sole_proprietor=True,
    )
    assert payload["useCase"] == "Mixed"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2FA ", "2FA"),
        ("  General Marketing  ", "General Marketing"),
        ("Conversational / Alerts\t", "Conversational / Alerts"),
        ("Appointments ", "Appointments"),
    ],
)
def test_use_case_whitespace_is_stripped(raw, expected):
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(useCase=raw),
        sole_proprietor=True,
    )
    assert payload["useCase"] == expected


# ----------------------------------------------------------------------------------
# optInWorkflowImageURLs
# ----------------------------------------------------------------------------------
def test_single_string_image_url_accepted():
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(optInWorkflowImageURLs="https://acme.example.com/optin.png"),
        sole_proprietor=True,
    )
    assert payload["optInWorkflowImageURLs"] == [
        {"url": "https://acme.example.com/optin.png"}
    ]


def test_mixed_sequence_of_urls_and_mappings_accepted():
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(
            optInWorkflowImageURLs=[
                {"url": "https://acme.example.com/a.png"},
                "http://acme.example.com/b.png",
            ]
        ),
        sole_proprietor=True,
    )
    assert payload["optInWorkflowImageURLs"] == [
        {"url": "https://acme.example.com/a.png"},
        {"url": "http://acme.example.com/b.png"},
    ]


@pytest.mark.parametrize(
    "urls",
    [
        [],
        "",
        [""],
        ["   "],
        ["not-a-url"],
        ["/relative/path.png"],
        ["ftp://acme.example.com/optin.png"],
        ["https:///missing-host.png"],
        [{"url": ""}],
        [{}],
        [123],
        None,
    ],
)
def test_invalid_image_urls_rejected(urls):
    with pytest.raises(ValidationFailedError, match="optInWorkflowImageURLs"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(optInWorkflowImageURLs=urls),
            sole_proprietor=True,
        )


# ----------------------------------------------------------------------------------
# business registration fields
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("missing", REGISTRATION_FIELDS)
def test_registration_fields_required_for_business(missing):
    fields = business_fields()
    del fields[missing]
    with pytest.raises(ValidationFailedError, match=missing):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=fields,
            sole_proprietor=False,
        )


@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_blank_registration_field_rejected(value):
    with pytest.raises(ValidationFailedError, match="businessRegistrationNumber"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=business_fields(businessRegistrationNumber=value),
            sole_proprietor=False,
        )


def test_registration_fields_omitted_for_sole_proprietor():
    payload = build_tollfree_verification_payload(
        phone_number=TOLLFREE_NUMBER,
        fields=base_fields(),
        sole_proprietor=True,
    )
    for key in REGISTRATION_FIELDS:
        assert key not in payload


# ----------------------------------------------------------------------------------
# response-only verificationStatus
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["approved", "pending", "rejected", ""])
def test_verification_status_rejected(status):
    with pytest.raises(ValidationFailedError, match="response-only"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(verificationStatus=status),
            sole_proprietor=True,
        )


def test_verification_status_rejected_for_sole_proprietor_too():
    with pytest.raises(ValidationFailedError, match="response-only"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=base_fields(verificationStatus="approved"),
            sole_proprietor=True,
        )


# ----------------------------------------------------------------------------------
# fail-closed on malformed input
# ----------------------------------------------------------------------------------
def test_non_mapping_fields_rejected():
    with pytest.raises(ValidationFailedError, match="supplied fields"):
        build_tollfree_verification_payload(
            phone_number=TOLLFREE_NUMBER,
            fields=["businessName", "Acme"],  # type: ignore[arg-type]
            sole_proprietor=True,
        )
