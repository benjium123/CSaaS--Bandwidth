"""P44 compatibility tests for the Didit adapter and the KYC expiry handler.

These tests deliberately do NOT re-derive the canonical form from the implementation.
The known vector below is an INDEPENDENT literal of the exact bytes Didit signs, and the
signature is computed with a hand-written HMAC over those bytes. That way a change to
``didit_client.canonical_payload`` that silently alters the wire format is caught here
rather than being masked by the helper the implementation itself uses.

No network calls: every HTTP interaction goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import FeatureUnavailableError, UnauthenticatedError
from app.models import KycPerson, KycProfile, Org
from app.services import didit_client, kyc
from tests.conftest import make_settings

DIDIT_KEY = "didit-test-api-key"
DIDIT_SECRET = "didit-test-webhook-secret"
WORKFLOW_ID = "11111111-1111-1111-1111-111111111111"


def didit_settings(**overrides):
    # kyc_enforced defaults to True here so the telephony gate actually evaluates the
    # profile status: a business seeded approved->needs_info must be refused a call.
    base = {
        "kyc_identity_provider": "didit",
        "didit_api_key": DIDIT_KEY,
        "didit_workflow_id": WORKFLOW_ID,
        "didit_webhook_secret": DIDIT_SECRET,
        "kyc_enforced": True,
    }
    base.update(overrides)
    return make_settings(**base)


# ------------------------------------------------------------------------------------
# 1. Canonical form: independent known vector
# ------------------------------------------------------------------------------------
# The exact bytes Didit signs for this payload, written out by hand. Keys are sorted
# recursively, whole-valued floats are normalised to ints recursively (including inside
# nested lists/dicts), -0.0 becomes 0, booleans and nulls survive, and non-ASCII is
# emitted as UTF-8 (ensure_ascii=False).
CANONICAL_VECTOR_PAYLOAD = {
    "z": 1.25,
    "a": {"b": [1, 2, {"c": "José", "d": "東京"}]},
    "whole": 100.0,
    "negzero": -0.0,
    "flag": True,
    "nothing": None,
    "nested": [100.0, -0.0, 1.25],
}
CANONICAL_VECTOR_BYTES = (
    b'{"a":{"b":[1,2,{"c":"Jos\xc3\xa9","d":"\xe6\x9d\xb1\xe4\xba\xac"}]},'
    b'"flag":true,"negzero":0,"nested":[100,0,1.25],"nothing":null,'
    b'"whole":100,"z":1.25}'
)


def test_canonical_payload_matches_independent_known_vector():
    payload = json.loads(json.dumps(CANONICAL_VECTOR_PAYLOAD))
    before = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert didit_client.canonical_payload(payload) == CANONICAL_VECTOR_BYTES
    # The input must not be mutated by canonicalisation. Comparing the re-serialised
    # form with identical settings distinguishes an int from a float (100 vs 100.0).
    after = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert after == before


def test_verify_webhook_accepts_independent_hmac_over_known_vector():
    """Sign the manually-specified canonical bytes and prove verify_webhook accepts it.

    The raw body is deliberately the plain ``json.dumps`` form (with spaces and a
    different float spelling) so the test proves the signature is checked over the
    canonical re-serialisation, not over the bytes on the wire.
    """
    payload = json.loads(json.dumps(CANONICAL_VECTOR_PAYLOAD))
    raw_body = json.dumps(payload).encode()
    signature = hmac.new(
        DIDIT_SECRET.encode(), CANONICAL_VECTOR_BYTES, hashlib.sha256
    ).hexdigest()
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature-V2": signature}

    assert didit_client.verify_webhook(didit_settings(), raw_body, headers) == payload


def test_verify_webhook_rejects_tampered_known_vector():
    payload = json.loads(json.dumps(CANONICAL_VECTOR_PAYLOAD))
    signature = hmac.new(
        DIDIT_SECRET.encode(), CANONICAL_VECTOR_BYTES, hashlib.sha256
    ).hexdigest()
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature-V2": signature}

    tampered = json.loads(json.dumps(payload))
    tampered["whole"] = 101
    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), json.dumps(tampered).encode(), headers)


def test_verify_webhook_rejects_stale_known_vector():
    payload = json.loads(json.dumps(CANONICAL_VECTOR_PAYLOAD))
    raw_body = json.dumps(payload).encode()
    signature = hmac.new(
        DIDIT_SECRET.encode(), CANONICAL_VECTOR_BYTES, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Timestamp": str(int(time.time()) - 400),
        "X-Signature-V2": signature,
    }

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


# ------------------------------------------------------------------------------------
# 2. NaN / Infinity safety on both signature paths
# ------------------------------------------------------------------------------------
def test_verify_webhook_rejects_nan_v2_safely():
    """A V2-signed body containing NaN must be refused, not crash the verifier.

    The signature is deliberately arbitrary: the point is that the canonical form
    cannot be produced for a non-finite number, so the verifier must fail closed with
    UnauthenticatedError rather than letting a ValueError escape as a 500.
    """
    raw_body = b'{"status": "Approved", "value": NaN}'
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature-V2": "deadbeef"}

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_rejects_infinity_v1_with_valid_signature():
    """A V1-signed body containing Infinity must be refused even when the HMAC over
    the raw bytes is VALID.

    Signing the raw bytes with the real secret proves the refusal is the number
    validation, not merely a bad signature: the legacy path would otherwise accept
    these bytes because it never re-serialises them.
    """
    raw_body = b'{"status": "Approved", "value": Infinity}'
    signature = hmac.new(DIDIT_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature": signature}

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


# ------------------------------------------------------------------------------------
# 3. create_session: non-object response
# ------------------------------------------------------------------------------------
async def test_create_session_nonobject_response_raises_feature_unavailable():
    """A JSON array (or scalar) from Didit is not a session and must be refused."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=["not", "a", "session"])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FeatureUnavailableError):
            await didit_client.create_session(
                didit_settings(),
                vendor_data="22222222-2222-2222-2222-222222222222",
                metadata={},
                client=client,
            )


