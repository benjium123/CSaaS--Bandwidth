"""End-to-end API tests for the individual (personal) KYC flow.

These exercise the real HTTP surface: register an individual workspace, fill the
personal business profile, declare a use case, add the owner as a person, run a
Didit identity verification through the real webhook handler, accept the agreement,
submit, and have an admin operator approve it. Negative cases pin the individual-only
rules (one owner, no company fields, no texting).

Fixtures ``kyc_app``, ``kyc_settings`` and ``_make_operator`` are reused from
tests.test_p41_kyc; the Didit provider's ``start`` is monkeypatched on the class so
no network call is made, and the webhook is driven through
``kyc_svc.handle_didit_event`` with a real Didit-shaped payload.
"""

from __future__ import annotations

import uuid

from app.db.base import set_org_context
from app.models import KycPerson
from tests.conftest import auth_headers
from tests.test_p41_kyc import _make_operator, _write_sanctions
from tests.test_p41_kyc import kyc_app as kyc_app
from tests.test_p41_kyc import kyc_settings as kyc_settings

PASSWORD = "correct-horse-battery"


async def _register_individual(client, email: str, full_name: str = "Ada Solo") -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "account_type": "individual",
            "full_name": full_name,
        },
    )
    assert r.status_code == 201, r.text
    from tests.conftest import confirm_registered_email

    await confirm_registered_email(client, email)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _org_id(client, token: str) -> str:
    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    memberships = r.json()["memberships"]
    assert len(memberships) == 1, memberships
    assert memberships[0]["account_type"] == "individual"
    return memberships[0]["org_id"]


