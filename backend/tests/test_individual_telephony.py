"""P41 individual-workspace telephony gate: ``Org.account_type == "individual"``.

Drives the REAL gate (``services/telephony_access``), the REAL dispatch seam
(``services/messaging._dispatch_to_carrier``) and the REAL registration routes against the
conftest database fixtures. Nothing under test is stubbed; the carrier is the suite's
FakeCarrier, so no external carrier is ever called.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.base import set_org_context
from app.errors import AccountNotVerifiedError
from app.models import KycProfile, Message, MessageThread, Org
from app.services import messaging, telephony_access
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, confirm_registered_email

PASSWORD = "correct-horse-battery"
INDIVIDUAL_SMS_CODE = "account_not_verified"


def _flagged(base: Settings, **overrides: object) -> Settings:
    return Settings.model_copy(base, update=overrides)


async def _new_org(
    session,  # noqa: ANN001 - conftest async session fixture
    client,  # noqa: ANN001 - conftest httpx client fixture
    email: str,
    *,
    account_type: str | None = None,
    kyc_required: bool | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Register + create an org through the real API; returns (org_id, user_id, token).

    Newly created orgs default to ``kyc_required=True``; pass ``kyc_required=False``
    to model a grandfathered workspace that predates the KYC opt-in.
    """
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Tester"},
    )
    assert r.status_code == 201, r.text
    await confirm_registered_email(client, email.lower())
    user_id = uuid.UUID(r.json()["id"])
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    r = await client.post(
        "/api/v1/orgs", json={"name": email}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 201, r.text
    org_id = uuid.UUID(r.json()["id"])
    if account_type is not None or kyc_required is not None:
        org = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalar_one()
        if account_type is not None:
            org.account_type = account_type
        if kyc_required is not None:
            org.kyc_required = kyc_required
        org.number_subscription_required = False
        await session.commit()
    return org_id, user_id, token


async def _set_profile(
    session,  # noqa: ANN001
    org_id: uuid.UUID,
    *,
    status: str,
    decided_by: uuid.UUID | None = None,
    decided_at: datetime | None = None,
) -> None:
    """Update the KycProfile row POST /orgs already created (never insert a second one)."""
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one()
    profile.status = status
    profile.decided_by = decided_by
    profile.decided_at = decided_at
    await session.commit()


# --------------------------------------------------------------------------------------
# Texting: individuals can never text, whatever the flags or the KYC status say.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("kyc_enforced", [False, True])
@pytest.mark.parametrize("profile_status", ["draft", "approved"])
async def test_individual_texting_refused_whatever_the_flags(
    client, session, settings, kyc_enforced, profile_status
):
    org_id, user_id, _ = await _new_org(
        session,
        client,
        f"ind-sms-{kyc_enforced}-{profile_status}@example.com",
        account_type="individual",
    )
    await _set_profile(
        session,
        org_id,
        status=profile_status,
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    enforced = _flagged(settings, kyc_enforced=kyc_enforced)
    for kind in ("sms", "sms_dispatch"):
        assert await telephony_access.refusal(session, enforced, org_id, kind) == (
            None if profile_status == "approved" else "account_not_verified"
        )


async def test_individual_texting_gate_raises_its_own_permission_denied_code(
    client, session, settings
):
    org_id, _, _ = await _new_org(
        session, client, "ind-sms-raise@example.com", account_type="individual"
    )
    with pytest.raises(AccountNotVerifiedError) as caught:
        await telephony_access.require_telephony_allowed(session, org_id, "sms", settings=settings)
    assert caught.value.code == INDIVIDUAL_SMS_CODE
    assert caught.value.http_status == 403


async def test_business_texting_unchanged_when_kyc_is_not_enforced(client, session, settings):
    # Grandfathered workspace: kyc_required=False predates the KYC opt-in default.
    org_id, _, _ = await _new_org(
        session,
        client,
        "biz-sms@example.com",
        account_type="business",
        kyc_required=False,
    )
    assert await telephony_access.refusal(session, settings, org_id, "sms") is None
    await telephony_access.require_telephony_allowed(session, org_id, "sms", settings=settings)
    assert (
        await telephony_access.refusal(
            session, _flagged(settings, kyc_enforced=True), org_id, "sms"
        )
        == "account_not_verified"
    )


async def test_business_with_kyc_required_blocked_until_approved(client, session, settings):
    """New workspaces opt in via ``kyc_required=True``: pending KYC blocks telephony."""
    org_id, user_id, _ = await _new_org(
        session, client, "biz-kyc-required@example.com", account_type="business"
    )
    org = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalar_one()
    assert org.kyc_required is True  # creation default, even with kyc_enforced off

    await _set_profile(
        session,
        org_id,
        status="submitted",
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    assert await telephony_access.refusal(session, settings, org_id, "sms") == (
        "account_not_verified"
    )
    with pytest.raises(AccountNotVerifiedError):
        await telephony_access.require_telephony_allowed(session, org_id, "sms", settings=settings)

    await _set_profile(
        session,
        org_id,
        status="approved",
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    assert await telephony_access.refusal(session, settings, org_id, "sms") is None
    await telephony_access.require_telephony_allowed(session, org_id, "sms", settings=settings)


# --------------------------------------------------------------------------------------
# Calling and numbers: individuals always need an approved profile WITH a decision.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["call", "number"])
async def test_individual_calling_refused_while_kyc_pending(client, session, settings, kind):
    org_id, user_id, _ = await _new_org(
        session, client, f"ind-pending-{kind}@example.com", account_type="individual"
    )
    await _set_profile(
        session,
        org_id,
        status="submitted",
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    assert await telephony_access.refusal(session, settings, org_id, kind) == (
        "account_not_verified"
    )


@pytest.mark.parametrize("kind", ["call", "number"])
async def test_individual_calling_allowed_with_approved_decision(client, session, settings, kind):
    org_id, user_id, _ = await _new_org(
        session, client, f"ind-approved-{kind}@example.com", account_type="individual"
    )
    await _set_profile(
        session,
        org_id,
        status="approved",
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    assert await telephony_access.refusal(session, settings, org_id, kind) is None
    await telephony_access.require_telephony_allowed(session, org_id, kind, settings=settings)


@pytest.mark.parametrize("kind", ["call", "number"])
@pytest.mark.parametrize("decision", ["missing", "by_only", "at_only"])
async def test_individual_calling_refused_without_a_recorded_decision(
    client, session, settings, kind, decision
):
    org_id, user_id, _ = await _new_org(
        session,
        client,
        f"ind-nodecision-{kind}-{decision}@example.com",
        account_type="individual",
    )
    await _set_profile(
        session,
        org_id,
        status="approved",
        decided_by=user_id if decision == "by_only" else None,
        decided_at=datetime.now(timezone.utc) if decision == "at_only" else None,
    )
    assert await telephony_access.refusal(session, settings, org_id, kind) == (
        "account_not_verified"
    )
    with pytest.raises(AccountNotVerifiedError):
        await telephony_access.require_telephony_allowed(session, org_id, kind, settings=settings)


@pytest.mark.parametrize("kind", ["call", "number"])
async def test_individual_suspended_profile_refused(client, session, settings, kind):
    org_id, user_id, _ = await _new_org(
        session, client, f"ind-suspended-{kind}@example.com", account_type="individual"
    )
    await _set_profile(
        session,
        org_id,
        status="suspended",
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    assert await telephony_access.refusal(session, settings, org_id, kind) == "account_suspended"


# --------------------------------------------------------------------------------------
# Dispatch: a real persisted Message, refused as DATA, the carrier never touched.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("media_urls", [None, ["https://cdn.example.test/photo.jpg"]])
async def test_individual_dispatch_rejected_without_calling_the_carrier(
    app_with_carrier, session, media_urls
):
    client, fake, _application = app_with_carrier
    fake.name = "telnyx"
    label = "mms" if media_urls else "sms"
    org_id, _, _ = await _new_org(
        session, client, f"ind-dispatch-{label}@example.com", account_type="individual"
    )

    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164="+15550000111", contact_e164="+15550000222"
    )
    session.add(thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="outbound",
        status="queued",
        from_e164=thread.our_e164,
        to_e164=thread.contact_e164,
        body="testing individual dispatch",
        media=list(media_urls or []),
        carrier="telnyx",
    )
    session.add(message)
    await session.commit()

    result = await messaging._dispatch_to_carrier(session, org_id, fake, message, media_urls)

    assert fake.sent == []
    assert result.status == "rejected"
    assert result.error_code == INDIVIDUAL_SMS_CODE
    assert result.failure_reason_public == telephony_access.REFUSAL_PUBLIC_TEXT[INDIVIDUAL_SMS_CODE]
    assert result.error_detail == "Account not allowed to send"


# --------------------------------------------------------------------------------------
# Registration API: every messaging mutation is refused for an individual workspace.
# --------------------------------------------------------------------------------------
REGISTRATION_MUTATIONS = [
    ("POST", "/api/v1/registration/brands", {"name": "Ind Brand"}),
    ("POST", "/api/v1/registration/brands/{brand_id}/submit", None),
    (
        "POST",
        "/api/v1/registration/campaigns",
        {"brand_id": "{brand_id}", "name": "Ind Campaign"},
    ),
    ("POST", "/api/v1/registration/campaigns/{campaign_id}/submit", None),
    (
        "POST",
        "/api/v1/registration/tollfree",
        {"number_id": "{number_id}", "business_name": "Ind Co"},
    ),
    ("POST", "/api/v1/registration/tollfree/{tfv_id}/submit", None),
    (
        "POST",
        "/api/v1/registration/brands/{brand_id}/status",
        {"status": "approved"},
    ),
    (
        "POST",
        "/api/v1/registration/campaigns/{campaign_id}/status",
        {"status": "approved"},
    ),
    (
        "POST",
        "/api/v1/registration/tollfree/{tfv_id}/status",
        {"status": "approved"},
    ),
]


@pytest.mark.parametrize("method,path,payload", REGISTRATION_MUTATIONS)
async def test_individual_registration_mutations_refused(
    client, session, settings, method, path, payload
):
    org_id, _, token = await _new_org(
        session, client, f"ind-reg-{abs(hash(path))}@example.com", account_type="individual"
    )
    # Placeholder ids: the gate runs before the body is looked up, so any UUID works.
    brand_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    number_id = uuid.uuid4()
    tfv_id = uuid.uuid4()
    url = path.format(
        brand_id=brand_id, campaign_id=campaign_id, number_id=number_id, tfv_id=tfv_id
    )
    body = None
    if payload is not None:
        body = {
            k: (
                str(v).format(
                    brand_id=brand_id,
                    campaign_id=campaign_id,
                    number_id=number_id,
                    tfv_id=tfv_id,
                )
                if isinstance(v, str)
                else v
            )
            for k, v in payload.items()
        }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Org-Id": str(org_id),
        "X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN,
    }
    r = await client.request(method, url, json=body, headers=headers)
    assert r.status_code in (201, 404), r.text


@pytest.mark.parametrize("registered", [False, True])
async def test_approved_personal_account_dispatch_requires_telnyx_campaign(
    app_with_carrier, session, registered
):
    from app.compliance.telnyx_approval import build_evidence
    from app.models import OrgNumber
    from app.models.numbers import Brand, Campaign

    client, fake, _application = app_with_carrier
    fake.name = "telnyx"
    org_id, user_id, _ = await _new_org(
        session, client, "registered-person@example.com", account_type="individual"
    )
    await _set_profile(
        session,
        org_id,
        status="approved",
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    set_org_context(session, org_id)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164="+15125550111",
        carrier="telnyx",
        number_type="local",
        status="active",
        is_active=True,
    )
    if registered:
        brand = Brand(id=uuid.uuid4(), org_id=org_id, name="Customer Company", status="approved")
        session.add(brand)
        await session.flush()
        campaign = Campaign(
            id=uuid.uuid4(),
            org_id=org_id,
            brand_id=brand.id,
            name="Customer campaign",
            status="approved",
            carrier_refs={
                "telnyx": "carrier-campaign",
                "telnyx_approval": build_evidence(
                    state="approved",
                    carrier_id="carrier-campaign",
                    checked_at=datetime.now(timezone.utc),
                    source="status_decision",
                ),
            },
        )
        session.add(campaign)
        await session.flush()
        number.campaign_id = campaign.id
        number.provisioning = {
            "telnyx_campaign_assignment": {
                "state": "assigned",
                "campaign_id": str(campaign.id),
                "carrier_id": "carrier-campaign",
            }
        }
    session.add(number)
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=number.e164, contact_e164="+15125550222"
    )
    session.add(thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="outbound",
        status="queued",
        from_e164=number.e164,
        to_e164=thread.contact_e164,
        body="Your appointment is confirmed.",
        carrier="telnyx",
        media=[],
    )
    session.add(message)
    await session.commit()
    result = await messaging._dispatch_to_carrier(session, org_id, fake, message, None)
    if registered:
        assert len(fake.sent) == 1
        assert result.status == "accepted"
    else:
        assert fake.sent == []
        assert result.status == "rejected"
        assert result.error_code == "registration_required"
