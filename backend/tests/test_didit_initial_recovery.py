"""Service-level tests for Didit incomplete-initial-approval recovery.

These tests exercise ``app.services.kyc.handle_didit_event`` directly. They
cover the recovery path for an incomplete *initial* Approved event, the narrow
legacy repair for a verified person missing an identity hash, duplicate
protection for already-verified people, session transplant rejection, the
expired sentinel path and the retry flow that starts a fresh Didit session.

HTTP-level tamper/replay/dedup coverage lives in the existing webhook suite;
these tests intentionally assert on service behaviour and persisted state.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import KycProfile
from app.services.identity_provider import StartedVerification
from app.services.kyc import handle_didit_event, start_person_verification
from tests.test_p44_didit_webhook import (
    didit_settings,
    payload_for,
    reload_person,
    seed_person,
)

COMPLETE_DECISION = [
    {
        "first_name": "Dan",
        "last_name": "Owner",
        "date_of_birth": "1980-04-02",
    }
]


async def _profile_status(session, org_id):
    result = await session.execute(select(KycProfile).where(KycProfile.org_id == org_id))
    profile = result.scalars().first()
    return None if profile is None else profile.status


async def test_incomplete_initial_approved_requires_input(session):
    org, person = await seed_person(session, status="pending", identity_hash=None)
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    payload = payload_for(org, person, status="Approved", event_id="evt-initial-1")

    await handle_didit_event(session, settings, payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "requires_input"
    assert refreshed.identity_hash is None
    assert refreshed.verified_at is None
    assert refreshed.last_error == "identity_unconfirmed: identity details are incomplete"


async def test_complete_same_session_event_verifies_and_keeps_profile_draft(session):
    org, person = await seed_person(session, status="pending", identity_hash=None)
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    session_id = person.provider_session_id

    incomplete = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-initial-2a",
        session_id=session_id,
    )
    await handle_didit_event(session, settings, incomplete)

    parked = await reload_person(session, person.id)
    assert parked.status == "requires_input"
    assert parked.identity_hash is None

    complete = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-initial-2b",
        session_id=session_id,
    )
    complete["decision"] = {"id_verifications": list(COMPLETE_DECISION)}

    await handle_didit_event(session, settings, complete)

    verified = await reload_person(session, person.id)
    assert verified.status == "verified"
    assert verified.identity_hash
    assert verified.verified_at is not None
    assert await _profile_status(session, org.id) == "draft"


@pytest.mark.parametrize(
    "decision",
    [
        [
            {
                "first_name": None,
                "last_name": None,
                "date_of_birth": "1980-04-02",
            }
        ],
        [
            {
                "first_name": "Dan",
                "last_name": "Owner",
                "date_of_birth": None,
            }
        ],
    ],
    ids=["missing-name", "missing-dob"],
)
async def test_incomplete_decision_variants_require_input(session, decision):
    org, person = await seed_person(session, status="pending", identity_hash=None)
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    payload = payload_for(org, person, status="Approved", event_id="evt-variant")
    payload["decision"] = {"id_verifications": decision}

    await handle_didit_event(session, settings, payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "requires_input"
    assert refreshed.identity_hash is None
    assert refreshed.verified_at is None


async def test_legacy_verified_without_hash_incomplete_requires_input(session):
    org, person = await seed_person(session, status="verified", identity_hash=None)
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    payload = payload_for(org, person, status="Approved", event_id="evt-legacy-1")

    await handle_didit_event(session, settings, payload)

    repaired = await reload_person(session, person.id)
    assert repaired.status == "requires_input"
    assert repaired.identity_hash is None


async def test_legacy_verified_without_hash_complete_is_repaired(session):
    org, person = await seed_person(session, status="verified", identity_hash=None)
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    payload = payload_for(org, person, status="Approved", event_id="evt-legacy-2")
    payload["decision"] = {"id_verifications": list(COMPLETE_DECISION)}

    await handle_didit_event(session, settings, payload)

    repaired = await reload_person(session, person.id)
    assert repaired.status == "verified"
    assert repaired.identity_hash


async def test_verified_duplicate_with_hash_is_not_demoted(session):
    org, person = await seed_person(session, status="verified", identity_hash="existing-hash")
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    payload = payload_for(org, person, status="Approved", event_id="evt-dup-1")

    await handle_didit_event(session, settings, payload)

    duplicate = await reload_person(session, person.id)
    assert duplicate.status == "verified"
    assert duplicate.identity_hash == "existing-hash"


async def test_transplanted_session_cannot_repair(session):
    org, person = await seed_person(session, status="verified", identity_hash=None)
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    payload = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-transplant-1",
        session_id="sess-transplant-other",
    )
    payload["decision"] = {"id_verifications": list(COMPLETE_DECISION)}

    await handle_didit_event(session, settings, payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.identity_hash is None


async def test_expired_sentinel_cannot_repair(session):
    org, person = await seed_person(session, status="verified", identity_hash="known-hash")
    org.account_type = "individual"
    await session.commit()

    settings = didit_settings()
    session_id = person.provider_session_id

    expired = payload_for(
        org,
        person,
        status="Kyc Expired",
        event_id="evt-expired-1",
        session_id=session_id,
    )
    await handle_didit_event(session, settings, expired)

    after_expired = await reload_person(session, person.id)
    assert after_expired.status == "canceled"
    assert after_expired.last_error.startswith("identity_expired")

    complete = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-expired-2",
        session_id=session_id,
    )
    complete["decision"] = {"id_verifications": list(COMPLETE_DECISION)}
    await handle_didit_event(session, settings, complete)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "canceled"
    assert refreshed.last_error.startswith("identity_expired")
    assert refreshed.identity_hash == "known-hash"


@pytest.mark.parametrize("account_type", ["business", "individual"])
async def test_retry_starts_new_session_and_verifies(account_type, session, monkeypatch):
    from app.models import User

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{user_id}@example.test",
        hashed_password="unused-test-hash",
        full_name="Dan Owner",
    )
    session.add(user)
    await session.flush()
    org, person = await seed_person(session, status="pending", identity_hash=None, user_id=user.id)
    org.account_type = account_type
    await session.commit()

    settings = didit_settings()
    stale_session_id = person.provider_session_id

    incomplete = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-retry-1",
        session_id=stale_session_id,
    )
    await handle_didit_event(session, settings, incomplete)

    parked = await reload_person(session, person.id)
    assert parked.status == "requires_input"

    async def _fake_start(self, *args, **kwargs):
        return StartedVerification("didit", "sess-new", "https://example.test/check")

    monkeypatch.setattr(
        "app.services.identity_provider.DiditIdentityProvider.start",
        _fake_start,
    )

    if account_type == "individual":
        from app.errors import PermissionDeniedError

        wrong_actor_id = uuid.uuid4()
        with pytest.raises(PermissionDeniedError):
            await start_person_verification(
                session,
                settings,
                person,
                return_url="https://example.test/settings/verification",
                actor_user_id=wrong_actor_id,
            )
    await start_person_verification(
        session,
        settings,
        person,
        return_url="https://example.test/settings/verification",
        actor_user_id=user.id,
    )

    restarted = await reload_person(session, person.id)
    assert restarted.status == "pending"
    assert restarted.provider_session_id == "sess-new"

    stale_complete = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-retry-2",
        session_id=stale_session_id,
    )
    stale_complete["decision"] = {"id_verifications": list(COMPLETE_DECISION)}
    await handle_didit_event(session, settings, stale_complete)

    still_pending = await reload_person(session, person.id)
    assert still_pending.status == "pending"
    assert still_pending.identity_hash is None

    fresh_complete = payload_for(
        org,
        person,
        status="Approved",
        event_id="evt-retry-3",
        session_id="sess-new",
    )
    fresh_complete["decision"] = {"id_verifications": list(COMPLETE_DECISION)}
    await handle_didit_event(session, settings, fresh_complete)

    verified = await reload_person(session, person.id)
    assert verified.status == "verified"
    assert verified.identity_hash
