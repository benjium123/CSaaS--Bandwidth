"""Adversarial audit (session voip-62): can the login endpoint be used to discover which
email addresses have accounts?

routes/auth.py login goes to real trouble to prevent this: an unknown email still pays for
an argon2 hash ("Verifying against a throwaway hash when the user is missing keeps the
timing profile similar, so login cannot be used to enumerate which emails have accounts"),
and unknown-email, bad-password and disabled-account all answer with the same
401 "Incorrect email or password".

This test checks that property survives the P42 lockout, which answers differently.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_settings

PASSWORD = "correct-horse-battery-staple"
WRONG = "not-the-right-password-at-all"


@pytest.fixture
def lockout_settings():
    # Thresholds left at the production defaults (10 failures / 15 min) -- the point is the
    # shipped behaviour. Rate limiting stays off, as everywhere else in this suite: it only
    # slows an attacker down, it does not change what the response reveals.
    return make_settings()


@pytest.fixture
async def app_client(engine, lockout_settings):
    import httpx

    from app.main import create_app

    application = create_app(lockout_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _hammer(client, email: str, attempts: int) -> list[int]:
    """Fail `attempts` logins for `email`; return the status code of each."""
    codes = []
    for _ in range(attempts):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": WRONG})
        codes.append(r.status_code)
    return codes


# ======================================================================================
# The oracle: an account that EXISTS eventually answers 423 account_locked
# ("Too many failed attempts. Try again in N minutes..."), while an address with no
# account answers 401 "Incorrect email or password" forever, because the lockout counter
# is keyed on user_id (services/lockout.py register_failure counts
# LoginEvent.user_id == user.id) and an unknown email has no user row to count against.
#
# So an attacker does not need timing at all: send `lockout_threshold` bad passwords per
# candidate address and read the status code. 423 means the address has an account, 401
# means it does not. That is a reliable, scriptable account-existence oracle for any email,
# and it defeats the throwaway-hash and identical-message work in the same handler.
#
# It also has a real-world side effect: every discovered account is locked out and its
# owner is emailed "Your account was temporarily locked" (lockout.fail), so enumerating a
# list both harvests valid addresses and denies service to all of them.
# ======================================================================================
async def test_lockout_does_not_reveal_which_emails_have_accounts(app_client, session):
    real = "real-person@example.com"
    r = await app_client.post(
        "/api/v1/auth/register", json={"email": real, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text

    ghost = "no-such-person@example.com"

    attempts = 12  # threshold is 10
    real_codes = await _hammer(app_client, real, attempts)
    ghost_codes = await _hammer(app_client, ghost, attempts)

    assert set(real_codes) == set(ghost_codes), (
        "login distinguishes a real account from an unknown address once the lockout "
        f"threshold is crossed: existing={real_codes} unknown={ghost_codes}. "
        "423 means 'this address has an account', 401 means it does not."
    )


async def test_control_a_single_bad_password_is_indistinguishable(app_client, session):
    """Control: BELOW the lockout threshold the handler's anti-enumeration work holds, so
    the failure above is specifically the lockout, not the login path in general."""
    real = "real-two@example.com"
    r = await app_client.post(
        "/api/v1/auth/register", json={"email": real, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text

    a = await app_client.post(
        "/api/v1/auth/login", json={"email": real, "password": WRONG}
    )
    b = await app_client.post(
        "/api/v1/auth/login", json={"email": "ghost-two@example.com", "password": WRONG}
    )
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["message"] == b.json()["error"]["message"]
    assert a.json()["error"]["code"] == b.json()["error"]["code"]


# ======================================================================================
# RESIDUAL ORACLE in the Finding 6 fix: the lock ESCALATION diverges on the second round.
#
# A real account's escalation is stateful and persists for 24h: AccountLockout.level is
# reset only when the last lock was more than 24 hours ago (lockout.py register_failure),
# so round 2 gives base*2 = 30 minutes.
#
# The synthetic lock for an unknown address is derived purely from LoginEvents inside the
# `lockout_window_minutes` window (15 min), so by round 2 the round-1 failures have aged
# out, `locks` is 1 again, and the duration is back to base = 15 minutes.
#
# So an attacker who is willing to wait out one lock reads the account's existence off the
# "Try again in N minutes" text: ~31 for a real address, ~16 for a made-up one.
# ======================================================================================
async def test_lock_escalation_does_not_reveal_account_existence_on_round_two(session):
    from datetime import datetime, timedelta, timezone

    from app.auth.security import hash_password
    from app.models import LoginEvent, User
    from app.services import lockout

    settings = make_settings()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=40)
    t1 = datetime.now(timezone.utc)

    alice = User(
        id=__import__("uuid").uuid4(),
        email="round-two@example.com",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    session.add(alice)
    await session.commit()

    def failures(at, *, user_id, email):
        for _ in range(settings.lockout_threshold):
            session.add(
                LoginEvent(
                    id=__import__("uuid").uuid4(),
                    user_id=user_id,
                    org_id=None,
                    email=email,
                    at=at,
                    outcome="bad_password",
                )
            )

    # Round 1 for both, 40 minutes ago.
    failures(t0, user_id=alice.id, email=alice.email)
    failures(t0, user_id=None, email="ghost-round-two@example.com")
    await session.commit()
    assert await lockout.register_failure(session, settings, alice, None, now=t0) is True
    await session.commit()

    # Round 2 for both, now.
    failures(t1, user_id=alice.id, email=alice.email)
    failures(t1, user_id=None, email="ghost-round-two@example.com")
    await session.commit()
    assert await lockout.register_failure(session, settings, alice, None, now=t1) is True
    await session.commit()

    real_until = await lockout.locked_until(session, alice.id)
    real_minutes = max(1, int((real_until - t1).total_seconds() // 60) + 1)

    try:
        await lockout.ensure_unknown_email_not_locked(
            session, settings, "ghost-round-two@example.com", now=t1
        )
    except lockout.AccountLockedError as exc:
        ghost_minutes = int("".join(ch for ch in str(exc) if ch.isdigit()))
    else:
        raise AssertionError("the unknown address was not locked at all on round two")

    assert ghost_minutes == real_minutes, (
        "the lock DURATION still distinguishes a real account from an unknown address on "
        f"the second round: real says {real_minutes} minutes, unknown says {ghost_minutes}. "
        "A real account's AccountLockout.level persists for 24h and doubles, while the "
        "synthetic lock is derived only from the 15-minute failure window, so it resets."
    )
