"""A code emailed to the account address works as a second factor.

Pins: enrolment and sign-in round trips; a code is single-use, dies after its TTL and after
MAX_ATTEMPTS wrong guesses, and only works for the purpose it was sent for; re-sending is
throttled; and an approved owner cannot drop email codes when they are the only factor.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.errors import UnauthenticatedError
from app.models import User
from app.services import email_code, mailer
from tests.conftest import approve_workspaces, auth_headers, make_settings, register_and_login

PASSWORD = "correct-horse-battery"


@pytest.fixture
async def sf_client(engine):
    from app.main import create_app

    application = create_app(make_settings(require_2fa_privileged_users=True))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def clear_outbox():
    mailer.outbox.clear()
    yield
    mailer.outbox.clear()


def _last_code(email: str) -> str:
    message = next(
        m for m in reversed(mailer.outbox) if email in m["To"] and "code" in m["Subject"]
    )
    return re.match(r"(\d{6}) is your ", message["Subject"]).group(1)


async def _user(session, email: str) -> User:
    return (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def _enrol(client, token: str, email: str) -> None:
    r = await client.post(
        "/api/v1/auth/2fa/email/enrol/send",
        json={"password": PASSWORD},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/v1/auth/2fa/email/enrol/activate",
        json={"code": _last_code(email)},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"email_2fa_enabled": True}


async def _pending(client, email: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_2fa"] is True and body["methods"] == ["email"], body
    assert body["access_token"] is None
    return body["pending_token"]


async def test_an_approved_owner_can_enrol_with_email_and_sign_in_with_a_code(sf_client):
    email = "email-owner@example.com"
    token = await register_and_login(sf_client, email)
    await approve_workspaces(email)
    me = (await sf_client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert me["second_factor_required"] is True

    await _enrol(sf_client, token, email)
    me = (await sf_client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert me["second_factor_required"] is False
    assert me["email_2fa_enabled"] is True

    pending = await _pending(sf_client, email)
    r = await sf_client.post("/api/v1/auth/2fa/email/login/send", json={"pending_token": pending})
    assert r.status_code == 200, r.text
    code = _last_code(email)

    wrong = "000000" if code != "000000" else "111111"
    r = await sf_client.post(
        "/api/v1/auth/2fa/email/login/verify", json={"pending_token": pending, "code": wrong}
    )
    assert r.status_code == 401, r.text

    r = await sf_client.post(
        "/api/v1/auth/2fa/email/login/verify", json={"pending_token": pending, "code": code}
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]

    # Single use: the same code cannot finish a second sign-in.
    r = await sf_client.post(
        "/api/v1/auth/2fa/email/login/verify", json={"pending_token": pending, "code": code}
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "email_code_expired"


async def test_a_code_only_works_for_its_purpose_and_dies_on_expiry_or_too_many_guesses(
    sf_client, session
):
    email = "email-rules@example.com"
    token = await register_and_login(sf_client, email)
    r = await sf_client.post(
        "/api/v1/auth/2fa/email/enrol/send",
        json={"password": PASSWORD},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    code = _last_code(email)
    settings = make_settings()
    user = await _user(session, email)

    # An enrolment code cannot pass as a sign-in or step-up code.
    for purpose in ("login", "step_up"):
        with pytest.raises(UnauthenticatedError) as exc:
            email_code.check(settings, user, purpose, code)
        assert getattr(exc.value, "code", None) == "email_code_expired"

    # Five wrong guesses burn it: the right code is refused after that.
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(email_code.MAX_ATTEMPTS):
        with pytest.raises(UnauthenticatedError):
            email_code.check(settings, user, "enrol", wrong)
    with pytest.raises(UnauthenticatedError) as exc:
        email_code.check(settings, user, "enrol", code)
    assert getattr(exc.value, "code", None) == "email_code_expired"

    # Expiry: a fresh code past its TTL is refused.
    user.email_code_expires_at = None
    await email_code.issue(settings, user, "enrol")
    fresh = _last_code(email)
    user.email_code_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(UnauthenticatedError) as exc:
        email_code.check(settings, user, "enrol", fresh)
    assert getattr(exc.value, "code", None) == "email_code_expired"


async def test_resending_is_throttled(sf_client):
    email = "email-throttle@example.com"
    token = await register_and_login(sf_client, email)
    first = await sf_client.post(
        "/api/v1/auth/2fa/email/enrol/send",
        json={"password": PASSWORD},
        headers=auth_headers(token),
    )
    assert first.status_code == 200, first.text
    again = await sf_client.post(
        "/api/v1/auth/2fa/email/enrol/send",
        json={"password": PASSWORD},
        headers=auth_headers(token),
    )
    assert again.status_code == 422, again.text
    assert again.json()["error"]["code"] == "email_code_throttled"


async def test_an_approved_owner_cannot_drop_their_only_factor(sf_client):
    email = "email-last@example.com"
    token = await register_and_login(sf_client, email)
    await approve_workspaces(email)
    await _enrol(sf_client, token, email)
    r = await sf_client.post(
        "/api/v1/auth/2fa/email/disable", json={"password": PASSWORD}, headers=auth_headers(token)
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "last_second_factor"


async def test_an_unapproved_owner_may_turn_email_codes_off_again(sf_client):
    email = "email-optional@example.com"
    token = await register_and_login(sf_client, email)
    await _enrol(sf_client, token, email)
    r = await sf_client.post(
        "/api/v1/auth/2fa/email/disable", json={"password": PASSWORD}, headers=auth_headers(token)
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"email_2fa_enabled": False}
