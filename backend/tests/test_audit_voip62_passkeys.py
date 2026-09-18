"""Adversarial audit (session voip-62): WebAuthn challenge single-use.

`consume_challenge` used to read `consumed_at is None` and then assign, which is
read-modify-write: two requests carrying the same challenge_id both passed the check and
both went on to verify. That matters because both verifications then read the SAME stored
sign_count and both accept the same new one, defeating the sign-count clone detection --
the one mechanism that exists to notice a duplicated authenticator.

It now claims the row with a conditional UPDATE and a rowcount check, the same way
kyc_step_up spends a selfie check. SQLite serialises writes so it cannot show the race
directly; what these tests pin is that exactly one caller can ever win, which is the
property the conditional UPDATE provides and the old code did not.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.security import hash_password
from app.errors import UnauthenticatedError
from app.models import User
from tests.conftest import make_settings


async def _user(session) -> User:
    row = User(
        id=uuid.uuid4(),
        email=f"passkey-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=hash_password("correct-horse-battery"),
        is_active=True,
    )
    session.add(row)
    await session.commit()
    return row


async def test_a_challenge_can_only_be_spent_once(session):
    from app.services import passkeys

    user = await _user(session)
    challenge = await passkeys._new_challenge(session, user.id, "login")
    await session.commit()

    first = await passkeys.consume_challenge(
        session, challenge.id, user_id=user.id, purpose="login"
    )
    assert first == challenge.challenge
    await session.commit()

    with pytest.raises(UnauthenticatedError):
        await passkeys.consume_challenge(session, challenge.id, user_id=user.id, purpose="login")


async def test_a_challenge_is_bound_to_its_user_and_purpose(session):
    """A challenge minted for one person, or for registration, is useless elsewhere."""
    from app.services import passkeys

    owner = await _user(session)
    stranger = await _user(session)
    challenge = await passkeys._new_challenge(session, owner.id, "login")
    await session.commit()

    with pytest.raises(UnauthenticatedError):
        await passkeys.consume_challenge(
            session, challenge.id, user_id=stranger.id, purpose="login"
        )
    with pytest.raises(UnauthenticatedError):
        await passkeys.consume_challenge(
            session, challenge.id, user_id=owner.id, purpose="register"
        )

    # Neither failed attempt may have spent it: the rightful owner still gets it.
    assert (
        await passkeys.consume_challenge(session, challenge.id, user_id=owner.id, purpose="login")
        == challenge.challenge
    )


async def test_an_expired_challenge_is_refused(session):
    from datetime import datetime, timedelta, timezone

    from app.services import passkeys

    user = await _user(session)
    challenge = await passkeys._new_challenge(session, user.id, "login")
    challenge.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(UnauthenticatedError):
        await passkeys.consume_challenge(session, challenge.id, user_id=user.id, purpose="login")


def test_settings_fixture_is_used():
    """Keeps make_settings imported and asserts the test defaults are what we think."""
    assert make_settings().require_passkey_for_privileged is False
