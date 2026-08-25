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