async def _fill_personal_profile(client, h: dict) -> None:
    r = await client.put(
        "/api/v1/kyc/profile/business",
        json={
            "country": "US",
            "legal_name": "Ada Solo",
            "business_email": "ada@example.com",
            "business_phone": "+15125550199",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        "/api/v1/kyc/profile/use-case",
        json={
            "description": "Personal calls and reminders for my own contacts.",
            "vertical": "personal",
            "who_you_contact": "Friends and family I already know",
            "list_source": "My own phone contacts",
            "monthly_calls": 20,
            "monthly_texts": 0,
            "destination_countries": ["US"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text


async def _add_owner(client, h: dict) -> str:
    r = await client.post(
        "/api/v1/kyc/persons",
        json={"role": "owner", "full_name": "Ada Solo", "is_me": True},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _verify_owner(
    client, session, settings, monkeypatch, h: dict, org_id: str, person_id: str
) -> None:
    """Drive a Didit verification through the real webhook handler.

    ``DiditIdentityProvider.start`` is monkeypatched on the CLASS because
    ``kyc.start_person_verification`` constructs a fresh instance for individual
    accounts. The webhook is then applied via ``kyc_svc.handle_didit_event`` with a
    real Didit-shaped payload carrying the verified identity.
    """
    from app.services import identity_provider
    from app.services import kyc as kyc_svc

    session_id = f"didit_{uuid.uuid4().hex[:12]}"

    async def fake_start(self, settings, *, org_id, person_id, email, return_url):
        return identity_provider.StartedVerification(
            "didit", session_id, f"https://verify.didit.me/{session_id}"
        )

    monkeypatch.setattr(identity_provider.DiditIdentityProvider, "start", fake_start)

    r = await client.post(f"/api/v1/kyc/persons/{person_id}/verify", json={}, headers=h)
    assert r.status_code == 200, r.text

    payload = {
        "webhook_type": "session.completed",
        "status": "Approved",
        "session_id": session_id,
        "vendor_data": person_id,
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "metadata": {
            "purpose": "kyc_person",
            "org_id": org_id,
            "person_id": person_id,
        },
        "decision": {
            "status": "Approved",
            "id_verifications": [
                {
                    "first_name": "Ada",
                    "last_name": "Solo",
                    "date_of_birth": "1980-04-02",
                    "document_type": "Passport",
                    "issuing_state": "US",
                }
            ],
        },
    }
    await kyc_svc.handle_didit_event(session, settings, payload)
    await session.commit()


async def _accept_agreement(client, h: dict) -> None:
    profile = (await client.get("/api/v1/kyc/profile", headers=h)).json()
    r = await client.post(
        "/api/v1/kyc/agreement",
        json={"version": profile["agreement"]["current_version"], "accept": True},
        headers=h,
    )
    assert r.status_code == 200, r.text


async def test_individual_kyc_end_to_end(
    kyc_app,
    session,
    kyc_settings,
    monkeypatch,  # noqa: F811 - pytest fixture injection
):
    from app.services import telephony_access

    client, app, *_ = kyc_app
    _write_sanctions(kyc_settings, ["OTHER PERSON"])
    token = await _register_individual(client, "ada@example.com")
    org_id = await _org_id(client, token)
    h = auth_headers(token, org_id)

    # Submit before verification fails.
    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "kyc_incomplete"

    await _fill_personal_profile(client, h)
    person_id = await _add_owner(client, h)
    await _verify_owner(client, session, kyc_settings, monkeypatch, h, org_id, person_id)
    await _accept_agreement(client, h)

    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted"
    assert body["account_type"] == "individual"

    # After submit but before admin approval, the real telephony gate refuses calling
    # (no operator decision recorded yet) and always refuses texting for individuals.
    assert (
        await telephony_access.refusal(session, kyc_settings, uuid.UUID(org_id), "call")
        == "account_not_verified"
    )
    assert (
        await telephony_access.refusal(session, kyc_settings, uuid.UUID(org_id), "sms")
        == "individual_messaging_disabled"
    )

    # Ops queue/detail show the individual account type.
    ops = await _make_operator(client, session, "ops@platform.example", role="reviewer")
    oh = auth_headers(ops)
    queue = (await client.get("/api/v1/ops/queue", headers=oh)).json()
    assert any(
        a["org_id"] == org_id and a["account_type"] == "individual" for a in queue["applications"]
    )
    detail = (await client.get(f"/api/v1/ops/applications/{org_id}", headers=oh)).json()
    assert detail["account_type"] == "individual"

    # Reviewer cannot approve an individual account.
    r = await client.post(
        f"/api/v1/ops/applications/{org_id}/approve",
        json={"note": "ok"},
        headers=oh,
    )
    assert r.status_code == 403, r.text

    # Admin can.
    admin = await _make_operator(client, session, "admin@platform.example", role="admin")
    r = await client.post(
        f"/api/v1/ops/applications/{org_id}/approve",
        json={"note": "ok"},
        headers=auth_headers(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    # Real approval unlocks calling: refresh the cached KycProfile in this session and
    # re-check the gate. Texting stays disabled for individuals.
    set_org_context(session, uuid.UUID(org_id))
    session.expire_all()
    assert await telephony_access.refusal(session, kyc_settings, uuid.UUID(org_id), "call") is None
    assert (
        await telephony_access.refusal(session, kyc_settings, uuid.UUID(org_id), "sms")
        == "individual_messaging_disabled"
    )

    set_org_context(session, uuid.UUID(org_id))
    person = await session.get(KycPerson, uuid.UUID(person_id))
    assert person.status == "verified"
    assert person.identity_hash and len(person.identity_hash) == 64
    assert person.verified_name


async def test_individual_rejects_nonself_and_second_person(
    kyc_app,
    session,  # noqa: F811 - pytest fixture injection
):
    client, *_ = kyc_app
    token = await _register_individual(client, "solo@example.com")
    org_id = await _org_id(client, token)
    h = auth_headers(token, org_id)

    r = await client.post(
        "/api/v1/kyc/persons",
        json={"role": "owner", "full_name": "Someone Else", "is_me": False},
        headers=h,
    )
    assert r.status_code == 422, r.text

    await _add_owner(client, h)
    r = await client.post(
        "/api/v1/kyc/persons",
        json={"role": "owner", "full_name": "Second", "is_me": True},
        headers=h,
    )
    assert r.status_code == 422, r.text


async def test_individual_rejects_company_fields_and_texts(
    kyc_app,
    session,  # noqa: F811 - pytest fixture injection
):
    client, *_ = kyc_app
    token = await _register_individual(client, "fields@example.com")
    org_id = await _org_id(client, token)
    h = auth_headers(token, org_id)

    r = await client.put(
        "/api/v1/kyc/profile/business",
        json={"country": "US", "legal_name": "Ada", "website": "https://x.example"},
        headers=h,
    )
    assert r.status_code == 422, r.text

    r = await client.put(
        "/api/v1/kyc/profile/use-case",
        json={
            "description": "Personal calls and reminders for my own contacts.",
            "vertical": "personal",
            "who_you_contact": "Friends and family I already know",
            "list_source": "My own phone contacts",
            "monthly_calls": 20,
            "monthly_texts": 5,
            "destination_countries": ["US"],
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
