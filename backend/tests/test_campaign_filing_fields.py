"""10DLC phase 5: the carrier-vetted campaign fields travel through payload, API and PATCH."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.db.session import get_sessionmaker
from app.errors import ValidationFailedError
from app.models import AuditLogEntry
from app.models.numbers import Campaign
from app.providers.telnyx.campaign_payload import build_campaign_payload
from tests.test_numbers_registration import _approved_campaign, _org

ASSERTIONS = {"autoRenewal": False, "termsAndConditions": True}
URLS = {
    "privacy_policy_url": "https://acme.example/privacy",
    "terms_url": "https://acme.example/terms",
}


def _campaign(**overrides) -> Campaign:
    base = {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "brand_id": uuid.uuid4(),
        "name": "c",
        "use_case": "MIXED",
        "description": "We text owners about land.",
        "opt_in_process": "Web form with a checkbox",
        "sample_messages": ["Hi, Acme here. Reply STOP to opt out."],
    }
    base.update(overrides)
    return Campaign(**base)


def _build(campaign: Campaign) -> dict:
    return build_campaign_payload(
        campaign,
        telnyx_brand_id="B1",
        assertions=ASSERTIONS,
        sub_usecases=["MARKETING", "CUSTOMER_CARE"],
    )


def test_payload_unchanged_when_no_new_fields():
    payload = _build(_campaign())
    for key in (
        "privacyPolicyLink", "termsAndConditionsLink", "optinKeywords", "optoutKeywords",
        "helpKeywords", "optinMessage",
    ):
        assert key not in payload
    assert payload["description"] == "We text owners about land."
    assert payload["messageFlow"] == "Web form with a checkbox"


def test_payload_sends_the_six_fields_and_the_two_lines():
    payload = _build(
        _campaign(
            **URLS,
            opt_in_keywords="start",
            opt_out_keywords=" stop , Unsubscribe,, ",
            help_keywords="help",
            opt_in_message="Welcome to Acme.",
            opt_in_proof_url="https://acme.example/signup",
            expected_monthly_messages=1500,
        )
    )
    assert payload["privacyPolicyLink"] == URLS["privacy_policy_url"]
    assert payload["termsAndConditionsLink"] == URLS["terms_url"]
    assert payload["optinKeywords"] == "START"
    assert payload["optoutKeywords"] == "STOP,UNSUBSCRIBE"
    assert payload["helpKeywords"] == "HELP"
    assert payload["optinMessage"] == "Welcome to Acme."
    assert payload["description"].endswith(
        "\n\nExpected volume: about 1500 messages a month."
    )
    assert payload["messageFlow"].endswith("\n\nOpt-in proof: https://acme.example/signup")


def test_blank_keywords_are_absent():
    assert "optinKeywords" not in _build(_campaign(opt_in_keywords=" , ,"))


def test_appended_line_survives_truncation():
    payload = _build(
        _campaign(
            description="x" * 4000,
            opt_in_process="y" * 4000,
            opt_in_proof_url="https://acme.example/p",
            expected_monthly_messages=10,
        )
    )
    assert len(payload["description"]) <= 4000
    assert payload["description"].endswith("Expected volume: about 10 messages a month.")
    assert len(payload["messageFlow"]) <= 4000
    assert payload["messageFlow"].endswith("Opt-in proof: https://acme.example/p")


def test_required_fields_still_enforced():
    with pytest.raises(ValidationFailedError):
        _build(_campaign(description=None, expected_monthly_messages=5))


FULL = {
    **URLS,
    "opt_in_keywords": "START",
    "opt_out_keywords": "STOP",
    "help_keywords": "HELP",
    "opt_in_message": "Welcome",
    "opt_in_proof_url": "https://acme.example/signup",
    "expected_monthly_messages": 900,
}


async def _brand_id(client, h) -> str:
    camp = await _approved_campaign(client, h, name="seed")
    return camp["brand_id"]


async def _set_status(h, campaign_id: str, status: str) -> None:
    async with get_sessionmaker()() as session:
        session.info["org_id"] = uuid.UUID(h["X-Org-Id"])
        row = await session.get(Campaign, uuid.UUID(campaign_id))
        row.status = status
        await session.commit()


async def _create(client, h, brand_id, name="Draft A", **extra):
    return await client.post(
        "/api/v1/registration/campaigns",
        json={"brand_id": brand_id, "name": name, **extra},
        headers=h,
    )


async def test_create_and_patch_round_trip(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _org_row, h, _n = await _org(client, "f1@example.com")
    brand_id = await _brand_id(client, h)

    created = await _create(client, h, brand_id, **FULL)
    assert created.status_code == 201, created.text
    body = created.json()
    for key, value in FULL.items():
        assert body[key] == value

    patched = await client.patch(
        f"/api/v1/registration/campaigns/{body['id']}",
        json={"terms_url": "https://acme.example/new-terms", "expected_monthly_messages": 50},
        headers=h,
    )
    assert patched.status_code == 200, patched.text
    out = patched.json()
    assert out["terms_url"] == "https://acme.example/new-terms"
    assert out["expected_monthly_messages"] == 50
    assert out["privacy_policy_url"] == FULL["privacy_policy_url"]  # untouched
    assert out["name"] == "Draft A"

    async with get_sessionmaker()() as session:
        session.info[ALLOW_UNSCOPED_KEY] = True
        rows = (
            await session.execute(
                sa.select(AuditLogEntry).where(AuditLogEntry.action == "tendlc.campaign_edited")
            )
        ).scalars().all()
    assert len(rows) == 1 and rows[0].target_id == body["id"]


async def test_url_and_range_validation(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "f2@example.com")
    brand_id = await _brand_id(client, h)
    bad_url = await _create(client, h, brand_id, name="X1", privacy_policy_url="ftp://x")
    assert bad_url.status_code == 422
    zero = await _create(client, h, brand_id, name="X2", expected_monthly_messages=0)
    assert zero.status_code == 422


async def test_patch_refused_unless_draft_or_rejected(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "f3@example.com")
    brand_id = await _brand_id(client, h)
    cid = (await _create(client, h, brand_id)).json()["id"]
    url = f"/api/v1/registration/campaigns/{cid}"

    for status in ("active", "pending", "submitted", "approved"):
        await _set_status(h, cid, status)
        r = await client.patch(url, json={"description": "new"}, headers=h)
        assert r.status_code == 409, (status, r.text)

    await _set_status(h, cid, "rejected")
    r = await client.patch(url, json={"description": "new"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "new"


async def test_patch_cannot_change_brand_status_or_refs(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "f4@example.com")
    brand_id = await _brand_id(client, h)
    created = (await _create(client, h, brand_id)).json()
    r = await client.patch(
        f"/api/v1/registration/campaigns/{created['id']}",
        json={
            "brand_id": str(uuid.uuid4()),
            "status": "active",
            "carrier_refs": {"telnyx": "evil"},
            "description": "ok",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["brand_id"] == brand_id
    assert out["status"] == "draft"
    assert out["carrier_refs"] == {}


async def test_patch_duplicate_name_is_409_and_unknown_is_404(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "f5@example.com")
    brand_id = await _brand_id(client, h)
    await _create(client, h, brand_id, name="One")
    two = (await _create(client, h, brand_id, name="Two")).json()
    dup = await client.patch(
        f"/api/v1/registration/campaigns/{two['id']}", json={"name": "One"}, headers=h
    )
    assert dup.status_code == 409, dup.text
    missing = await client.patch(
        f"/api/v1/registration/campaigns/{uuid.uuid4()}", json={"name": "Z"}, headers=h
    )
    assert missing.status_code == 404