# ------------------------------------------------------------------------------------
# 4. handle_didit_event: metadata non-mapping is a safe no-op
# ------------------------------------------------------------------------------------
async def seed_person(
    session,
    *,
    status="pending",
    provider="didit",
    session_id="sess-1",
    identity_hash=None,
    profile_status="draft",
    role="owner",
):
    org = Org(id=uuid.uuid4(), name="Compat Org", slug=f"compat-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)

    profile = KycProfile(id=uuid.uuid4(), org_id=org.id, status=profile_status)
    session.add(profile)

    person = KycPerson(
        id=uuid.uuid4(),
        org_id=org.id,
        role=role,
        full_name="Dan Owner",
        email="dan@example.test",
        identity_provider=provider,
        provider_session_id=session_id,
        status=status,
        identity_hash=identity_hash,
    )
    session.add(person)
    await session.commit()
    return org, person


def payload_for(org, person, *, status="Approved", session_id=None, event_id="evt-1"):
    return {
        "webhook_type": "status.updated",
        "timestamp": int(time.time()),
        "created_at": int(time.time()),
        "status": status,
        "session_id": session_id if session_id is not None else person.provider_session_id,
        "workflow_id": WORKFLOW_ID,
        "vendor_data": str(person.id),
        "event_id": event_id,
        "metadata": {
            "purpose": "kyc_person",
            "org_id": str(org.id),
            "person_id": str(person.id),
        },
    }


async def reload_person(session, person_id):
    await session.flush()
    row = await session.get(KycPerson, person_id)
    await session.refresh(row)
    return row


async def test_metadata_nonmapping_is_a_safe_noop(session):
    org, person = await seed_person(session, status="pending")
    before_status = person.status
    before_error = person.last_error

    payload = payload_for(org, person, status="Approved")
    payload["metadata"] = "not-a-mapping"

    # The mapping layer must coerce a non-dict metadata to {} rather than raising.
    outcome = didit_client.outcome_from_payload(payload)
    assert outcome is not None
    assert outcome["metadata"] == {}

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == before_status
    assert refreshed.last_error == before_error


# ------------------------------------------------------------------------------------
# 5. Expiry semantics: current session, late events, session scoping
# ------------------------------------------------------------------------------------
async def test_current_session_kyc_expired_cancels_and_blocks_approval(session):
    """An owner whose CURRENT session expires must be canceled with identity_expired,
    the approved profile must drop to needs_info, and the telephony gate must refuse."""
    from app.services import telephony_access

    org, person = await seed_person(
        session,
        status="verified",
        identity_hash=kyc.identity_hash("Dan", "Owner", "1980-04-02"),
        profile_status="approved",
    )
    payload = payload_for(org, person, status="Kyc Expired")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "canceled"
    assert refreshed.last_error is not None
    assert refreshed.last_error.startswith("identity_expired")
    # The identity hash is retained so the same human can be recognised on retry.
    assert refreshed.identity_hash == kyc.identity_hash("Dan", "Owner", "1980-04-02")

    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org.id))
    ).scalar_one()
    assert profile.status == "needs_info"

    code = await telephony_access.refusal(session, didit_settings(), org.id, "call")
    assert code == "account_not_verified"


async def test_duplicate_old_approved_same_expired_session_does_not_autoapprove(session):
    """A late Approved for the SAME expired session must not re-verify the person."""
    org, person = await seed_person(
        session,
        status="verified",
        identity_hash=kyc.identity_hash("Dan", "Owner", "1980-04-02"),
        profile_status="approved",
    )
    expired = payload_for(org, person, status="Kyc Expired", event_id="evt-exp")
    await kyc.handle_didit_event(session, didit_settings(), expired)
    assert (await reload_person(session, person.id)).status == "canceled"

    late = payload_for(org, person, status="Approved", event_id="evt-late")
    await kyc.handle_didit_event(session, didit_settings(), late)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "canceled"
    assert refreshed.last_error is not None
    assert refreshed.last_error.startswith("identity_expired")


