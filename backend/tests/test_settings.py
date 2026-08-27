from __future__ import annotations

import pytest
import structlog

from app.config import Settings
from app.errors import ConfigurationError
from app.main import _log_provider_report
from tests.conftest import make_settings

SENTINEL = "sentinel-hunter2-XYZZY"


def test_boot_fails_without_jwt_secret():
    with pytest.raises(ConfigurationError) as exc:
        Settings(jwt_secret="", session_secret="x", _env_file=None)
    assert "JWT_SECRET" in str(exc.value)


def test_boot_aggregates_all_problems():
    with pytest.raises(ConfigurationError) as exc:
        Settings(jwt_secret="", session_secret="", _env_file=None)
    msg = str(exc.value)
    assert "JWT_SECRET" in msg and "SESSION_SECRET" in msg


def test_production_rejects_placeholder_public_url():
    with pytest.raises(ConfigurationError) as exc:
        Settings(
            app_env="production",
            jwt_secret="x",
            session_secret="y",
            credential_encryption_key="not-a-fernet-key",
            public_base_url="http://your-tunnel-or-domain.example.com",
            _env_file=None,
        )
    msg = str(exc.value)
    assert "PUBLIC_BASE_URL" in msg
    assert "CREDENTIAL_ENCRYPTION_KEY" in msg


def test_provider_disabled_reason_names_missing_vars():
    s = make_settings(bandwidth_enabled=True, bandwidth_account_id="")
    status = next(p for p in s.provider_statuses() if p.name == "bandwidth")
    assert status.enabled is False
    assert "BANDWIDTH_ACCOUNT_ID" in status.missing
    assert "BANDWIDTH_ACCOUNT_ID" in status.reason


def test_provider_enabled_when_complete():
    s = make_settings(
        bandwidth_enabled=True,
        bandwidth_account_id="acct",
        bandwidth_api_username="user",
        bandwidth_api_password="pw",
    )
    status = next(p for p in s.provider_statuses() if p.name == "bandwidth")
    assert status.enabled is True
    assert status.missing == []


def test_disabled_flag_reported_as_such():
    s = make_settings(bandwidth_enabled=False)
    status = next(p for p in s.provider_statuses() if p.name == "bandwidth")
    assert status.enabled is False
    assert "ENABLED is false" in status.reason


def test_secrets_are_redacted_by_type():
    s = make_settings(bandwidth_api_password=SENTINEL)
    assert SENTINEL not in repr(s)
    assert str(s.bandwidth_api_password) == "**********"
    # The value is still retrievable deliberately and explicitly.
    assert s.bandwidth_api_password.get_secret_value() == SENTINEL


def test_secrets_never_logged():
    """Plant a sentinel secret, run the boot report, assert ZERO leakage."""
    captured: list[dict] = []

    def capture(_logger, _method, event_dict):
        captured.append(dict(event_dict))
        return event_dict

    old = structlog.get_config()["processors"]
    structlog.configure(processors=[capture, *old])
    try:
        s = make_settings(
            bandwidth_enabled=True,
            bandwidth_account_id="acct",
            bandwidth_api_username="user",
            bandwidth_api_password=SENTINEL,
            deepgram_api_key=SENTINEL,
            anthropic_api_key=SENTINEL,
        )
        _log_provider_report(s)
    finally:
        structlog.configure(processors=old)

    assert captured, "provider report produced no log lines"
    blob = repr(captured)
    assert SENTINEL not in blob
    assert any(e.get("provider") == "bandwidth" for e in captured)


# ==================================================================================
# phase-9b DR-1: keys are the switch
# ==================================================================================
def _base(**kw):
    return dict(jwt_secret="x" * 32, session_secret="y" * 32, _env_file=None, **kw)


def test_credentials_alone_bring_a_carrier_live():
    """The whole point: paste a key, restart, it works. No second flag to remember."""
    s = Settings(**_base(twilio_account_sid="ACxxx", twilio_auth_token="tok"))
    assert s.carrier_live("twilio") is True
    assert s.carrier_flag("twilio") is None, "unset flag means AUTO, not False"


def test_explicit_false_is_a_kill_switch_that_beats_present_keys():
    """The one thing the flag still says that the keys cannot: 'not this one, for now.'"""
    s = Settings(
        **_base(twilio_account_sid="ACxxx", twilio_auth_token="tok", twilio_enabled=False)
    )
    assert s.carrier_live("twilio") is False


def test_flag_true_without_keys_names_the_missing_variables():
    """An operator who set the flag and nothing else must be told exactly what is absent -
    this is the failure mode the old two-step design left silent."""
    s = Settings(**_base(plivo_enabled=True))
    status = {p.name: p for p in s.provider_statuses()}["plivo"]
    assert status.enabled is False
    assert set(status.missing) == {"PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"}
    assert "PLIVO_AUTH_ID" in status.reason


def test_unconfigured_carrier_says_how_to_enable_it():
    s = Settings(**_base())
    status = {p.name: p for p in s.provider_statuses()}["twilio"]
    assert status.enabled is False
    assert "add TWILIO_ACCOUNT_SID" in status.reason


def test_every_supported_carrier_is_listed_even_when_dark():
    """The console renders this list; a carrier missing from it is a carrier the operator
    never learns they could switch on."""
    names = {p.name for p in Settings(**_base()).provider_statuses()}
    assert {"bandwidth", "telnyx", "twilio", "plivo", "signalwire"} <= names
