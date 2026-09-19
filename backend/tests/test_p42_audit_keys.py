"""P42 slice 6: audit trail for numbers and invites, emailed invites, security headers,
API key network restriction / lifetime / rotation overlap, members' sign-ins for admins."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.models import ApiKey, AuditLogEntry
from tests.conftest import (
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)


@pytest.fixture(autouse=True)
def outbox():
    from app.services import mailer

    mailer.outbox.clear()
    yield mailer.outbox
    mailer.outbox.clear()


async def _audit_actions(session, org_id) -> list[str]:
    from app.db.base import set_org_context

    set_org_context(session, uuid.UUID(str(org_id)))
    return list((await session.execute(sa.select(AuditLogEntry.action))).scalars().all())


async def test_number_add_and_release_are_audited(app_with_carrier, session):
    client, carrier, _ = app_with_carrier
    token, org, number = await make_org_with_number(
        client, "num@audit.example", "Num Co", "+15125550142"
    )
    r = await client.delete(
        f"/api/v1/numbers/{number['id']}", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    actions = await _audit_actions(session, org["id"])
    assert "number.added" in actions and "number.released" in actions


async def test_invites_are_emailed_and_audited(client, session, outbox):
    token = await register_and_login(client, "inviter@audit.example")
    org = await create_org(client, token, "Invite Co")
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": "new@audit.example", "role_name": "agent"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert outbox and "new@audit.example" in outbox[-1]["To"]
    assert r.json()["accept_url"] in outbox[-1].get_body(preferencelist=("plain",)).get_content()
    invite_id = r.json()["id"]
    r = await client.delete(f"/api/v1/orgs/current/invites/{invite_id}", headers=h)
    assert r.status_code == 200
    actions = await _audit_actions(session, org["id"])
    assert "invite.created" in actions and "invite.revoked" in actions


async def test_api_responses_carry_security_headers(client):
    r = await client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    r = await client.post(
        "/api/v1/auth/login", json={"email": "x@y.example", "password": "nope-nope-nope"}
    )
    assert r.headers["cache-control"] == "no-store"


async def test_api_key_network_restriction(client, session):
    token = await register_and_login(client, "keys@audit.example")
    org = await create_org(client, token, "Key Co")
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/api-keys",
        json={
            "name": "office only",
            "scopes": ["contacts:read"],
            "allowed_cidrs": ["203.0.113.0/24"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert r.json()["allowed_cidrs"] == ["203.0.113.0/24"]
    outside = await client.get(
        "/api/v1/contacts",
        headers={"Authorization": f"Bearer {key}", "X-Forwarded-For": "198.51.100.1"},
    )
    assert outside.status_code == 401
    inside = await client.get(
        "/api/v1/contacts",
        headers={"Authorization": f"Bearer {key}", "X-Forwarded-For": "203.0.113.7"},
    )
    assert inside.status_code == 200, inside.text
    from app.db.base import set_org_context

    set_org_context(session, uuid.UUID(org["id"]))
    row = (await session.execute(sa.select(ApiKey))).scalar_one()
    assert row.last_used_ip == "203.0.113.7"


async def test_api_key_lifetime_and_rotation_overlap(engine, session):
    from app.main import create_app

    settings = make_settings(api_key_max_days=365)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        token = await register_and_login(c, "life@audit.example")
        org = await create_org(c, token, "Life Co")
        h = auth_headers(token, org["id"])
        r = await c.post(
            "/api/v1/api-keys", json={"name": "default", "scopes": ["contacts:read"]}, headers=h
        )
        assert r.status_code == 201
        expires = datetime.fromisoformat(r.json()["expires_at"])
        expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        assert timedelta(days=364) < expires - datetime.now(timezone.utc) <= timedelta(days=365)
        too_long = (datetime.now(timezone.utc) + timedelta(days=800)).isoformat()
        r2 = await c.post(
            "/api/v1/api-keys",
            json={"name": "forever", "scopes": ["contacts:read"], "expires_at": too_long},
            headers=h,
        )
        assert r2.status_code == 422

        old_key = r.json()["key"]
        r = await c.post(f"/api/v1/api-keys/{r.json()['id']}/rotate?overlap_hours=24", headers=h)
        assert r.status_code == 200, r.text
        # The old key keeps working during the overlap window.
        assert (
            await c.get("/api/v1/contacts", headers={"Authorization": f"Bearer {old_key}"})
        ).status_code == 200


async def test_admins_see_members_password_sign_ins(client):
    token = await register_and_login(client, "admin@events.example")
    org = await create_org(client, token, "Events Co")
    await client.post(
        "/api/v1/auth/login", json={"email": "admin@events.example", "password": "wrong-password-x"}
    )
    r = await client.get(
        "/api/v1/orgs/current/login-events", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    outcomes = {e["outcome"] for e in r.json()}
    assert {"ok", "bad_password"} <= outcomes