async def test_old_session_expiry_after_fresh_session_is_ignored(session):
    """An Expired for an OLD session must not touch a person who has started a new one."""
    org, person = await seed_person(
        session,
        status="verified",
        identity_hash=kyc.identity_hash("Dan", "Owner", "1980-04-02"),
        profile_status="approved",
    )
    # The person has started a fresh Didit session; the old one expires afterwards.
    person.provider_session_id = "sess-new"
    await session.flush()

    stale = payload_for(org, person, status="Kyc Expired", session_id="sess-old")
    await kyc.handle_didit_event(session, didit_settings(), stale)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "verified"
    assert refreshed.identity_hash == kyc.identity_hash("Dan", "Owner", "1980-04-02")

    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org.id))
    ).scalar_one()
    assert profile.status == "approved"


async def test_expiry_retains_hash_and_allows_new_session(session, monkeypatch):
    """After expiry the person can start a new Didit session; a new Approved identity
    stays needs_info until an operator approves."""
    from app.services import identity_provider

    original_hash = kyc.identity_hash("Dan", "Owner", "1980-04-02")
    org, person = await seed_person(
        session,
        status="verified",
        identity_hash=original_hash,
        profile_status="approved",
    )
    expired = payload_for(org, person, status="Kyc Expired")
    await kyc.handle_didit_event(session, didit_settings(), expired)
    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "canceled"
    assert refreshed.identity_hash == original_hash

    started: list[dict] = []

    class _Provider:
        async def start(self, settings, *, org_id, person_id, email, return_url):
            started.append({"org_id": org_id, "person_id": person_id})
            return identity_provider.StartedVerification(
                provider="didit", session_id="sess-retry", url="https://verify.didit.me/retry"
            )

    monkeypatch.setattr(identity_provider, "get_provider", lambda settings: _Provider())

    url = await kyc.start_person_verification(
        session, didit_settings(), refreshed, return_url="https://app.example.test/kyc/return"
    )
    assert url == "https://verify.didit.me/retry"
    assert started and started[0]["person_id"] == person.id

    # A new Approved identity on the fresh session verifies the person, but the profile
    # stays needs_info until an operator approves it.
    approved = payload_for(
        org, refreshed, status="Approved", session_id="sess-retry", event_id="evt-new"
    )
    approved["decision"] = {
        "status": "Approved",
        "id_verifications": [
            {
                "first_name": "Dan",
                "last_name": "Owner",
                "date_of_birth": "1980-04-02",
                "document_type": "Passport",
                "document_number": "P1",
                "issuing_state": "gb",
            }
        ],
    }
    await kyc.handle_didit_event(session, didit_settings(), approved)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "verified"
    assert refreshed.identity_hash == original_hash
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org.id))
    ).scalar_one()
    assert profile.status == "needs_info"


async def test_nonowner_expiry_does_not_invalidate_org_profile(session):
    """A non-owner's expiry must not drop an approved business profile to needs_info."""
    org, person = await seed_person(
        session,
        status="verified",
        identity_hash=kyc.identity_hash("Dan", "Owner", "1980-04-02"),
        profile_status="approved",
        role="admin",
    )

    payload = payload_for(org, person, status="Kyc Expired")
    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "canceled"

    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org.id))
    ).scalar_one()
    assert profile.status == "approved"


async def test_ordinary_expired_keeps_distinct_behavior(session):
    """Plain ``Expired`` (not ``Kyc Expired``) keeps the ordinary canceled mapping and
    does not set the identity_expired error."""
    org, person = await seed_person(session, status="pending")
    payload = payload_for(org, person, status="Expired")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "canceled"
    assert refreshed.last_error is None


# ------------------------------------------------------------------------------------
# 6. Resubmitted / Declined / In Review mapping (exact string case)
# ------------------------------------------------------------------------------------
async def test_resubmitted_maps_to_requires_input(session):
    """Didit uses Resubmitted when the reviewer asked the user to redo steps, so the
    person must land in requires_input to expose the retry affordance."""
    org, person = await seed_person(session, status="processing")
    payload = payload_for(org, person, status="Resubmitted")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "requires_input"


async def test_in_review_maps_to_processing(session):
    org, person = await seed_person(session, status="pending")
    payload = payload_for(org, person, status="In Review")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "processing"


async def test_declined_maps_to_requires_input(session):
    org, person = await seed_person(session, status="processing")
    payload = payload_for(org, person, status="Declined")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "requires_input"


async def test_status_mapping_is_case_sensitive(session):
    org, person = await seed_person(session, status="pending")
    payload = payload_for(org, person, status="resubmitted")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "pending"
