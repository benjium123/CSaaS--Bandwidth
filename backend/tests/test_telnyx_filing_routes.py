"""Filing a brand, campaign or toll-free verification to Telnyx is BILLABLE and
non-refundable.

These tests never touch a carrier or the network: they cover the request models, the
declared OpenAPI paths, and that the billable routes are operator-gated. The toll-free
(TFV) body carries the carrier's documented camelCase field names straight through to the
payload builder, so the model is asserted on structurally rather than via any live call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes import registration
from app.api.routes.registration import (
    FileBrandTelnyxIn,
    FileCampaignTelnyxIn,
    FileTfvTelnyxIn,
    TfvOut,
)
from app.auth.deps import require_platform_operator
from app.main import create_app

FILE = (
    "/api/v1/registration/brands/{brand_id}/file-telnyx",
    "/api/v1/registration/campaigns/{campaign_id}/file-telnyx",
    "/api/v1/registration/tollfree/{tfv_id}/file-telnyx",
)
LOCAL_SUBMIT = (
    "/api/v1/registration/brands/{brand_id}/submit",
    "/api/v1/registration/campaigns/{campaign_id}/submit",
    "/api/v1/registration/tollfree/{tfv_id}/submit",
)
FILE_MODELS = (FileBrandTelnyxIn, FileCampaignTelnyxIn, FileTfvTelnyxIn)

#: A complete, valid NON-sole-proprietor Telnyx toll-free body. Every required carrier
#: field is present under its documented camelCase name; ``confirm_non_refundable`` is
#: intentionally omitted so the shared confirmation tests below control it.
TFV_FIELDS = {
    "sole_proprietor": False,
    "businessName": "Sabine Property Group LLC",
    "corporateWebsite": "https://sabineproperty.example",
    "businessAddr1": "100 Main Street",
    "businessCity": "Austin",
    "businessState": "TX",
    "businessZip": "78701",
    "businessContactFirstName": "Dana",
    "businessContactLastName": "Reyes",
    "businessContactEmail": "dana@sabineproperty.example",
    "businessContactPhone": "+15125550123",
    "useCaseSummary": "Appointment notices to opted-in residents.",
    "productionMessageContent": "Sabine: your appointment is tomorrow. Reply STOP to opt out.",
    "optInWorkflow": "Residents opt in via a checkbox on the tenant portal.",
    "additionalInformation": "No additional information.",
    "useCase": "Appointments",
    "messageVolume": "10,000",
    "optInWorkflowImageURLs": ["https://sabineproperty.example/optin.png"],
    "businessRegistrationNumber": "87-1234567",
    "businessRegistrationType": "EIN",
    "businessRegistrationCountry": "US",
}

# Every field EXCEPT confirm_non_refundable, so the negative cases can only fail on it.
OTHER_FIELDS = {
    FileBrandTelnyxIn: {
        "company_name": "Sabine Property Group LLC",
        "first_name": "Dana",
        "last_name": "Reyes",
        "brand_relationship": "DIRECT",
    },
    FileCampaignTelnyxIn: {
        "assertions": {"autoRenewal": True, "termsAndConditions": True},
    },
    FileTfvTelnyxIn: dict(TFV_FIELDS),
}


def _body(model, **overrides):
    return {**OTHER_FIELDS[model], **overrides}


@pytest.mark.parametrize("model", FILE_MODELS)
def test_confirm_non_refundable_is_required(model):
    with pytest.raises(ValidationError):
        model(**_body(model))


@pytest.mark.parametrize("model", FILE_MODELS)
def test_confirm_non_refundable_false_is_rejected(model):
    with pytest.raises(ValidationError):
        model(**_body(model, confirm_non_refundable=False))


@pytest.mark.parametrize("model", FILE_MODELS)
def test_confirm_non_refundable_true_is_accepted(model):
    body = model(**_body(model, confirm_non_refundable=True))
    assert body.confirm_non_refundable is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"useCase": "NOT_A_REAL_USE_CASE"},
        {"messageVolume": "not-a-volume"},
        {"optInWorkflowImageURLs": []},
    ],
)
def test_tfv_rejects_invalid_carrier_fields(overrides):
    with pytest.raises(ValidationError):
        FileTfvTelnyxIn(**_body(FileTfvTelnyxIn, confirm_non_refundable=True, **overrides))


def test_tfv_model_keeps_camelcase_fields_and_rejects_generic_bypass():
    dumped = FileTfvTelnyxIn(
        **_body(FileTfvTelnyxIn, confirm_non_refundable=True)
    ).model_dump()
    for name in ("useCase", "messageVolume", "optInWorkflowImageURLs"):
        assert dumped[name] == TFV_FIELDS[name]
    with pytest.raises(ValidationError):
        FileTfvTelnyxIn(
            **_body(
                FileTfvTelnyxIn,
                confirm_non_refundable=True,
                fields={"useCase": "Appointments"},
            )
        )


def test_tfv_out_exposes_carrier_refs():
    assert "carrier_refs" in TfvOut.model_fields


def _dependant_calls(dependant):
    calls, stack = [], [dependant]
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.append(dep.call)
        stack.extend(dep.dependencies)
    return calls


def _find_route(path):
    for route in registration.router.routes:
        if getattr(route, "path", None) == path:
            return route
    return None


def test_billable_filing_routes_are_operator_gated():
    for path in FILE:
        route = _find_route(path)
        assert route is not None, f"no route registered for {path}"
        assert require_platform_operator in _dependant_calls(route.dependant), path


def test_tollfree_file_and_local_submit_are_declared_and_distinct():
    file_route = _find_route(FILE[2])
    submit_route = _find_route(LOCAL_SUBMIT[2])
    assert file_route is not None and submit_route is not None
    assert file_route is not submit_route
    assert require_platform_operator in _dependant_calls(file_route.dependant)
    assert require_platform_operator not in _dependant_calls(submit_route.dependant)


def test_openapi_declares_filing_routes_and_keeps_local_submit(settings):
    paths = create_app(settings).openapi()["paths"]
    for path in FILE + LOCAL_SUBMIT:
        assert path in paths and "post" in paths[path], f"missing POST {path}"
