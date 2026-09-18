"""Adversarial audit (session voip-62) of the P42/P43 session + websocket auth boundary.

These tests are written to FAIL where the implementation is wrong, not to describe it.
Each one states the control it is probing and the concrete attack it stands in for.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.models import Org
from app.models import Session as IdentitySession
from tests.conftest import make_settings

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def cookie_settings():
    return make_settings(
        auth_bearer_compat=False,
        session_cookie_secure=False,
        session_idle_minutes=30,
        session_max_hours=12,
    )


@pytest.fixture
async def browser(engine, cookie_settings):
    from app.main import create_app

    application = create_app(cookie_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _csrf(client: httpx.AsyncClient) -> dict:
    return {"X-CSRF-Token": client.cookies.get("csaas_csrf", "")}


async def _login(client: httpx.AsyncClient, email: str) -> None:
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text


async def _latest_session(session) -> IdentitySession:
    return (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()


def _socket(org_id: str, cookie: str, sid=None):
    """The subset of Starlette's WebSocket that the auth helpers actually touch."""
    sock = SimpleNamespace(
        query_params={"org_id": org_id},
        cookies={"csaas_session": cookie},
        headers={"origin": "http://localhost:5173"},
    )
    if sid is not None:
        sock._csaas_session_id = sid
    return sock


async def _setup(browser, session, email: str):
    await _login(browser, email)
    r = await browser.post("/api/v1/orgs", json={"name": "Audit Co"}, headers=_csrf(browser))
    assert r.status_code == 201, r.text
    org_id = r.json()["id"]
    cookie = browser.cookies.get("csaas_session")
    row = await _latest_session(session)
    return org_id, cookie, row


# ======================================================================================
# The platform idle timeout on an ALREADY-OPEN events websocket.
#
# Attack: a console is left signed in on a shared/unattended machine. The person stops
# touching it, so no HTTP request refreshes last_seen_at. The platform promises "you were
# signed out after a period of inactivity" (SESSION_IDLE_MINUTES, default 30). The events
# socket streams live call/message/inbox events for the whole org, so if it survives the
# idle timeout the promise is only true of the REST API, not of the live data feed.
# ======================================================================================
async def test_open_socket_is_cut_off_by_the_platform_idle_timeout(
    browser, session, cookie_settings
):
    from app.api.routes.softphone import _ws_recheck, resolve_ws_org

    org_id, cookie, row = await _setup(browser, session, "ws-idle@example.com")
    sid, user_id = row.id, row.user_id

    # Handshake works right now.
    assert await resolve_ws_org(_socket(org_id, cookie), session, cookie_settings) is not None

    # The person walks away. Well past SESSION_IDLE_MINUTES (30), but the absolute
    # lifetime (12 h) has NOT run out, and the session was never revoked.
    stale = datetime.now(timezone.utc) - timedelta(minutes=90)
    row.last_seen_at = stale
    await session.commit()
    assert row.revoked_at is None
    assert row.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)

    # A NEW handshake correctly refuses -- so the rule is implemented on that path.
    assert await resolve_ws_org(_socket(org_id, cookie), session, cookie_settings) is None

    # The periodic re-check of the socket that is ALREADY open must reach the same verdict.
    again = await _ws_recheck(
        _socket(org_id, cookie, sid=sid), session, cookie_settings, uuid.UUID(org_id), user_id
    )
    assert again is None, (
        "an open events socket outlived the platform idle timeout: _ws_recheck kept "
        "streaming org events for a session the HTTP path and the WS handshake both refuse"
    )


async def test_http_path_does_enforce_the_same_idle_timeout(browser, session):
    """Control for the test above: prove the idle timeout is a real, enforced rule."""
    org_id, cookie, row = await _setup(browser, session, "http-idle@example.com")
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=90)
    await session.commit()

    r = await browser.get("/api/v1/auth/me")
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "session_expired"


async def test_open_socket_is_cut_off_by_the_ORG_idle_timeout(browser, session, cookie_settings):
    """The asymmetry: _ws_org_policy_allows implements exactly this check for the
    per-workspace setting. Same scenario, org-level limit, so the re-check DOES refuse."""
    from app.api.routes.softphone import _ws_recheck

    org_id, cookie, row = await _setup(browser, session, "ws-orgidle@example.com")
    sid, user_id = row.id, row.user_id

    org = await session.get(Org, uuid.UUID(org_id))
    org.session_idle_minutes = 15
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=90)
    await session.commit()

    again = await _ws_recheck(
        _socket(org_id, cookie, sid=sid), session, cookie_settings, uuid.UUID(org_id), user_id
    )
    assert again is None
