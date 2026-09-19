"""P44 part 2: the identity-provider seam and the security rule that must survive it.

Covers:
  D. start_person_verification dispatches to the configured provider - Didit when
     kyc_identity_provider="didit", Stripe (with exactly today's arguments) when it is
     "stripe" or unset, and ConfigurationError for an unknown value.
  E. The not_your_identity refusal is enforced BEFORE any provider I/O, and it is
     provider independent: it fires identically under Didit and Stripe.

No network call happens anywhere in this module. Every provider entry point
(didit_client.create_session, stripe_client.create_verification_session) is
monkeypatched with a recorder, and the tests assert on the recorded calls rather than
on log output - httpx logs an identical "HTTP Request:" line for a mocked transport as
for a real one, so logs are not evidence that a call happened.
"""

from __future__ import annotations

import uuid

import pytest

from app.errors import ConfigurationError, PermissionDeniedError
from app.models import KycPerson, KycProfile, Org, User
from app.db.base import set_org_context
from app.services import didit_client, identity_provider, stripe_client
from app.services.kyc import start_person_verification
from tests.conftest import make_settings

DIDIT_KEY = "didit-test-api-key"
DIDIT_SECRET = "didit-test-webhook-secret"
WORKFLOW_ID = "11111111-1111-1111-1111-111111111111"

RETURN_URL = "https://app.example.test/kyc/return"


