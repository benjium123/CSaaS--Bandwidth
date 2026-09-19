"""Registration has its own ceiling, because public signup made it the front door.

Under invite-only the request limit on `/auth/register` was a formality: a valid invite token
was the real limit, and 20 requests a minute per IP was never the thing standing between an
attacker and an account. Public self-serve signup removes the token and leaves the rate limit
holding the door alone, at which point 20/minute - 1,200 an hour from one address - is an
API-shaped number doing an account-creation job.

Every test here pins a property that could regress silently, and two of them exist because the
obvious assertion would have passed against the bug:
  - the IP key must bind even though the attacker chooses the email (so a test that varies the
    email must still be stopped);
  - other routes must keep the OLD key shape, or a deploy resets every live counter;
  - the two registration windows must not share a counter.
"""

from __future__ import annotations

import pytest

from app.errors import RateLimitExceededError
from app.rate_limit import _limiter, _route_ip_buckets, enforce_rate_limit
from tests.conftest import make_settings


class _FakeApp:
    def __init__(self, settings):
        self.state = type("S", (), {"settings": settings})()


class _FakeRequest:
    """Only what the code under test touches: settings, method, path, and enough for client_ip.

    `scope` is here because `app.net.client_ip` reads `request.scope["app"]` to find the
    trusted-proxy count - a fake without it fails on an attribute rather than on the assertion,
    which tells you nothing about the ceiling. Both `x-forwarded-for` and `client.host` are set
    to the same address so the test does not depend on which branch of client_ip wins.
    """

    def __init__(self, settings, *, path="/api/v1/auth/register", ip="203.0.113.7"):
        self.app = _FakeApp(settings)
        self.scope = {"app": self.app}
        self.method = "POST"
        self.url = type("U", (), {"path": path})()
        self.headers = {"x-forwarded-for": ip}
        self.client = type("C", (), {"host": ip})()
        self.state = type("S", (), {})()


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
    # setdefault, not a keyword argument: conftest disables rate limiting for the whole suite, so
    # every test here has to turn it back on - but one test turns it off again deliberately, and
    # passing it both ways is a duplicate-keyword TypeError rather than an override.
    over.setdefault("rate_limit_enabled", True)
    return make_settings(**over)


async def test_registration_is_capped_far_below_the_global_default():
    """The global default would allow 20 in a minute. Registration allows five."""
    settings = _settings()
    assert settings.rate_limit_max_requests == 20, "the default this test contrasts with"

    for n in range(settings.registration_ip_burst_max):
        await enforce_rate_limit(_FakeRequest(settings), f"register:person{n}@example.com")

    with pytest.raises(RateLimitExceededError):
        await enforce_rate_limit(_FakeRequest(settings), "register:one-too-many@example.com")


async def test_varying_the_email_does_not_evade_the_limit():
    """The identifier at this route is `register:{email}` - a value the ATTACKER chooses. A limit
    keyed only on the identifier would never bind here, because every request carries a fresh
    one. This is why the IP key is the one doing the work, and why a test that reuses one email
    would pass without proving anything."""
    settings = _settings()
    for n in range(settings.registration_ip_burst_max):
        await enforce_rate_limit(_FakeRequest(settings), f"register:unique{n}@example.com")

    with pytest.raises(RateLimitExceededError) as excinfo:
        await enforce_rate_limit(_FakeRequest(settings), "register:brand-new@example.com")
    assert excinfo.value.retry_after >= 1


async def test_a_different_address_is_not_punished_for_a_neighbours_flood():
    """The ceiling is per IP. One abusive source must not close signup for everyone else."""
    settings = _settings()
    for n in range(settings.registration_ip_burst_max):
        await enforce_rate_limit(
            _FakeRequest(settings, ip="198.51.100.1"), f"register:flood{n}@example.com"
        )
    with pytest.raises(RateLimitExceededError):
        await enforce_rate_limit(
            _FakeRequest(settings, ip="198.51.100.1"), "register:flood-more@example.com"
        )

    # A different address is unaffected.
    await enforce_rate_limit(
        _FakeRequest(settings, ip="198.51.100.2"), "register:innocent@example.com"
    )


