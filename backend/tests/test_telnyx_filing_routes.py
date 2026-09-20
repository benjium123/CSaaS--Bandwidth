"""Filing a brand or campaign to Telnyx is BILLABLE and non-refundable.

These tests never touch a carrier or the network: they cover the request models, the
declared OpenAPI paths, and that the billable routes are operator-gated.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes import registration
from app.api.routes.registration import FileBrandTelnyxIn, FileCampaignTelnyxIn
from app.auth.deps import require_platform_operator
from app.main import create_app

FILE = (
    "/api/v1/registration/brands/{brand_id}/file-telnyx",
    "/api/v1/registration/campaigns/{campaign_id}/file-telnyx",
)
LOCAL_SUBMIT = (
    "/api/v1/registration/brands/{brand_id}/submit",
    "/api/v1/registration/campaigns/{campaign_id}/submit",
)
FILE_MODELS = (FileBrandTelnyxIn, FileCampaignTelnyxIn)

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


def test_openapi_declares_filing_routes_and_keeps_local_submit(settings):
    paths = create_app(settings).openapi()["paths"]
    for path in FILE + LOCAL_SUBMIT:
        assert path in paths and "post" in paths[path], f"missing POST {path}"
