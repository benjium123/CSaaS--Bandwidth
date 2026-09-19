"""P42 verified email domains and SCIM 2.0 provisioning."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes.auth import _sso_enforced_for
from app.api.routes.enterprise_sso import hash_scim_token
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.main import create_app
from app.models import AuditLogEntry, Org, OrgDomain, OrgMembership, ScimToken, User
from app.models import Session as IdentitySession
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

DOMAIN = "scim-corp.example.com"
TXT = {"records": []}


async def _doh(request: httpx.Request) -> httpx.Response:
    name = request.url.params.get("name")
    answers = [{"name": name, "type": 16, "data": f'"{v}"'} for n, v in TXT["records"] if n == name]
    return httpx.Response(200, json={"Status": 0, "Answer": answers})


@pytest.fixture
async def app_client(engine):
    settings = make_settings(
        public_base_url="https://app.example.com", sso_require_verified_domain=True
    )
    application = create_app(settings)
    application.state.doh_client = httpx.AsyncClient(transport=httpx.MockTransport(_doh))
    TXT["records"] = []
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await application.state.doh_client.aclose()


async def _owner(client, domain: str = DOMAIN):
    token = await register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@{domain}")
    org = await create_org(client, token, f"Corp {uuid.uuid4().hex[:6]}")
    return token, org


def _unscoped(stmt):
    return stmt.execution_options(**{ALLOW_UNSCOPED_KEY: True})


# --- domains ------------------------------------------------------------------------------


async def test_domain_verifies_only_with_the_txt_record(app_client, session):
    token, org = await _owner(app_client)
    h = auth_headers(token, org["id"])
    r = await app_client.post(
        "/api/v1/orgs/current/domains", json={"domain": "@Scim-Corp.Example.com."}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["domain"] == DOMAIN and body["verified"] is False
    assert body["txt_name"] == f"_csaas-verify.{DOMAIN}"

    r = await app_client.post(f"/api/v1/orgs/current/domains/{body['id']}/verify", headers=h)
    assert r.status_code == 200 and r.json()["verified"] is False

    TXT["records"] = [(body["txt_name"], "csaas-verify=wrong")]
    r = await app_client.post(f"/api/v1/orgs/current/domains/{body['id']}/verify", headers=h)
    assert r.json()["verified"] is False

    TXT["records"] = [(body["txt_name"], body["txt_value"])]
    r = await app_client.post(f"/api/v1/orgs/current/domains/{body['id']}/verify", headers=h)
    assert r.json()["verified"] is True

    # A second workspace cannot also verify the same domain.
    token2, org2 = await _owner(app_client)
    h2 = auth_headers(token2, org2["id"])
    other = (
        await app_client.post("/api/v1/orgs/current/domains", json={"domain": DOMAIN}, headers=h2)
    ).json()
    TXT["records"].append((other["txt_name"], other["txt_value"]))
    r = await app_client.post(f"/api/v1/orgs/current/domains/{other['id']}/verify", headers=h2)
    assert r.status_code == 422 and r.json()["error"]["code"] == "domain_taken"


async def test_invalid_domain_is_rejected(app_client):
    token, org = await _owner(app_client)
    r = await app_client.post(
        "/api/v1/orgs/current/domains",
        json={"domain": "not a domain"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422


async def test_sso_enforcement_ignores_unverified_domains(app_client, session):
    token, org_dict = await _owner(app_client)
    org = await session.get(Org, uuid.UUID(org_dict["id"]))
    org.sso = {
        "issuer": "https://idp",
        "client_id": "c",
        "client_secret_encrypted": "x",
        "domain": DOMAIN,
        "enforce": True,
    }
    await session.commit()
    agent_email = f"agent-{uuid.uuid4().hex[:6]}@{DOMAIN}"
    await register_and_login(app_client, agent_email)
    agent = (
        await session.execute(_unscoped(sa.select(User).where(User.email == agent_email)))
    ).scalar_one()
    set_org_context(session, org.id)
    from app.models import Role

    role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    session.add(OrgMembership(org_id=org.id, user_id=agent.id, role_id=role.id))
    await session.commit()
    settings = make_settings(sso_require_verified_domain=True)

    assert await _sso_enforced_for(session, agent, agent_email, settings) is False
    set_org_context(session, org.id)
    session.add(
        OrgDomain(
            id=uuid.uuid4(),
            org_id=org.id,
            domain=DOMAIN,
            verify_token="t",
            verified_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    assert await _sso_enforced_for(session, agent, agent_email, settings) is True


# --- SCIM ---------------------------------------------------------------------------------


async def _scim_setup(client, session, *, verified: bool = True):
    token, org_dict = await _owner(client)
    org_id = uuid.UUID(org_dict["id"])
    set_org_context(session, org_id)
    secret = f"scim_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex}"
    session.add(
        ScimToken(
            id=uuid.uuid4(),
            org_id=org_id,
            name="okta",
            prefix=secret.split("_")[1],
            token_hash=hash_scim_token(secret),
        )
    )
    if verified:
        session.add(
            OrgDomain(
                id=uuid.uuid4(),
                org_id=org_id,
                domain=DOMAIN,
                verify_token="t",
                verified_at=datetime.now(timezone.utc),
            )
        )
    await session.commit()
    return token, org_id, {"Authorization": f"Bearer {secret}"}


def _user(email: str, **extra) -> dict:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": email,
        "name": {"givenName": "Pat", "familyName": "Lee"},
        "active": True,
        **extra,
    }


async def test_scim_requires_a_valid_token(app_client, session):
    r = await app_client.get("/scim/v2/Users")
    assert r.status_code == 401
    assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    r = await app_client.get(
        "/scim/v2/Users", headers={"Authorization": "Bearer scim_deadbeef_nope"}
    )
    assert r.status_code == 401


async def test_scim_refuses_unverified_domains(app_client, session):
    _t, _org, h = await _scim_setup(app_client, session, verified=False)
    r = await app_client.post("/scim/v2/Users", json=_user(f"p@{DOMAIN}"), headers=h)
    assert r.status_code == 403


async def test_scim_provision_list_and_deprovision_ends_sessions(app_client, session):
    _t, org_id, h = await _scim_setup(app_client, session)
    email = f"pat-{uuid.uuid4().hex[:6]}@{DOMAIN}"
    # An existing account (with a live session) gets linked, not duplicated.
    await register_and_login(app_client, email)

    r = await app_client.post("/scim/v2/Users", json=_user(email), headers=h)
    assert r.status_code == 201, r.text
    assert r.headers["content-type"].startswith("application/scim+json")
    user_id = r.json()["id"]
    assert (
        await app_client.post("/scim/v2/Users", json=_user(email), headers=h)
    ).status_code == 409

    r = await app_client.get(f'/scim/v2/Users?filter=userName eq "{email}"', headers=h)
    assert r.json()["totalResults"] == 1 and r.json()["Resources"][0]["id"] == user_id

    r = await app_client.patch(
        f"/scim/v2/Users/{user_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
        },
        headers=h,
    )
    assert r.status_code == 200 and r.json()["active"] is False

    uid = uuid.UUID(user_id)
    memberships = (
        (
            await session.execute(
                _unscoped(
                    sa.select(OrgMembership).where(
                        OrgMembership.user_id == uid, OrgMembership.org_id == org_id
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert memberships == []
    live = (
        (
            await session.execute(
                sa.select(IdentitySession).where(
                    IdentitySession.user_id == uid, IdentitySession.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert live == []
    actions = (
        (
            await session.execute(
                _unscoped(sa.select(AuditLogEntry.action).where(AuditLogEntry.org_id == org_id))
            )
        )
        .scalars()
        .all()
    )
    assert {"scim.user_provisioned", "scim.user_deprovisioned"} <= set(actions)
    assert (await app_client.get(f"/scim/v2/Users/{user_id}", headers=h)).status_code == 404


async def test_scim_cannot_remove_or_rerole_the_owner(app_client, session):
    _t, org_id, h = await _scim_setup(app_client, session)
    owner = (
        await session.execute(
            _unscoped(sa.select(OrgMembership.user_id).where(OrgMembership.org_id == org_id))
        )
    ).scalar_one()
    await session.commit()
    assert (await app_client.delete(f"/scim/v2/Users/{owner}", headers=h)).status_code == 403

    groups = (await app_client.get("/scim/v2/Groups", headers=h)).json()["Resources"]
    assert "owner" not in {g["displayName"] for g in groups}
    agent_group = next(g for g in groups if g["displayName"] == "agent")
    r = await app_client.patch(
        f"/scim/v2/Groups/{agent_group['id']}",
        json={"Operations": [{"op": "add", "path": "members", "value": [{"value": str(owner)}]}]},
        headers=h,
    )
    assert r.status_code == 403


async def test_scim_group_membership_changes_role(app_client, session):
    _t, org_id, h = await _scim_setup(app_client, session)
    email = f"g-{uuid.uuid4().hex[:6]}@{DOMAIN}"
    user_id = (await app_client.post("/scim/v2/Users", json=_user(email), headers=h)).json()["id"]
    groups = (await app_client.get("/scim/v2/Groups", headers=h)).json()["Resources"]
    target = next(g for g in groups if g["displayName"] not in ("agent",))
    r = await app_client.patch(
        f"/scim/v2/Groups/{target['id']}",
        json={"Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert user_id in {m["value"] for m in r.json()["members"]}


async def test_scim_token_creation_needs_fresh_second_factor(app_client):
    token, org = await _owner(app_client)
    r = await app_client.post(
        "/api/v1/orgs/current/scim-tokens",
        json={"name": "okta"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 403
    assert r.json()["error"]["kind"] == "recent_2fa"
