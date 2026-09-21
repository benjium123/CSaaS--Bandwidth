"""The admin login endpoint must share the public login ceiling, not add a second one.

`/auth/admin/login` and `/auth/login` are the same credential check against the same accounts.
If each URL gets its own IP bucket, an attacker simply alternates between them and doubles the
attempts available per window - the ceiling is only real if both paths resolve to one counter.

These tests drive the real application through httpx's ASGI transport and assert on the
response status only, so they pin the observable behaviour (401 then 429) rather than the key
shape inside the limiter.
"""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from app.rate_limit import _limiter
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _clean_limiter():
    """The limiter is a module-level singleton, so one test's counters are the next test's
    starting state. Cleared here rather than in each test: a leaked bucket makes a later test
    fail for a reason that has nothing to do with what it asserts."""
    _limiter._events.clear()
    yield
    _limiter._events.clear()


def _settings(**over):
    # No REDIS_URL, so every bucket resolves through the in-process limiter. That is the point:
    # these tests pin the ceiling arithmetic, and the Redis path shares the same call signature.
    over.setdefault("rate_limit_enabled", True)
    over.setdefault("rate_limit_max_requests", 1)
    over.setdefault("rate_limit_window_seconds", 60)
    return make_settings(**over)


async def _post(
    app,
    path: str,
    *,
    email: str,
    password: str = "correct-horse-battery",
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json={"email": email, "password": password})


async def test_admin_login_shares_the_public_login_ceiling(engine):
    """First public login consumes the IP budget; the admin login from the same IP is refused.

    The email is unknown and the password is a valid length, so the first request fails on
    credentials (401) rather than on validation - which is what makes the second request's 429
    attributable to the shared ceiling and not to a malformed payload.
    """
    settings = _settings()
    app = create_app(settings)

    email = "nobody@example.com"
    password = "correct-horse-battery"

    first = await _post(app, "/api/v1/auth/login", email=email, password=password)
    assert first.status_code == 401

    second = await _post(app, "/api/v1/auth/admin/login", email=email, password=password)
    assert second.status_code == 429


async def test_public_login_is_refused_after_admin_login_consumes_the_budget(engine):
    """The reverse order, to prove the sharing is symmetric and not an artefact of ordering.

    A fresh limiter is used (the autouse fixture clears it, and this test does not rely on the
    previous test's state) so the only thing under test is that the admin path's consumption is
    visible to the public path.
    """
    settings = _settings()
    app = create_app(settings)

    email = "nobody@example.com"
    password = "correct-horse-battery"

    first = await _post(app, "/api/v1/auth/admin/login", email=email, password=password)
    assert first.status_code == 401

    second = await _post(app, "/api/v1/auth/login", email=email, password=password)
    assert second.status_code == 429
