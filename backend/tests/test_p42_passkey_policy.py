"""P42 slice 4, narrowed: passkeys are required for platform operators only.

Customer owners, admins and billing staff pick their own second factor (email code,
authenticator app or passkey) - the product owner's call.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.models import User
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


async def test_customer_owner_is_not_held_to_passkeys(pclient, session):
    """Customer owners choose their own second factor; the passkey mandate is operators only.

    If this failed, every customer would be locked out of their own workspace two weeks after
    signing up unless they owned a passkey - making email codes and authenticator apps moot.
    """
    token = await register_and_login(pclient, "owner@pk.example")
    org = await create_org(pclient, token, "PK Co")
    h = auth_headers(token, org["id"])
    assert (await pclient.get("/api/v1/orgs/current", headers=h)).status_code == 200
    await _age_grace(session, "owner@pk.example", 60)
    r = await pclient.get("/api/v1/orgs/current", headers=h)
    assert r.status_code == 200, r.text
    me = (await pclient.get("/api/v1/auth/me", headers=h)).json()
    assert me["passkey_required"] is False


async def test_operator_needs_a_passkey_session_after_grace(pclient, session):
    """Platform operators (super admins) still must sign in with a passkey after the grace."""
    from app.models import PlatformOperator
    from tests.conftest import mark_recent_2fa

    email = "op@pk.example"
    token = await register_and_login(pclient, email)
    user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
    user.totp_enabled = True
    session.add(PlatformOperator(id=uuid.uuid4(), user_id=user.id, role="admin", is_active=True))
    await session.commit()
    await mark_recent_2fa(session, email)
    h = auth_headers(token)

    # Inside the grace period a non-passkey session works (and starts the clock).
    r = await pclient.get("/api/v1/ops/queue", headers=h)
    assert r.status_code == 200, r.text
    me = (await pclient.get("/api/v1/auth/me", headers=h)).json()
    assert me["passkey_required"] is True and me["passkey_grace_until"]

    await _age_grace(session, email, 15)
    r = await pclient.get("/api/v1/ops/queue", headers=h)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "passkey_required"


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


def test_p25_policy_counts_passkeys():
    from app.services.identity import two_factor_required

    org = SimpleNamespace(require_2fa=True, require_2fa_grace_until=None)
    passkey_only = SimpleNamespace(totp_enabled=False, has_passkey=True, has_second_factor=True)
    nothing = SimpleNamespace(totp_enabled=False, has_passkey=False, has_second_factor=False)
    assert two_factor_required(org, passkey_only) is False
    assert two_factor_required(org, nothing) is True
