"""P42 slice 4: passkeys required for owners, admins, billing and operators."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.models import Org, User
from app.models import Session as IdentitySession
from tests.conftest import auth_headers, create_org, make_settings, register_and_login


@pytest.fixture
def policy_settings():
    return make_settings(require_passkey_for_privileged=True, passkey_grace_days=14)


@pytest.fixture
async def pclient(engine, policy_settings):
    from app.main import create_app

    application = create_app(policy_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def fake_webauthn(monkeypatch):
    import webauthn
    from webauthn.helpers import base64url_to_bytes

    monkeypatch.setattr(
        webauthn,
        "verify_registration_response",
        lambda *, credential, **_: SimpleNamespace(
            credential_id=base64url_to_bytes(credential["id"]),
            credential_public_key=b"pk",
            sign_count=0,
        ),
    )
    monkeypatch.setattr(
        webauthn,
        "verify_authentication_response",
        lambda *, credential_current_sign_count, **_: SimpleNamespace(
            new_sign_count=credential_current_sign_count + 1
        ),
    )


def _cred(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


async def _age_grace(session, email: str, days: int) -> None:
    user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
    user.passkey_required_since = datetime.now(timezone.utc) - timedelta(days=days)
    await session.commit()


async def test_owner_needs_a_passkey_session_after_grace(pclient, session):
    token = await register_and_login(pclient, "owner@pk.example")
    org = await create_org(pclient, token, "PK Co")
    h = auth_headers(token, org["id"])

    # Inside the grace period a password session still works (and starts the clock).
    assert (await pclient.get("/api/v1/orgs/current", headers=h)).status_code == 200
    me = (await pclient.get("/api/v1/auth/me", headers=h)).json()
    assert me["passkey_required"] is True and me["passkey_grace_until"]

    await _age_grace(session, "owner@pk.example", 15)
    r = await pclient.get("/api/v1/orgs/current", headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "passkey_required"
    # Enrolment routes stay reachable so the person can fix it.
    opts = await pclient.post("/api/v1/auth/passkeys/register/options", headers=h)
    assert opts.status_code == 200
    r = await pclient.post(
        "/api/v1/auth/passkeys/register",
        json={"challenge_id": opts.json()["challenge_id"], "credential": {"id": _cred("pk-owner")}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    # Registering alone is not enough - the SESSION must be a passkey session.
    assert (await pclient.get("/api/v1/orgs/current", headers=h)).status_code == 403
    opts = await pclient.post("/api/v1/auth/passkeys/step-up/options", headers=h)
    r = await pclient.post(
        "/api/v1/auth/passkeys/step-up/verify",
        json={"challenge_id": opts.json()["challenge_id"], "credential": {"id": _cred("pk-owner")}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert (await pclient.get("/api/v1/orgs/current", headers=h)).status_code == 200


async def test_agents_are_not_affected(pclient, session):
    from app.db.base import set_org_context
    from app.models import OrgMembership, Role

    owner = await register_and_login(pclient, "boss@pk.example")
    org = await create_org(pclient, owner, "Agents Co")
    agent_token = await register_and_login(pclient, "agent@pk.example")
    set_org_context(session, uuid.UUID(org["id"]))
    role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    agent = (
        await session.execute(sa.select(User).where(User.email == "agent@pk.example"))
    ).scalar_one()
    agent.passkey_required_since = datetime.now(timezone.utc) - timedelta(days=60)
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=uuid.UUID(org["id"]), user_id=agent.id, role_id=role.id
        )
    )
    await session.commit()
    r = await pclient.get("/api/v1/contacts", headers=auth_headers(agent_token, org["id"]))
    assert r.status_code == 200, r.text


async def test_trusted_idp_sso_session_counts(pclient, session):
    token = await register_and_login(pclient, "sso@pk.example")
    org = await create_org(pclient, token, "SSO Co")
    h = auth_headers(token, org["id"])
    await pclient.get("/api/v1/orgs/current", headers=h)
    await _age_grace(session, "sso@pk.example", 30)
    row = (
        await session.execute(
            sa.select(IdentitySession).order_by(IdentitySession.created_at.desc()).limit(1)
        )
    ).scalar_one()
    row.auth_method = "sso"
    await session.commit()
    assert (await pclient.get("/api/v1/orgs/current", headers=h)).status_code == 403
    org_row = await session.get(Org, uuid.UUID(org["id"]))
    org_row.trust_idp_mfa = True
    await session.commit()
    assert (await pclient.get("/api/v1/orgs/current", headers=h)).status_code == 200


def test_p25_policy_counts_passkeys():
    from app.services.identity import two_factor_required

    org = SimpleNamespace(require_2fa=True, require_2fa_grace_until=None)
    passkey_only = SimpleNamespace(totp_enabled=False, has_passkey=True, has_second_factor=True)
    nothing = SimpleNamespace(totp_enabled=False, has_passkey=False, has_second_factor=False)
    assert two_factor_required(org, passkey_only) is False
    assert two_factor_required(org, nothing) is True
