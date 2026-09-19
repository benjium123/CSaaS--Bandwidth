"""P44 part 2: the Didit webhook endpoint, its authenticity/replay rules, the
``event_id`` idempotency ledger, and the mapping of Didit statuses onto KycPerson.

Covers:
  A. The webhook endpoint: a correctly signed payload is accepted (204) and moves the
     person; a corrupted signature and a stale timestamp are both refused (401) and
     leave the person untouched.
  B. Idempotency on ``event_id``: a duplicate delivery is suppressed by the ledger,
     not merely by the ``apply_person_outcome`` un-verify guard.
  C. Status mapping onto KycPerson, including the fail-closed re-verification rule.

No network call happens anywhere in this module. The endpoint is driven over
``httpx.ASGITransport`` (in-process ASGI, no socket), and the seam tests monkeypatch
``didit_client.create_session`` / ``stripe_client.create_verification_session`` with
recorders. httpx logs an identical ``HTTP Request:`` line for a mocked transport as
for a real one, so log output is never treated as evidence a call happened - the tests
assert on recorded calls and on database rows instead.
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
from app.models import IdentityWebhookEvent, KycPerson, KycProfile, Org
from app.services import didit_client, kyc
from tests.conftest import make_settings

DIDIT_KEY = "didit-test-api-key"
DIDIT_SECRET = "didit-test-webhook-secret"
WORKFLOW_ID = "11111111-1111-1111-1111-111111111111"


def didit_settings(**overrides):
    base = {
        "kyc_identity_provider": "didit",
        "didit_api_key": DIDIT_KEY,
        "didit_workflow_id": WORKFLOW_ID,
        "didit_webhook_secret": DIDIT_SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


# ----------------------------------------------------------------------------------
# Signing helpers
# ----------------------------------------------------------------------------------
def signed(payload: dict, *, secret: str = DIDIT_SECRET, timestamp: int | None = None):
    """Returns (raw_body_bytes, headers) with a valid X-Signature-V2 and X-Timestamp.

    The signature is computed with the implementation's own ``canonical_payload`` so
    the test cannot drift from the wire format Didit actually signs.
    """
    raw = json.dumps(payload).encode()
    ts = int(time.time()) if timestamp is None else timestamp
    sig = hmac.new(
        secret.encode(), didit_client.canonical_payload(payload), hashlib.sha256
    ).hexdigest()
    return raw, {"X-Timestamp": str(ts), "X-Signature-V2": sig}


async def post_webhook(client, payload, **kw):
    raw, headers = signed(payload, **kw)
    return await client.post("/api/v1/webhooks/didit", content=raw, headers=headers)


# ----------------------------------------------------------------------------------
# Database seeding
# ----------------------------------------------------------------------------------
async def seed_person(
    session,
    *,
    status="pending",
    provider="didit",
    session_id="sess-1",
    identity_hash=None,
    user_id=None,
    profile_status="draft",
):
    """Creates an Org + KycProfile + KycPerson and commits. Returns (org, person)."""
    org = Org(id=uuid.uuid4(), name="Webhook Org", slug=f"hook-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    set_org_context(session, org.id)

    profile = KycProfile(id=uuid.uuid4(), org_id=org.id, status=profile_status)
    session.add(profile)

    person = KycPerson(
        id=uuid.uuid4(),
        org_id=org.id,
        role="owner",
        full_name="Dan Owner",
        email="dan@example.test",
        identity_provider=provider,
        provider_session_id=session_id,
        status=status,
        identity_hash=identity_hash,
        user_id=user_id,
    )
    session.add(person)
    await session.commit()
    return org, person


def payload_for(
    org,
    person,
    *,
    status="Approved",
    session_id=None,
    event_id="evt-1",
    vendor_data=None,
    metadata=None,
    webhook_type="session.completed",
):
    """Build a Didit-shaped webhook payload carrying our correlation metadata."""
    if metadata is None:
        metadata = {
            "purpose": "kyc_person",
            "org_id": str(org.id),
            "person_id": str(person.id),
        }
    return {
        "webhook_type": webhook_type,
        "timestamp": int(time.time()),
        "created_at": int(time.time()),
        "status": status,
        "session_id": session_id if session_id is not None else person.provider_session_id,
        "workflow_id": WORKFLOW_ID,
        "vendor_data": str(person.id) if vendor_data is None else vendor_data,
        "event_id": event_id,
        "metadata": metadata,
    }


async def reload_person(session, person_id):
    """Re-read a person from the database so we observe what the request committed.

    Only this row is refreshed: expiring the whole session would make the test's own
    ``person`` object reload lazily on the next attribute read, which is sync IO inside
    an async test. The org context is deliberately left in place - clearing it makes
    the cross-tenant write guard refuse the audit row the handler queued.
    """
    # handle_didit_event mutates the person in memory and leaves the commit to the route,
    # so the change has to be flushed before it can be re-read - session.refresh() replaces
    # the object's state from the database and would otherwise discard it.
    await session.flush()
    row = await session.get(KycPerson, person_id)
    await session.refresh(row)
    return row


# ----------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
async def didit_client_app(engine):
    from app.main import create_app

    application = create_app(didit_settings())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ==================================================================================
# A. The webhook endpoint: authenticity and replay
# ==================================================================================
async def test_signed_approved_verifies_person(session, didit_client_app):
    """A correctly signed Approved payload returns 204 and moves the person to
    verified."""
    org, person = await seed_person(session)
    payload = payload_for(org, person, status="Approved")

    r = await post_webhook(didit_client_app, payload)
    assert r.status_code == 204, r.text

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "verified"


async def test_corrupted_signature_is_refused(session, didit_client_app):
    """A corrupted signature must be refused with 401 AND leave the person unchanged.
    Asserting only the status code would let a route that 401s after mutating slip
    through."""
    org, person = await seed_person(session)
    payload = payload_for(org, person, status="Approved")
    raw, headers = signed(payload)
    headers["X-Signature-V2"] = "0" * len(headers["X-Signature-V2"])

    r = await didit_client_app.post(
        "/api/v1/webhooks/didit", content=raw, headers=headers
    )
    assert r.status_code == 401, r.text

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "pending"


async def test_stale_timestamp_is_refused(session, didit_client_app):
    """A correctly signed payload whose X-Timestamp is 400 seconds old is a replay:
    the signature is valid but the request has expired, so it must be refused with 401
    and the person must be unchanged."""
    org, person = await seed_person(session)
    payload = payload_for(org, person, status="Approved")

    r = await post_webhook(didit_client_app, payload, timestamp=int(time.time()) - 400)
    assert r.status_code == 401, r.text

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "pending"


# ==================================================================================
# B. Idempotency on event_id
# ==================================================================================
async def test_duplicate_event_id_is_suppressed_by_ledger(session, didit_client_app):
    """Delivering the same event_id twice must not re-apply the second decision, and
    exactly one ledger row must exist for it.

    The ledger's primary key is namespaced by provider (``"didit:<event_id>"``) so a
    Stripe event id and a Didit event id can never collide in one table.
    """
    org, person = await seed_person(session)

    first = payload_for(org, person, status="Approved", event_id="evt-A")
    r = await post_webhook(didit_client_app, first)
    assert r.status_code == 204, r.text
    assert (await reload_person(session, person.id)).status == "verified"

    # Same event id, different decision: the ledger must suppress it.
    second = payload_for(org, person, status="Declined", event_id="evt-A")
    r = await post_webhook(didit_client_app, second)
    assert r.status_code == 204, r.text
    assert (await reload_person(session, person.id)).status == "verified"

    rows = (
        await session.execute(
            sa.select(IdentityWebhookEvent)
            .where(IdentityWebhookEvent.id == "didit:evt-A")
            .execution_options(allow_unscoped=True)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider == "didit"


async def test_ledger_suppresses_duplicate_not_the_unverify_guard(
    session, didit_client_app
):
    """Test 4 alone cannot distinguish the ledger from apply_person_outcome's
    un-verify guard, because both would leave a verified person verified. This test
    uses a person in a NON-terminal state so the guard cannot be the reason:

      * evt-B with status "In Review" moves a fresh person to processing.
      * Re-delivering evt-B with status "Declined" must leave them at processing -
        only the ledger can explain that, since a Declined decision would otherwise
        move them to requires_input.
      * A NEW event id evt-C carrying Declined DOES move them to requires_input,
        proving the mapping is live and the suppression above was the ledger.
    """
    org, person = await seed_person(session)

    first = payload_for(org, person, status="In Review", event_id="evt-B")
    r = await post_webhook(didit_client_app, first)
    assert r.status_code == 204, r.text
    assert (await reload_person(session, person.id)).status == "processing"

    duplicate = payload_for(org, person, status="Declined", event_id="evt-B")
    r = await post_webhook(didit_client_app, duplicate)
    assert r.status_code == 204, r.text
    assert (await reload_person(session, person.id)).status == "processing"

    fresh = payload_for(org, person, status="Declined", event_id="evt-C")
    r = await post_webhook(didit_client_app, fresh)
    assert r.status_code == 204, r.text
    assert (await reload_person(session, person.id)).status == "requires_input"


# ==================================================================================
# C. Status mapping onto KycPerson
# ==================================================================================
@pytest.mark.parametrize(
    "didit_status,expected",
    [
        ("Approved", "verified"),
        ("Declined", "requires_input"),
        ("In Review", "processing"),
        ("In Progress", "processing"),
        ("Resubmitted", "processing"),
        ("Abandoned", "canceled"),
        ("Expired", "canceled"),
        ("Kyc Expired", "canceled"),
    ],
)
async def test_status_mapping(session, didit_status, expected):
    """Each Didit status maps onto the KycPerson status the KYC service understands.
    Driven through kyc.handle_didit_event directly - the endpoint path is covered
    above."""
    org, person = await seed_person(session, status="pending", identity_hash=None)
    payload = payload_for(org, person, status=didit_status)

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == expected


async def test_awaiting_user_sets_pending_from_not_started(session):
    """Awaiting User means the ball is in the user's court, so a person who has not
    started is moved to pending."""
    org, person = await seed_person(session, status="not_started")
    payload = payload_for(org, person, status="Awaiting User")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "pending"


async def test_awaiting_user_never_drags_processing_backwards(session):
    """Awaiting User must never move a person who is already processing back to
    pending - that would erase in-flight progress."""
    org, person = await seed_person(session, status="processing")
    payload = payload_for(org, person, status="Awaiting User")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "processing"


async def test_unknown_status_is_a_noop(session):
    """An unrecognised status string must not raise and must leave status and
    last_error byte-for-byte unchanged."""
    org, person = await seed_person(session, status="pending")
    before_status = person.status
    before_error = person.last_error

    payload = payload_for(org, person, status="Wibble")
    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == before_status
    assert refreshed.last_error == before_error


async def test_missing_status_is_a_noop(session):
    """A payload with no status at all must not raise and must leave the person
    unchanged."""
    org, person = await seed_person(session, status="pending")
    before_status = person.status
    before_error = person.last_error

    payload = payload_for(org, person)
    payload.pop("status")
    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == before_status
    assert refreshed.last_error == before_error


async def test_missing_org_id_in_metadata_is_a_noop(session):
    """A payload whose metadata.org_id is missing must not raise and must leave the
    person unchanged."""
    org, person = await seed_person(session, status="pending")
    before_status = person.status
    before_error = person.last_error

    payload = payload_for(
        org,
        person,
        status="Approved",
        metadata={"purpose": "kyc_person", "person_id": str(person.id)},
    )
    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == before_status
    assert refreshed.last_error == before_error


async def test_non_uuid_org_id_in_metadata_is_a_noop(session):
    """A payload whose metadata.org_id is not a UUID must not raise and must leave
    the person unchanged."""
    org, person = await seed_person(session, status="pending")
    before_status = person.status
    before_error = person.last_error

    payload = payload_for(
        org,
        person,
        status="Approved",
        metadata={
            "purpose": "kyc_person",
            "org_id": "not-a-uuid",
            "person_id": str(person.id),
        },
    )
    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == before_status
    assert refreshed.last_error == before_error


async def test_unknown_session_and_vendor_data_is_a_noop(session):
    """A payload whose session_id matches no person and whose vendor_data is not one
    of our ids must not raise and must not change any person anywhere.

    The metadata is deliberately VALID (real org_id and person_id) so the handler
    reaches the unknown-person branch rather than returning early at the bad-org
    branch - otherwise this test would pass for the wrong reason.
    """
    org, person = await seed_person(session, status="pending")
    before_status = person.status

    payload = payload_for(
        org,
        person,
        status="Approved",
        session_id="sess-does-not-exist",
        vendor_data="not-our-id",
    )
    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == before_status

    # No person anywhere changed.
    all_people = (
        await session.execute(
            sa.select(KycPerson).execution_options(allow_unscoped=True)
        )
    ).scalars().all()
    assert all(p.status == before_status for p in all_people)


async def test_reverification_without_identity_evidence_fails_closed(session):
    """A verified person with an identity_hash on a reverification_due profile who
    starts a new Didit check (status back to pending, identity_hash still set) and
    receives Approved must NOT become verified again.

    Didit's webhook carries no verified name or date of birth, so we cannot prove it
    is the same human. The check fails closed for an operator (processing +
    identity_unconfirmed) rather than accepting a possible stranger.
    """
    org, person = await seed_person(
        session,
        status="pending",
        identity_hash="deadbeef" * 8,
        profile_status="reverification_due",
    )
    payload = payload_for(org, person, status="Approved")

    await kyc.handle_didit_event(session, didit_settings(), payload)

    refreshed = await reload_person(session, person.id)
    assert refreshed.status == "processing"
    assert refreshed.last_error is not None
    assert refreshed.last_error.startswith("identity_unconfirmed")
