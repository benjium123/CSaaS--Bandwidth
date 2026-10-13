"""Customer-side account deletion request (Google Play data-deletion requirement)."""

from __future__ import annotations

import sqlalchemy as sa

from app.models import SecurityAlert
from tests.conftest import auth_headers, create_org, register_and_login

PASSWORD = "correct-horse-battery"
URL = "/api/v1/auth/deletion-request"


async def _alerts(session):
    return (
        (
            await session.execute(
                sa.select(SecurityAlert)
                .where(SecurityAlert.kind == "account_deletion_request")
                .execution_options(allow_unscoped=True)
            )
        )
        .scalars()
        .all()
    )


async def test_requires_sign_in(client):
    assert (await client.get(URL)).status_code == 401
    r = await client.post(URL, json={"current_password": PASSWORD})
    assert r.status_code == 401


async def test_wrong_password_is_refused_and_creates_nothing(client, session):
    token = await register_and_login(client, "wrongpw@example.com", PASSWORD)
    r = await client.post(URL, json={"current_password": "nope"}, headers=auth_headers(token))
    assert r.status_code == 401, r.text
    assert await _alerts(session) == []


async def test_request_creates_one_open_alert_for_ops(client, session):
    token = await register_and_login(client, "leaving@example.com", PASSWORD)
    org = await create_org(client, token, "Leaving Co")

    r = await client.get(URL, headers=auth_headers(token))
    assert r.status_code == 200 and r.json()["status"] == "none"

    r = await client.post(
        URL,
        json={"current_password": PASSWORD, "reason": "  switching providers  "},
        headers=auth_headers(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending" and r.json()["requested_at"]

    # Asking again does not open a second request.
    r2 = await client.post(URL, json={"current_password": PASSWORD}, headers=auth_headers(token))
    assert r2.status_code == 201 and r2.json()["requested_at"] == r.json()["requested_at"]

    alerts = await _alerts(session)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.status == "open"
    assert alert.detail["email"] == "leaving@example.com"
    assert alert.detail["reason"] == "switching providers"
    assert alert.detail["is_owner"] is True
    listed = {w["id"]: w for w in alert.detail["workspaces"]}
    assert listed[str(org["id"])]["name"] == "Leaving Co"
    assert listed[str(org["id"])]["role"] == "owner"

    r = await client.get(URL, headers=auth_headers(token))
    assert r.json()["status"] == "pending"