async def test_the_hourly_ceiling_binds_after_the_burst_window_passes():
    """Two windows stop different attacks, and the slow one is the one a patient script meets.

    Rather than sleeping an hour, this drives the hourly bucket directly with the same key shape
    `enforce_rate_limit` builds - if that shape ever changes, this test stops testing the hourly
    bucket, which is why the shape is asserted rather than assumed.
    """
    settings = _settings()
    buckets = _route_ip_buckets(settings, "/api/v1/auth/register")
    assert buckets == (
        (settings.registration_ip_burst_max, settings.registration_ip_burst_seconds),
        (settings.registration_ip_hourly_max, settings.registration_ip_hourly_seconds),
    )

    hourly_key = (
        f"POST:/api/v1/auth/register:ip:203.0.113.7"
        f":{settings.registration_ip_hourly_seconds}"
    )
    for _ in range(settings.registration_ip_hourly_max):
        assert (
            _limiter.allow(
                hourly_key,
                settings.registration_ip_hourly_max,
                settings.registration_ip_hourly_seconds,
            )
            == 0.0
        )

    # The hourly bucket is now full, so the next real request is refused even though the burst
    # window is empty.
    with pytest.raises(RateLimitExceededError):
        await enforce_rate_limit(_FakeRequest(settings), "register:patient@example.com")


async def test_the_two_registration_windows_do_not_share_a_counter():
    """The window is part of the key for exactly this reason. Sharing one counter would mean the
    5/60 and 20/3600 buckets consumed the same sliding window, and whichever was checked first
    would decide both answers - making the hourly ceiling unreachable and the burst ceiling the
    only real limit."""
    settings = _settings()
    await enforce_rate_limit(_FakeRequest(settings), "register:first@example.com")

    keys = set(_limiter._events)
    assert (
        f"POST:/api/v1/auth/register:ip:203.0.113.7:{settings.registration_ip_burst_seconds}"
        in keys
    )
    assert (
        f"POST:/api/v1/auth/register:ip:203.0.113.7:{settings.registration_ip_hourly_seconds}"
        in keys
    )


async def test_other_routes_keep_the_original_key_shape_and_ceiling():
    """A deploy must not reset every live counter, and no other route's ceiling may move.

    The un-suffixed `:ip:{ip}` key is what every other route has been using; adding the window
    suffix everywhere would have silently cleared the limiter on rollout and, worse, would have
    been invisible in testing because a cleared limiter simply allows more.
    """
    settings = _settings()
    await enforce_rate_limit(_FakeRequest(settings, path="/api/v1/auth/login"), "login:a@b.com")
    assert "POST:/api/v1/auth/login:ip:203.0.113.7" in _limiter._events

    # And login still gets the global ceiling, not registration's.
    for n in range(settings.rate_limit_max_requests - 1):
        await enforce_rate_limit(
            _FakeRequest(settings, path="/api/v1/auth/login"), f"login:a{n}@b.com"
        )
    with pytest.raises(RateLimitExceededError):
        await enforce_rate_limit(
            _FakeRequest(settings, path="/api/v1/auth/login"), "login:last@b.com"
        )


async def test_disabling_rate_limits_still_disables_them():
    """The kill switch must not be broken by the new path - a ceiling nobody can turn off is its
    own kind of outage."""
    settings = _settings(rate_limit_enabled=False)
    for n in range(50):
        await enforce_rate_limit(_FakeRequest(settings), f"register:{n}@example.com")
    assert not _limiter._events


def test_the_route_the_ceiling_is_keyed_to_still_exists():
    """The ceiling is looked up by an exact path string, which fails OPEN if the path changes.

    Rename or move `/auth/register` and `_route_ip_buckets` silently stops matching, so
    registration quietly reverts to the global 20/minute with nothing going red - the tests above
    would all keep passing, because they build their own request objects and never consult the
    application's routing table. This is the one assertion that connects the two, and it is the
    reason the lookup can be a path string at all rather than something wired into the route.
    """
    from app.main import create_app

    # Via the OpenAPI schema, NOT `app.routes`. This application's routers are included lazily:
    # `app.routes` holds 47 opaque `_IncludedRouter` wrappers whose `.path` is None, so a filter
    # over it finds nothing and an `assert not found` written against it would pass for the wrong
    # reason forever. `openapi()` forces full expansion and is the published contract besides.
    paths = create_app(make_settings()).openapi()["paths"]
    assert "/api/v1/auth/register" in paths, (
        "POST /api/v1/auth/register is gone or moved, so the registration rate ceiling in "
        "app/rate_limit.py no longer applies to anything. Update _route_ip_buckets."
    )
    assert "post" in paths["/api/v1/auth/register"], "register is no longer a POST"

    # And the override genuinely resolves for that exact path, not merely for a string that
    # happens to look like it.
    settings = _settings()
    assert _route_ip_buckets(settings, "/api/v1/auth/register") == (
        (settings.registration_ip_burst_max, settings.registration_ip_burst_seconds),
        (settings.registration_ip_hourly_max, settings.registration_ip_hourly_seconds),
    )
