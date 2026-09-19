"""Open registration is permitted in production only alongside the controls an invite provided.

WHY THIS FILE EXISTS. `ALLOW_OPEN_REGISTRATION` used to refuse the production boot outright.
That was correct while the product was invite-only, and simply deleting it now that public
self-serve signup is the business model would lose something specific: the refusal was not
config hygiene, it was the thing that made the flag safe to leave in the codebase. Deleted, the
only surviving record of the decision is an env var somebody flipped once, with no statement
anywhere of what the flip took away.

An invite token did three jobs beyond admitting someone. It proved a trusted person chose to
invite this one; it proved the address was reachable, because the token arrived there; and it
capped account creation at the rate humans issue invitations, which is why the request rate
limit was a formality. Public signup removes all three at once.

EVERY TEST HERE COMES IN A PAIR: one proving the guard trips, one proving it can be satisfied.
A boot check that can only refuse is as useless as one that can only pass, and the second half
of each pair is the half that would have caught me if the guard had been unconditional.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.errors import ConfigurationError


def _prod(**overrides) -> dict:
    """A production configuration that is otherwise valid, so the only thing under test is the
    open-registration rule. Every value here exists to satisfy a DIFFERENT production check -
    if one is missing the boot fails for that reason instead and the test passes vacuously,
    which is why `test_the_baseline_actually_boots` exists below."""
    base = {
        "app_env": "production",
        "jwt_secret": "x" * 32,
        "session_secret": "y" * 32,
        "public_base_url": "https://app.realco.test",
        "public_web_url": "https://app.realco.test",
        "credential_encryption_key": Fernet.generate_key().decode(),
        "credentials_master_key": "z" * 32,
        "database_url": "postgresql+asyncpg://user:pass@db/csaas",
        "cors_origins": "https://app.realco.test",
        "redis_url": "redis://localhost:6379/0",
        "require_2fa_privileged_users": True,
    }
    base.update(overrides)
    return base


def _boot(**overrides) -> Settings:
    # _env_file=None so a developer's own .env cannot decide the outcome of a test about
    # production configuration - the exact leak that made two other test files inherit a live
    # API key and a real Redis URL.
    return Settings(**_prod(**overrides), _env_file=None)


def test_the_baseline_actually_boots():
    """The control for every other test in this file.

    Without it, each test below could be passing because of some unrelated production rule -
    a missing Fernet key, a placeholder URL - and would keep passing if the open-registration
    guard were deleted entirely. This is the assertion that makes the others mean something.
    """
    settings = _boot(allow_open_registration=False)
    assert settings.app_env == "production"
    assert settings.allow_open_registration is False


def test_invite_only_production_is_unaffected():
    """The common case, and the one that must not have become harder. Nothing about turning
    open registration OFF should now require business-email checks or anything else."""
    settings = _boot(
        allow_open_registration=False, require_business_email=False, kyc_enforced=False
    )
    assert settings.allow_open_registration is False


def test_open_registration_boots_when_every_control_is_present():
    """The half that proves the guard is satisfiable.

    A guard that refuses every configuration is indistinguishable, from inside a test suite that
    only ever asserts refusals, from a guard that works. This is the pair to the three tests
    below, and it is the one that fails if someone converts the requirement back into a refusal.
    """
    settings = _boot(
        allow_open_registration=True,
        require_business_email=True,
        kyc_enforced=True,
        redis_url="redis://localhost:6379/0",
    )
    assert settings.allow_open_registration is True
    assert settings.require_business_email is True


def test_open_registration_without_the_business_email_check_is_refused():
    """With no invite, the domain check is the only filter on who may create an account. It
    being switched off is a state reachable by flipping two flags on different days, and
    neither flag's own comment would tell you the combination is the dangerous one."""
    with pytest.raises(ConfigurationError) as excinfo:
        _boot(allow_open_registration=True, require_business_email=False)
    message = str(excinfo.value)
    assert "ALLOW_OPEN_REGISTRATION is on" in message
    assert "REQUIRE_BUSINESS_EMAIL must be true" in message


def test_open_registration_with_verification_disabled_is_refused():
    """Open registration plus KYC off is worse than either alone: anyone signs up and nothing
    gates telephony behind verification. The operator's rule that nothing may be purchased
    before approval is the control that actually holds, and KYC_ENFORCED is what enforces it -
    so the guard names it rather than assuming it."""
    with pytest.raises(ConfigurationError) as excinfo:
        _boot(allow_open_registration=True, kyc_enforced=False)
    assert "KYC_ENFORCED must be true" in str(excinfo.value)


def test_open_registration_without_shared_rate_limits_is_refused():
    """The failure this one exists for is silent in both directions.

    Without Redis every ceiling in `rate_limit.py` falls back to a per-process counter, so the
    5-per-minute registration limit becomes 5 per minute PER WORKER. Nothing goes red: a limiter
    with no shared state simply allows more. The deployed image ships `--workers 1`, so the
    multiplier is 1 today and the first person to raise it would weaken every limit on the
    public front door without a single signal.
    """
    with pytest.raises(ConfigurationError) as excinfo:
        _boot(allow_open_registration=True, redis_url="")
    message = str(excinfo.value)
    assert "REDIS_URL must be set" in message
    assert "multiplies" in message, "the message must say WHY, not just name the setting"


def test_the_refusal_lists_every_missing_control_at_once():
    """An operator turning on public signup on a fresh deployment is missing several things.
    Reporting them one per boot attempt turns one configuration task into four deploys, and
    the last three would each look like a new problem."""
    with pytest.raises(ConfigurationError) as excinfo:
        _boot(
            allow_open_registration=True,
            require_business_email=False,
            kyc_enforced=False,
            redis_url="",
        )
    message = str(excinfo.value)
    assert "REQUIRE_BUSINESS_EMAIL" in message
    assert "KYC_ENFORCED" in message
    assert "REDIS_URL must be set" in message


def test_development_is_not_gated_at_all():
    """None of this applies outside production. A developer running open registration locally
    with no Redis and KYC off is the normal case, and a guard that blocked it would be
    reintroduced-by-deletion within a week."""
    settings = Settings(
        app_env="test",
        jwt_secret="x" * 32,
        session_secret="y" * 32,
        allow_open_registration=True,
        require_business_email=False,
        kyc_enforced=False,
        redis_url="",
        _env_file=None,
    )
    assert settings.allow_open_registration is True
