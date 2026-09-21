# ruff: noqa: F811 -- imported pytest fixtures are injected by name
import pytest

from tests.conftest import auth_headers, register_and_login
from tests.test_individual_kyc_api import (
    _accept_agreement,
    _org_id,
    _register_individual,
    _verify_owner,
)
from tests.test_p41_kyc import _make_operator, _signup_org, _write_sanctions
from tests.test_p41_kyc import kyc_app as kyc_app
from tests.test_p41_kyc import kyc_settings as kyc_settings

FORM = {
    "legal_name": "Ada Solo",
    "country": "PK",
    "phone": "+923001234567",
    "industry": "Consulting",
    "purpose": "Calling customers about appointments.",
    "customer_country": "NZ",
}


@pytest.mark.parametrize("manual", [False, True])
async def test_short_application_submits_and_admin_approves(
    kyc_app, session, kyc_settings, monkeypatch, manual
):
    client, *_ = kyc_app
    if not manual:
        _write_sanctions(kyc_settings, ["OTHER PERSON"])
    token = await _register_individual(client, "short@example.com")
    org = await _org_id(client, token)
    h = auth_headers(token, org)
    r = await client.put("/api/v1/kyc/application", json=FORM, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["business"]["country"] == "PK"
    assert len(data["persons"]) == 1
    assert set(data["missing"]) == {"id_verification", "agreement"}
    # Saving again does not duplicate the owner.
    r = await client.put("/api/v1/kyc/application", json=FORM, headers=h)
    assert len(r.json()["persons"]) == 1
    assert (await client.post("/api/v1/kyc/submit", headers=h)).status_code == 422
    await _verify_owner(
        client, session, kyc_settings, monkeypatch, h, org, data["persons"][0]["id"]
    )
    await _accept_agreement(client, h)
    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 200, r.text
    admin = await _make_operator(client, session, "short-admin@example.com")
    ah = auth_headers(admin)
    from unittest.mock import AsyncMock

    from app.services import didit_client

    monkeypatch.setattr(
        didit_client,
        "retrieve_session",
        AsyncMock(
            return_value={
                "session_id": "test",
                "id_verifications": [
                    {
                        "full_name": "Ada Solo",
                        "front_image": "https://service-didit-test.s3.amazonaws.com/front.jpg",
                    }
                ],
            }
        ),
    )
    evidence_path = f"/api/v1/ops/applications/{org}/persons/{data['persons'][0]['id']}/evidence"
    evidence = await client.get(evidence_path, headers=ah)
    assert evidence.status_code == 200, evidence.text
    assert "no-store" in evidence.headers["cache-control"]
    assert evidence.json()["id_verifications"][0]["full_name"] == "Ada Solo"
    assert (await client.get(evidence_path, headers=h)).status_code == 403
    queue = (await client.get("/api/v1/ops/queue", headers=ah)).json()
    assert any(a["org_id"] == org for a in queue["applications"])
    r = await client.post(
        f"/api/v1/ops/applications/{org}/approve",
        json={"note": "Reviewed", "manual_override": manual},
        headers=ah,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


async def test_company_personal_details_preserve_company_requirements(kyc_app):
    client, *_ = kyc_app
    token = await register_and_login(client, "company-short@example.com")
    org = await _signup_org(client, token, "Example Company")
    h = auth_headers(token, org["id"])
    r = await client.put(
        "/api/v1/kyc/profile/business",
        json={"legal_name": "Example Company", "country": "US"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        "/api/v1/kyc/application", json={**FORM, "accept_personal_agreement": True}, headers=h
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["business"]["legal_name"] == "Example Company"
    assert data["business"]["country"] == "US"
    assert data["use_case"]["applicant_details"]["legal_name"] == "Ada Solo"
    assert "documents" in data["missing"]
    assert "registration_number" in data["missing"]
    assert "agreement" in data["missing"]  # Personal acceptance is not company acceptance.
    assert (await client.post("/api/v1/kyc/submit", headers=h)).status_code == 422


@pytest.mark.parametrize(
    "bad", [{"country": "ZZ"}, {"phone": "12345"}, {"customer_country": "ZZ"}, {"legal_name": "  "}]
)
async def test_short_application_validation(kyc_app, bad):
    client, *_ = kyc_app
    token = await _register_individual(client, "invalid-short@example.com")
    org = await _org_id(client, token)
    r = await client.put(
        "/api/v1/kyc/application", json={**FORM, **bad}, headers=auth_headers(token, org)
    )
    assert r.status_code == 422, r.text
