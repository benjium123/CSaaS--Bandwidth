"""Individual (person) KYC: the service-level rules for an account_type='individual'
workspace.

Everything here drives app.services.individual_kyc / app.services.kyc / app.services.kyc_tick
/ app.services.suspension directly against the async session fixture - no HTTP, no network.
The API integration tests for the same flow live in a separate module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import PermissionDeniedError, ValidationFailedError
from app.models import (
    KycCheck,
    KycPerson,
    KycProfile,
    Org,
    OrgMembership,
    PlatformOperator,
    Role,
    User,
)
from app.services import individual_kyc, kyc, kyc_tick, suspension
from tests.conftest import make_settings

NOW = datetime(2027, 9, 20, 12, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------------
# Fixture builders
# ----------------------------------------------------------------------------------
async def _make_individual(session, *, profile_status="draft", person_status="verified"):
    """Org(individual) + owner User/Role/OrgMembership + a complete PERSONAL-ONLY
    KycProfile + a verified Didit owner KycPerson. Returns (org, user, profile, person)."""
    org = Org(
        id=uuid.uuid4(),
        name="Jane Smith",
        slug=f"ind-{uuid.uuid4().hex[:8]}",
        account_type="individual",
    )
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)

    user = User(
        id=uuid.uuid4(),
        email=f"jane-{uuid.uuid4().hex[:6]}@example.test",
        hashed_password="x",
        full_name="Jane Smith",
    )
    session.add(user)
    await session.flush()

    role = Role(
        id=uuid.uuid4(),
        org_id=org.id,
        name="owner",
        permissions=["*"],
        is_system=True,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role_id=role.id)
    )

    profile = KycProfile(
        id=uuid.uuid4(),
        org_id=org.id,
        status=profile_status,
        country="US",
        legal_name="Jane Smith",
        business_email="jane@example.test",
        business_phone="+15551234567",
        use_case={
            "description": "Personal calls only",
            "vertical": "personal",
            "who_you_contact": "friends and family",
            "list_source": "personal contacts",
            "monthly_calls": 50,
            "monthly_texts": 0,
            "destination_countries": ["US"],
        },
        agreement_version=kyc.AGREEMENT_VERSION,
        agreement_accepted_at=NOW,
        agreement_accepted_by=user.id,
    )
    session.add(profile)

    person = KycPerson(
        id=uuid.uuid4(),
        org_id=org.id,
        role="owner",
        user_id=user.id,
        full_name="Jane Smith",
        email=user.email,
        status=person_status,
        identity_provider="didit",
        provider_session_id=f"sess-{uuid.uuid4().hex[:8]}",
        identity_hash="a" * 64,
        verified_name="Jane Smith",
        verified_at=NOW - timedelta(days=1),
    )
    session.add(person)
    await session.commit()
    set_org_context(session, org.id)
    return org, user, profile, person


async def _make_operator(session, *, role="admin", active=True):
    """A real User + PlatformOperator. Returns (operator, user)."""
    user = User(
        id=uuid.uuid4(),
        email=f"op-{uuid.uuid4().hex[:6]}@example.test",
        hashed_password="x",
        full_name="Op",
    )
    session.add(user)
    await session.flush()
    op = PlatformOperator(
        id=uuid.uuid4(),
        user_id=user.id,
        role=role,
        is_active=active,
    )
    session.add(op)
    await session.commit()
    return op, user


async def _write_sanctions_file(settings):
    from app.services import sanctions

    directory = sanctions.list_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ofac_sdn.txt").write_text("SOMEONE ELSE ENTIRELY\n", encoding="utf-8")


# ==================================================================================
# missing_for_submission
# ==================================================================================
async def test_missing_for_submission_empty_for_complete_individual(session):
    """A complete individual application needs no company fields and no documents."""
    _, _, profile, _ = await _make_individual(session)
    assert await individual_kyc.missing_for_submission(session, profile) == []


async def test_missing_for_submission_lists_personal_fields(session):
    _, _, profile, _ = await _make_individual(session)
    profile.country = None
    profile.legal_name = None
    profile.business_email = None
    profile.business_phone = None
    await session.commit()
    set_org_context(session, profile.org_id)
    missing = await individual_kyc.missing_for_submission(session, profile)
    assert set(missing) >= {"country", "legal_name", "business_email", "business_phone"}
    # No business-only keys leak into an individual application.
    assert not any(m in missing for m in ("entity_type", "registration_number", "documents"))


async def test_missing_for_submission_requires_zero_texts(session):
    _, _, profile, _ = await _make_individual(session)
    profile.use_case = {**profile.use_case, "monthly_texts": 5}
    await session.commit()
    set_org_context(session, profile.org_id)
    assert "use_case.monthly_texts" in await individual_kyc.missing_for_submission(
        session, profile
    )


# ==================================================================================
# validate_owner
# ==================================================================================
@pytest.mark.parametrize(
    "person_status",
    ["pending", "not_started", "requires_input", "canceled"],
)
async def test_validate_owner_blocks_unverified_person(session, person_status):
    _, _, profile, _ = await _make_individual(session, person_status=person_status)
    problems = await individual_kyc.validate_owner(session, profile)
    assert any("not been verified" in p for p in problems)


async def test_validate_owner_blocks_stripe_provider(session):
    _, _, profile, person = await _make_individual(session)
    person.identity_provider = "stripe"
    await session.commit()
    set_org_context(session, profile.org_id)
    assert any("Didit" in p for p in await individual_kyc.validate_owner(session, profile))


async def test_validate_owner_blocks_missing_session(session):
    _, _, profile, person = await _make_individual(session)
    person.provider_session_id = None
    await session.commit()
    set_org_context(session, profile.org_id)
    assert any(
        "provider session" in p for p in await individual_kyc.validate_owner(session, profile)
    )


async def test_validate_owner_blocks_missing_identity_hash(session):
    _, _, profile, person = await _make_individual(session)
    person.identity_hash = None
    await session.commit()
    set_org_context(session, profile.org_id)
    assert any(
        "identity record" in p for p in await individual_kyc.validate_owner(session, profile)
    )


async def test_validate_owner_blocks_name_mismatch(session):
    _, _, profile, person = await _make_individual(session)
    person.verified_name = "Someone Else"
    await session.commit()
    set_org_context(session, profile.org_id)
    assert any(
        "does not match" in p for p in await individual_kyc.validate_owner(session, profile)
    )


async def test_validate_owner_blocks_wrong_submitting_user(session):
    _, _, profile, _ = await _make_individual(session)
    other = uuid.uuid4()
    assert any(
        "signed-in user" in p
        for p in await individual_kyc.validate_owner(session, profile, other)
    )


async def test_validate_owner_blocks_beneficial_owner(session):
    org, user, profile, _ = await _make_individual(session)
    session.add(
        KycPerson(
            id=uuid.uuid4(),
            org_id=org.id,
            role="beneficial_owner",
            user_id=user.id,
            full_name="Other Person",
            status="verified",
        )
    )
    await session.commit()
    set_org_context(session, org.id)
    assert any(
        "beneficial owners" in p for p in await individual_kyc.validate_owner(session, profile)
    )


async def test_validate_owner_blocks_stale_reverification(session):
    """A due annual re-verification whose owner verified BEFORE the due date is stale."""
    _, _, profile, person = await _make_individual(session)
    profile.next_reverification_at = datetime.now(timezone.utc) - timedelta(days=1)
    person.verified_at = datetime.now(timezone.utc) - timedelta(days=400)
    await session.commit()
    set_org_context(session, profile.org_id)
    problems = await individual_kyc.validate_owner(session, profile)
    assert any("annual re-verification" in p for p in problems)


async def test_validate_owner_allows_fresh_reverification(session):
    """A due annual re-verification whose owner verified AFTER the due date is not stale."""
    _, _, profile, person = await _make_individual(session)
    profile.next_reverification_at = datetime.now(timezone.utc) - timedelta(days=1)
    person.verified_at = datetime.now(timezone.utc)
    await session.commit()
    set_org_context(session, profile.org_id)
    problems = await individual_kyc.validate_owner(session, profile)
    assert not any("annual re-verification" in p for p in problems)


# ==================================================================================
# validate_person_creation
# ==================================================================================
async def test_validate_person_creation_rejects_duplicate_owner(session):
    _, user, profile, _ = await _make_individual(session)
    with pytest.raises(ValidationFailedError):
        await individual_kyc.validate_person_creation(session, profile, "owner", user.id)


async def test_validate_person_creation_rejects_non_owner_role(session):
    _, user, profile, _ = await _make_individual(session)
    with pytest.raises(ValidationFailedError):
        await individual_kyc.validate_person_creation(session, profile, "admin", user.id)


async def test_validate_person_creation_rejects_unbound_user(session):
    _, _, profile, _ = await _make_individual(session)
    with pytest.raises(ValidationFailedError):
        await individual_kyc.validate_person_creation(session, profile, "owner", None)


# ==================================================================================
# start_person_verification: actor binding + explicit Didit
# ==================================================================================
async def test_start_person_verification_denies_wrong_actor(session, monkeypatch):
    _, _, _, person = await _make_individual(session, person_status="not_started")
    person.identity_hash = None
    await session.commit()
    set_org_context(session, person.org_id)

    called = {"n": 0}

    async def _boom(*a, **kw):  # pragma: no cover - must not run
        called["n"] += 1
        raise AssertionError("provider must not be touched")

    from app.services import identity_provider

    monkeypatch.setattr(identity_provider.DiditIdentityProvider, "start", _boom)

    with pytest.raises(PermissionDeniedError):
        await kyc.start_person_verification(
            session,
            make_settings(kyc_identity_provider="stripe"),
            person,
            return_url="https://example.test/return",
            actor_user_id=uuid.uuid4(),
        )
    assert called["n"] == 0


@pytest.mark.parametrize("person_status", ["not_started", "requires_input", "canceled"])
async def test_start_person_verification_retry_uses_didit_even_when_global_is_stripe(
    session, monkeypatch, person_status
):
    _, user, _, person = await _make_individual(session, person_status=person_status)
    person.identity_hash = None
    await session.commit()
    set_org_context(session, person.org_id)

    seen = {}

    async def _fake_start(self, settings, *, org_id, person_id, email, return_url):
        seen["provider"] = self.name
        from app.services.identity_provider import StartedVerification

        return StartedVerification("didit", "sess-new", "https://didit.test/x")

    from app.services import identity_provider

    monkeypatch.setattr(identity_provider.DiditIdentityProvider, "start", _fake_start)

    url = await kyc.start_person_verification(
        session,
        make_settings(kyc_identity_provider="stripe"),
        person,
        return_url="https://example.test/return",
        actor_user_id=user.id,
    )
    assert seen["provider"] == "didit"
    assert url == "https://didit.test/x"
    assert person.identity_provider == "didit"
    assert person.provider_session_id == "sess-new"


async def test_start_person_verification_suspended_owner_can_retry_didit(
    session, monkeypatch
):
    """A suspended individual whose annual re-verification is overdue may retry Didit.

    The owner is bound to the person and already has an identity_hash, so the retry is
    only allowed because the profile is suspended (annual re-verification is overdue).
    """
    org, user, profile, person = await _make_individual(
        session, profile_status="suspended", person_status="verified"
    )
    profile.next_reverification_at = datetime.now(timezone.utc) - timedelta(days=1)
    person.verified_at = datetime.now(timezone.utc) - timedelta(days=400)
    await session.commit()
    set_org_context(session, org.id)

    seen = {}

    async def _fake_start(self, settings, *, org_id, person_id, email, return_url):
        seen["provider"] = self.name
        seen["org_id"] = org_id
        seen["person_id"] = person_id
        from app.services.identity_provider import StartedVerification

        return StartedVerification("didit", "sess-retry", "https://didit.test/retry")

    from app.services import identity_provider

    monkeypatch.setattr(identity_provider.DiditIdentityProvider, "start", _fake_start)

    url = await kyc.start_person_verification(
        session,
        make_settings(kyc_identity_provider="stripe"),
        person,
        return_url="https://example.test/return",
        actor_user_id=user.id,
    )
    assert seen["provider"] == "didit"
    assert seen["org_id"] == org.id
    assert seen["person_id"] == person.id
    assert url == "https://didit.test/retry"
    assert person.status == "pending"
    assert person.identity_provider == "didit"
    assert person.provider_session_id == "sess-retry"
    await session.commit()
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "suspended"


async def test_start_person_verification_suspended_owner_wrong_actor_denied(
    session, monkeypatch
):
    """Even with the suspended retry path open, a different actor is refused."""
    org, _, profile, person = await _make_individual(
        session, profile_status="suspended", person_status="verified"
    )
    profile.next_reverification_at = datetime.now(timezone.utc) - timedelta(days=1)
    person.verified_at = datetime.now(timezone.utc) - timedelta(days=400)
    await session.commit()
    set_org_context(session, org.id)

    called = {"n": 0}

    async def _boom(*a, **kw):  # pragma: no cover - must not run
        called["n"] += 1
        raise AssertionError("provider must not be touched")

    from app.services import identity_provider

    monkeypatch.setattr(identity_provider.DiditIdentityProvider, "start", _boom)

    with pytest.raises(PermissionDeniedError):
        await kyc.start_person_verification(
            session,
            make_settings(kyc_identity_provider="stripe"),
            person,
            return_url="https://example.test/return",
            actor_user_id=uuid.uuid4(),
        )
    assert called["n"] == 0


# ==================================================================================
# submit: validation raises before transition
# ==================================================================================
@pytest.mark.parametrize(
    "person_status",
    ["pending", "not_started", "requires_input", "canceled"],
)
async def test_submit_raises_for_unverified_person(session, person_status):
    _, user, profile, _ = await _make_individual(session, person_status=person_status)
    settings = make_settings(kyc_enforced=True)
    with pytest.raises(ValidationFailedError):
        await kyc.submit(session, settings, profile, user_id=user.id, flagged_login=False)
    assert profile.status == "draft"


async def test_submit_raises_for_stripe_provider(session):
    _, user, profile, person = await _make_individual(session)
    person.identity_provider = "stripe"
    await session.commit()
    set_org_context(session, profile.org_id)
    settings = make_settings(kyc_enforced=True)
    with pytest.raises(ValidationFailedError):
        await kyc.submit(session, settings, profile, user_id=user.id, flagged_login=False)
    assert profile.status == "draft"


async def test_submit_raises_for_missing_hash(session):
    _, user, profile, person = await _make_individual(session)
    person.identity_hash = None
    await session.commit()
    set_org_context(session, profile.org_id)
    settings = make_settings(kyc_enforced=True)
    with pytest.raises(ValidationFailedError):
        await kyc.submit(session, settings, profile, user_id=user.id, flagged_login=False)
    assert profile.status == "draft"


async def test_submit_raises_for_missing_session(session):
    _, user, profile, person = await _make_individual(session)
    person.provider_session_id = None
    await session.commit()
    set_org_context(session, profile.org_id)
    settings = make_settings(kyc_enforced=True)
    with pytest.raises(ValidationFailedError):
        await kyc.submit(session, settings, profile, user_id=user.id, flagged_login=False)
    assert profile.status == "draft"


async def test_submit_succeeds_with_local_sanctions_and_no_business_http(session, tmp_path):
    """A complete individual application submits. Any business HTTP call is a failure.

    run_all catches AssertionError, so raising from the transport alone proves nothing:
    we also record every request and assert the list is empty after submit.
    """
    _, user, profile, _ = await _make_individual(session)
    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    await _write_sanctions_file(settings)

    requests: list[httpx.Request] = []

    def _no_http(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        requests.append(request)
        raise AssertionError(f"unexpected business HTTP call: {request.method} {request.url}")

    transport = httpx.MockTransport(_no_http)
    async with httpx.AsyncClient(transport=transport) as http_client:
        await kyc.submit(
            session,
            settings,
            profile,
            user_id=user.id,
            flagged_login=False,
            http_client=http_client,
        )
    assert requests == []
    await session.commit()
    set_org_context(session, profile.org_id)
    await session.refresh(profile)
    assert profile.status == "submitted"
    assert profile.status != "approved"

    # An individual application must never run business-only checks.
    kinds = set(
        (
            await session.execute(
                sa.select(KycCheck.kind).where(KycCheck.org_id == profile.org_id)
            )
        )
        .scalars()
        .all()
    )
    assert not (kinds & {"registry", "website", "email_domain"})


# ==================================================================================
# approve: reviewer vs admin
# ==================================================================================
async def test_approve_rejects_non_admin_operator(session, tmp_path):
    _, _, profile, _ = await _make_individual(session, profile_status="submitted")
    reviewer, reviewer_user = await _make_operator(session, role="reviewer")
    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    with pytest.raises(PermissionDeniedError):
        await kyc.approve(session, settings, profile, reviewer_user.id, "ok")


async def test_approve_allows_admin_and_records_decision(session, tmp_path, monkeypatch):
    _, _, profile, _ = await _make_individual(session, profile_status="submitted")
    admin, admin_user = await _make_operator(session, role="admin")
    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    await _write_sanctions_file(settings)

    # Mock the rescreen so it writes genuine pass rows for the checks the individual
    # approval gate requires (sanctions, ban_list, name_match).
    async def _fake_rescreen(session_, settings_, profile_):
        for kind in ("sanctions", "ban_list", "name_match"):
            session_.add(
                KycCheck(
                    id=uuid.uuid4(),
                    org_id=profile_.org_id,
                    kind=kind,
                    result="pass",
                    summary="ok",
                )
            )
        await session_.flush()

    monkeypatch.setattr(kyc, "rescreen", _fake_rescreen)

    await kyc.approve(session, settings, profile, admin_user.id, "looks good")
    await session.commit()
    set_org_context(session, profile.org_id)
    await session.refresh(profile)
    assert profile.status == "approved"
    assert profile.decided_by == admin_user.id
    assert profile.decided_at is not None


# ==================================================================================
# Didit webhook identity update must not touch the application status
# ==================================================================================
async def test_didit_webhook_identity_update_leaves_application_untouched(session):
    org, _, profile, person = await _make_individual(session, profile_status="submitted")
    person.status = "pending"
    person.identity_hash = None
    await session.commit()
    set_org_context(session, org.id)

    payload = {
        "webhook_type": "session.completed",
        "status": "Approved",
        "session_id": person.provider_session_id,
        "vendor_data": str(person.id),
        "metadata": {
            "purpose": "kyc_person",
            "org_id": str(org.id),
            "person_id": str(person.id),
        },
        "decision": {
            "status": "Approved",
            "id_verifications": [
                {
                    "first_name": "Jane",
                    "last_name": "Smith",
                    "date_of_birth": "1980-04-02",
                    "document_type": "Passport",
                    "document_number": "P1",
                    "issuing_state": "us",
                }
            ],
        },
    }
    await kyc.handle_didit_event(session, make_settings(kyc_identity_provider="didit"), payload)
    await session.commit()
    set_org_context(session, org.id)
    await session.refresh(person)
    await session.refresh(profile)
    assert person.status == "verified"
    assert profile.status == "submitted"


# ==================================================================================
# Annual re-verification tick for an individual
# ==================================================================================
async def test_annual_tick_individual_stays_reverification_due_inside_grace(session, tmp_path):
    org, _, profile, _ = await _make_individual(session, profile_status="approved")
    profile.next_reverification_at = NOW - timedelta(days=1)
    await session.commit()
    set_org_context(session, org.id)

    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    await _write_sanctions_file(settings)

    await kyc_tick.reverification_tick(session, settings, now=NOW)
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "reverification_due"

    await kyc_tick.reverification_tick(session, settings, now=NOW + timedelta(days=3))
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "reverification_due"
    assert profile.status != "approved"


async def test_annual_tick_individual_needs_info_after_grace(session, tmp_path):
    org, _, profile, _ = await _make_individual(session, profile_status="reverification_due")
    profile.next_reverification_at = NOW - timedelta(days=1)
    await session.commit()
    set_org_context(session, org.id)

    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    await _write_sanctions_file(settings)

    await kyc_tick.reverification_tick(
        session, settings, now=NOW + timedelta(days=settings.kyc_reverify_grace_days + 1)
    )
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "needs_info"
    assert profile.status != "approved"


# ==================================================================================
# Unsuspend refuses an unapproved individual
# ==================================================================================
async def test_unsuspend_unapproved_individual_raises(session):
    """A suspended individual whose owner is incomplete cannot be unsuspended: the real
    services.suspension.unsuspend delegates to kyc.approve, which raises
    ValidationFailedError(kyc_incomplete) and leaves the profile suspended."""
    org, _, profile, person = await _make_individual(
        session, profile_status="suspended", person_status="pending"
    )
    person.identity_hash = None
    profile.status_before_suspension = "approved"
    await session.commit()
    set_org_context(session, org.id)

    admin, admin_user = await _make_operator(session, role="admin")

    with pytest.raises(ValidationFailedError) as excinfo:
        await suspension.unsuspend(
            session, org.id, operator_id=admin_user.id, note="appeal accepted"
        )
    assert excinfo.value.code == "kyc_incomplete"
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "suspended"
    assert profile.status != "approved"


async def test_unsuspend_approved_individual_clears_suspension(session, tmp_path):
    """A suspended individual with a complete, verified owner and real local sanctions
    can be unsuspended by an active admin: the profile is approved, decided_by is the
    operator, and the suspension fields are cleared."""
    org, _, profile, person = await _make_individual(
        session, profile_status="suspended", person_status="verified"
    )
    admin, admin_user = await _make_operator(session, role="admin")
    profile.status_before_suspension = "approved"
    profile.suspended_at = NOW
    profile.suspended_by = admin_user.id
    profile.suspension_reason = "manual review"
    await session.commit()
    set_org_context(session, org.id)

    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    await _write_sanctions_file(settings)
    session.info["settings"] = settings

    await suspension.unsuspend(
        session, org.id, operator_id=admin_user.id, note="appeal accepted"
    )
    await session.commit()
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "approved"
    assert profile.decided_by == admin_user.id
    assert profile.status_before_suspension is None
    assert profile.suspended_at is None
    assert profile.suspended_by is None
    assert profile.suspension_reason is None


# ==================================================================================
# approval_blockers
# ==================================================================================
async def test_approval_blockers_pending_screening(session):
    """Pending sanctions/ban_list checks and a passing name_match still block approval."""
    _, _, profile, _ = await _make_individual(session, profile_status="submitted")
    for kind in ("sanctions", "ban_list"):
        session.add(
            KycCheck(
                id=uuid.uuid4(),
                org_id=profile.org_id,
                kind=kind,
                result="pending",
                summary="running",
            )
        )
    session.add(
        KycCheck(
            id=uuid.uuid4(),
            org_id=profile.org_id,
            kind="name_match",
            result="pass",
            summary="ok",
        )
    )
    await session.commit()
    set_org_context(session, profile.org_id)
    blockers = await kyc.approval_blockers(session, profile)
    assert any("sanctions" in b for b in blockers)
    assert any("ban list" in b for b in blockers)
    assert not any("names" in b for b in blockers)


async def test_approval_blockers_bad_names(session):
    _, _, profile, person = await _make_individual(session, profile_status="submitted")
    person.verified_name = "Someone Else"
    for kind in ("sanctions", "ban_list"):
        session.add(
            KycCheck(
                id=uuid.uuid4(),
                org_id=profile.org_id,
                kind=kind,
                result="pass",
                summary="ok",
            )
        )
    session.add(
        KycCheck(
            id=uuid.uuid4(),
            org_id=profile.org_id,
            kind="name_match",
            result="fail",
            summary="mismatch",
        )
    )
    await session.commit()
    set_org_context(session, profile.org_id)
    blockers = await kyc.approval_blockers(session, profile)
    assert any("names" in b for b in blockers)
