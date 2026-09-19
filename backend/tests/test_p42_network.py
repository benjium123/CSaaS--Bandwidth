"""P42 slice 3: one trusted-proxy-aware client IP rule."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.requests import Request

from tests.conftest import make_settings


def _request(xff: str | None, peer: str = "172.18.0.5", trusted: int = 1) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    app = SimpleNamespace(
        state=SimpleNamespace(settings=make_settings(trusted_proxy_count=trusted))
    )
    return Request({"type": "http", "headers": headers, "client": (peer, 1234), "app": app})


def test_spoofed_leading_entries_are_ignored():
    from app.net import client_ip

    # Caller sends a fake allowlisted address; nginx appends the real peer.
    assert client_ip(_request("10.1.2.3, 198.51.100.77")) == "198.51.100.77"
    assert client_ip(_request("198.51.100.77")) == "198.51.100.77"


def test_two_trusted_proxies():
    from app.net import client_ip

    assert client_ip(_request("6.6.6.6, 203.0.113.9, 10.0.0.2", trusted=2)) == "203.0.113.9"


def test_no_trusted_proxy_uses_socket_peer():
    from app.net import client_ip

    assert client_ip(_request("203.0.113.9", trusted=0)) == "172.18.0.5"
    assert client_ip(_request(None)) == "172.18.0.5"


def test_ip_allowlist_cannot_be_bypassed_with_a_fake_header():
    from app.services import identity as identity_svc

    request = _request("10.0.0.9, 198.51.100.77")
    assert identity_svc.ip_in_allowlist(identity_svc.client_ip(request), ["10.0.0.0/8"]) is False


def test_production_requires_redis():
    import pytest

    from app.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="REDIS_URL"):
        make_settings(app_env="production", redis_url="")