def didit_settings(**overrides):
    base = {
        "kyc_identity_provider": "didit",
        "didit_api_key": DIDIT_KEY,
        "didit_workflow_id": WORKFLOW_ID,
        "didit_webhook_secret": DIDIT_SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


class Recorder:
    """Records every call it receives and returns a scripted result.

    Tests assert on ``calls`` rather than on log output: a mocked transport logs the
    same HTTP line as a real one, so the only honest evidence a provider was (or was
    not) touched is the recorded call list.
    """

    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {}
        self.calls: list[dict] = []

    async def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        return self.result


class Exploding:
    """A provider entry point that fails loudly if it is ever called.

    Used to prove the OTHER provider is not touched - a silent no-op would let a bug
    through, an AssertionError will not.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    async def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError(f"{self.name} must not be called")


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
    org = Org(id=uuid.uuid4(), name="Seam Org", slug=f"seam-{uuid.uuid4().hex[:8]}")
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


async def seed_user(session, *, email=None) -> User:
    """A real User row - KycPerson.user_id is a FK to users.id, so a random UUID would
    violate the constraint on Postgres (and on SQLite with foreign_keys=ON)."""
    user = User(
        id=uuid.uuid4(),
        email=email or f"user-{uuid.uuid4().hex[:8]}@example.test",
        hashed_password="x",
        full_name="Dan Owner",
    )
    session.add(user)
    await session.commit()
    return user


# ==================================================================================
# D. The provider seam
# ==================================================================================


async def test_didit_provider_is_used_when_configured(session, monkeypatch):
    """With kyc_identity_provider="didit", start_person_verification must call Didit
    with our person id as vendor_data and the org/person metadata, and must NOT touch
    Stripe."""
    org, person = await seed_person(session)

    didit = Recorder(
        {
            "session_id": "sess-xyz",
            "url": "https://verify.didit.me/s/xyz",
            "status": "Not Started",
        }
    )
    stripe = Exploding("stripe_client.create_verification_session")
    monkeypatch.setattr(didit_client, "create_session", didit)
    monkeypatch.setattr(stripe_client, "create_verification_session", stripe)

    settings = didit_settings()
    result = await start_person_verification(
        session, settings, person, return_url=RETURN_URL, actor_user_id=None
    )

    assert result == "https://verify.didit.me/s/xyz"
    assert person.provider_session_id == "sess-xyz"
    assert person.identity_provider == "didit"
    assert person.stripe_verification_session_id is None
    assert person.status == "pending"

    assert len(didit.calls) == 1
    call = didit.calls[0]
    assert call["vendor_data"] == str(person.id)
    assert call["metadata"] == {
        "purpose": "kyc_person",
        "org_id": str(org.id),
        "person_id": str(person.id),
    }
    assert call["callback"] == RETURN_URL
    assert stripe.calls == []


async def test_stripe_provider_is_used_when_configured(session, monkeypatch):
    """With kyc_identity_provider="stripe", Stripe must be called with EXACTLY today's
    arguments - metadata and return_url, and no email kwarg - and Didit must not be
    touched."""
    org, person = await seed_person(session, provider="stripe", session_id="sess-1")

    stripe = Recorder({"id": "vs_123", "url": "https://verify.stripe.test/vs_123"})
    didit = Exploding("didit_client.create_session")
    monkeypatch.setattr(stripe_client, "create_verification_session", stripe)
    monkeypatch.setattr(didit_client, "create_session", didit)

    settings = make_settings(kyc_identity_provider="stripe")
    result = await start_person_verification(
        session, settings, person, return_url=RETURN_URL, actor_user_id=None
    )

    assert result == "https://verify.stripe.test/vs_123"
    assert person.stripe_verification_session_id == "vs_123"
    assert person.provider_session_id == "vs_123"
    assert person.identity_provider == "stripe"
    assert person.status == "pending"

    assert len(stripe.calls) == 1
    call = stripe.calls[0]
    assert call["metadata"] == {
        "purpose": "kyc_person",
        "org_id": str(org.id),
        "person_id": str(person.id),
    }
    assert call["return_url"] == RETURN_URL
    # Stripe collects the person's details itself; forwarding an email would change the
    # request the incumbent provider receives.
    assert "email" not in call
    assert didit.calls == []


async def test_stripe_is_the_default_when_unset(session, monkeypatch):
    """make_settings() leaves kyc_identity_provider at its default, which must keep
    behaving exactly as it did before the seam existed: Stripe."""
    org, person = await seed_person(session, provider="stripe", session_id="sess-1")

    stripe = Recorder({"id": "vs_123", "url": "https://verify.stripe.test/vs_123"})
    didit = Exploding("didit_client.create_session")
    monkeypatch.setattr(stripe_client, "create_verification_session", stripe)
    monkeypatch.setattr(didit_client, "create_session", didit)

    settings = make_settings()
    result = await start_person_verification(
        session, settings, person, return_url=RETURN_URL, actor_user_id=None
    )

    assert result == "https://verify.stripe.test/vs_123"
    assert person.identity_provider == "stripe"
    assert person.stripe_verification_session_id == "vs_123"
    assert person.provider_session_id == "vs_123"
    assert person.status == "pending"

    assert len(stripe.calls) == 1
    assert stripe.calls[0]["return_url"] == RETURN_URL
    assert "email" not in stripe.calls[0]
    assert didit.calls == []


async def test_unknown_provider_raises_configuration_error(session, monkeypatch):
    """An unrecognised provider name is a configuration mistake, not a runtime
    condition: get_provider and start_person_verification must both refuse it."""
    org, person = await seed_person(session)

    didit = Exploding("didit_client.create_session")
    stripe = Exploding("stripe_client.create_verification_session")
    monkeypatch.setattr(didit_client, "create_session", didit)
    monkeypatch.setattr(stripe_client, "create_verification_session", stripe)

    settings = make_settings(kyc_identity_provider="wibble")

    with pytest.raises(ConfigurationError):
        identity_provider.get_provider(settings)

    with pytest.raises(ConfigurationError):
        await start_person_verification(
            session, settings, person, return_url=RETURN_URL, actor_user_id=None
        )

    assert didit.calls == []
    assert stripe.calls == []


# ==================================================================================
# E. The security rule survives the provider swap
# ==================================================================================


async def _seed_reverification(session):
    """A verified person with an identity_hash and a linked user, on a profile whose
    status is reverification_due - the exact shape the not_your_identity rule guards."""
    user = await seed_user(session)
    org, person = await seed_person(
        session,
        status="verified",
        identity_hash="a" * 64,
        user_id=user.id,
        profile_status="reverification_due",
    )
    return org, person, user


async def test_not_your_identity_refused_under_didit(session, monkeypatch):
    """A different actor re-verifying someone else's identity must be refused with
    not_your_identity, and the refusal must happen BEFORE any provider I/O."""
    org, person, user = await _seed_reverification(session)

    didit = Recorder({"session_id": "sess-xyz", "url": "https://verify.didit.me/s/xyz"})
    monkeypatch.setattr(didit_client, "create_session", didit)

    settings = didit_settings()
    with pytest.raises(PermissionDeniedError) as exc:
        await start_person_verification(
            session,
            settings,
            person,
            return_url=RETURN_URL,
            actor_user_id=uuid.uuid4(),
        )

    assert exc.value.code == "not_your_identity"
    assert didit.calls == []


async def test_not_your_identity_allows_the_owner_under_didit(session, monkeypatch):
    """The same setup with actor_user_id == person.user_id must succeed and call Didit
    once - without this the refusal test could pass for the wrong reason."""
    org, person, user = await _seed_reverification(session)

    didit = Recorder(
        {
            "session_id": "sess-xyz",
            "url": "https://verify.didit.me/s/xyz",
            "status": "Not Started",
        }
    )
    monkeypatch.setattr(didit_client, "create_session", didit)

    settings = didit_settings()
    result = await start_person_verification(
        session, settings, person, return_url=RETURN_URL, actor_user_id=user.id
    )

    assert result == "https://verify.didit.me/s/xyz"
    assert len(didit.calls) == 1
    assert person.provider_session_id == "sess-xyz"
    assert person.identity_provider == "didit"


async def test_not_your_identity_refused_under_stripe(session, monkeypatch):
    """The rule is provider independent: the same refusal fires under Stripe, and
    Stripe is never called."""
    org, person, user = await _seed_reverification(session)

    stripe = Recorder({"id": "vs_123", "url": "https://verify.stripe.test/vs_123"})
    monkeypatch.setattr(stripe_client, "create_verification_session", stripe)

    settings = make_settings(kyc_identity_provider="stripe")
    with pytest.raises(PermissionDeniedError) as exc:
        await start_person_verification(
            session,
            settings,
            person,
            return_url=RETURN_URL,
            actor_user_id=uuid.uuid4(),
        )

    assert exc.value.code == "not_your_identity"
    assert stripe.calls == []


# ==================================================================================
# F. End to end: a Didit-verified person can start a step-up
# ==================================================================================


async def test_didit_verified_person_can_start_a_step_up(session, monkeypatch):
    """After a Didit identity check, the verified hash must unlock the Stripe step-up."""
    from app.services import kyc, kyc_step_up

    user = await seed_user(session)
    org, person = await seed_person(session, status="pending", user_id=user.id)

    payload = {
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
                    "first_name": "Dan",
                    "last_name": "Owner",
                    "date_of_birth": "1980-04-02",
                }
            ],
        },
    }

    await kyc.handle_didit_event(session, didit_settings(), payload)
    await session.flush()

    assert person.identity_hash is not None
    assert await kyc_step_up.verified_identity_hashes(session, user.id)

    recorder = Recorder(
        {"id": "vs_1", "url": "https://stripe.example/x", "status": "requires_input"}
    )
    monkeypatch.setattr(stripe_client, "create_verification_session", recorder)

    row, url = await kyc_step_up.start(
        session, didit_settings(), user, action="api_key_create", return_url=RETURN_URL
    )

    assert url == "https://stripe.example/x"
    assert row.user_id == user.id


async def test_didit_person_without_identity_still_cannot_start_a_step_up(session, monkeypatch):
    """Without Didit identity evidence, step-up refuses with identity_not_verified
    before any Stripe I/O."""
    from app.errors import ValidationFailedError
    from app.services import kyc, kyc_step_up

    user = await seed_user(session)
    org, person = await seed_person(session, status="pending", user_id=user.id)

    payload = {
        "status": "Approved",
        "session_id": person.provider_session_id,
        "vendor_data": str(person.id),
        "metadata": {
            "purpose": "kyc_person",
            "org_id": str(org.id),
            "person_id": str(person.id),
        },
    }

    await kyc.handle_didit_event(session, didit_settings(), payload)
    await session.flush()
    assert person.identity_hash is None

    recorder = Recorder({"id": "vs_1", "url": "https://stripe.example/x"})
    monkeypatch.setattr(stripe_client, "create_verification_session", recorder)

    with pytest.raises(ValidationFailedError) as exc:
        await kyc_step_up.start(
            session, didit_settings(), user, action="api_key_create", return_url=RETURN_URL
        )

    assert exc.value.code == "identity_not_verified"
    assert recorder.calls == []
