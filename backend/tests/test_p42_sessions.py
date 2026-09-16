"""P42 slice 2: HttpOnly cookie sessions, CSRF, idle/absolute timeouts, rotation, logout."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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


async def _login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] is None, "no bearer token without compat mode"
    assert "csaas_session" in client.cookies
    set_cookie = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    session_cookie = next(c for c in set_cookie if c.startswith("csaas_session="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie


async def _latest_session(session) -> IdentitySession:
    return (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()


async def test_cookie_login_and_bearer_refused(browser, session):
    await _login(browser, "cookie@example.com")
    assert (await browser.get("/api/v1/auth/me")).status_code == 200
    row = await _latest_session(session)
    assert row.token_hash and len(row.token_hash) == 64
    assert row.auth_method == "password"

    # The same person with a forged/old bearer header and no cookie gets nowhere.
    anon = httpx.AsyncClient(transport=browser._transport, base_url="http://test")
    r = await anon.get("/api/v1/auth/me", headers={"Authorization": "Bearer abc.def.ghi"})
    assert r.status_code == 401
    await anon.aclose()


async def test_unsafe_requests_need_the_csrf_token(browser):
    await _login(browser, "csrf@example.com")
    r = await browser.post("/api/v1/orgs", json={"name": "No Token"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"
    r = await browser.post(
        "/api/v1/orgs", json={"name": "Wrong"}, headers={"X-CSRF-Token": "0" * 64}
    )
    assert r.status_code == 403
    r = await browser.post("/api/v1/orgs", json={"name": "With Token"}, headers=_csrf(browser))
    assert r.status_code == 201, r.text


async def test_idle_timeout_ends_the_session(browser, session):
    await _login(browser, "idle@example.com")
    row = await _latest_session(session)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    await session.commit()
    r = await browser.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "session_expired"
    session.expire_all()
    assert (await _latest_session(session)).revoked_at is not None


async def test_absolute_timeout(browser, session):
    await _login(browser, "absolute@example.com")
    row = await _latest_session(session)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    assert (await browser.get("/api/v1/auth/me")).status_code == 401


async def test_stolen_cookie_with_wrong_secret_is_rejected(browser, session):
    await _login(browser, "forge@example.com")
    row = await _latest_session(session)
    browser.cookies.set("csaas_session", f"{row.id}.{'x' * 43}")
    assert (await browser.get("/api/v1/auth/me")).status_code == 401


async def test_logout_revokes_and_clears(browser, session):
    await _login(browser, "logout@example.com")
    old_cookie = browser.cookies.get("csaas_session")
    r = await browser.post("/api/v1/auth/logout")
    assert r.status_code == 204
    assert (await browser.get("/api/v1/auth/me")).status_code == 401
    # Replaying the old cookie value does not bring the session back.
    browser.cookies.set("csaas_session", old_cookie)
    assert (await browser.get("/api/v1/auth/me")).status_code == 401
    session.expire_all()
    assert (await _latest_session(session)).revoked_at is not None


async def test_workspace_can_demand_shorter_idle_time(browser, session):
    await _login(browser, "strict@example.com")
    r = await browser.post("/api/v1/orgs", json={"name": "Strict Co"}, headers=_csrf(browser))
    org_id = r.json()["id"]
    org = await session.get(Org, uuid.UUID(org_id))
    org.session_idle_minutes = 5
    row = await _latest_session(session)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.commit()
    r = await browser.get("/api/v1/orgs/current", headers={"X-Org-Id": org_id})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "session_expired"


async def test_policy_cannot_exceed_platform_maximum(browser, session):
    await _login(browser, "policy@example.com")
    r = await browser.post("/api/v1/orgs", json={"name": "Policy Co"}, headers=_csrf(browser))
    org_id = r.json()["id"]
    h = {"X-Org-Id": org_id, **_csrf(browser)}
    r = await browser.patch(
        "/api/v1/orgs/current/security", json={"session_idle_minutes": 600}, headers=h
    )
    assert r.status_code == 422
    r = await browser.patch(
        "/api/v1/orgs/current/security",
        json={"session_idle_minutes": 15, "session_max_hours": 8},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_idle_minutes"] == 15 and r.json()["session_max_hours"] == 8


async def test_removed_member_is_signed_out(browser, session, engine, cookie_settings):
    from app.main import create_app
    from app.models import OrgMembership, Role, User

    await _login(browser, "owner@remove.example")
    r = await browser.post("/api/v1/orgs", json={"name": "Remove Co"}, headers=_csrf(browser))
    org_id = r.json()["id"]

    other_app = create_app(cookie_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=other_app), base_url="http://test"
    ) as member:
        await _login(member, "member@remove.example")
        from app.db.base import set_org_context

        set_org_context(session, uuid.UUID(org_id))
        role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
        user = (
            await session.execute(sa.select(User).where(User.email == "member@remove.example"))
        ).scalar_one()
        user_id = user.id
        session.add(
            OrgMembership(
                id=uuid.uuid4(), org_id=uuid.UUID(org_id), user_id=user_id, role_id=role.id
            )
        )
        await session.commit()
        assert (await member.get("/api/v1/auth/me")).status_code == 200

        r = await browser.delete(
            f"/api/v1/orgs/current/members/{user_id}",
            headers={"X-Org-Id": org_id, **_csrf(browser)},
        )
        assert r.status_code == 204, r.text
        assert (await member.get("/api/v1/auth/me")).status_code == 401


async def test_websocket_cookie_auth_checks_origin(browser, session, cookie_settings):
    from types import SimpleNamespace

    from app.api.routes.softphone import resolve_ws_org

    await _login(browser, "ws@example.com")
    r = await browser.post("/api/v1/orgs", json={"name": "WS Co"}, headers=_csrf(browser))
    org_id = r.json()["id"]
    cookie = browser.cookies.get("csaas_session")

    def socket(origin):
        return SimpleNamespace(
            query_params={"org_id": org_id},
            cookies={"csaas_session": cookie},
            headers={"origin": origin},
        )

    ok = await resolve_ws_org(socket("http://localhost:5173"), session, cookie_settings)
    assert ok is not None and str(ok[0]) == org_id
    assert await resolve_ws_org(socket("https://evil.example"), session, cookie_settings) is None
