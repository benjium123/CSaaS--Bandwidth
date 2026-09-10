from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.security import hash_password
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.main import create_app
from app.models import LoginEvent, Org, OrgMembership, Role, User
from app.models import Session as IdentitySession
from app.services import credentials as credentials_svc
from app.services import oidc, session_cache
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

ISSUER = "https://idp.example.com"
CLIENT_ID = "sso-client-id"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"

# 2048-bit keygen is slow enough that the IdP key is generated only once per module.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_BAD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_BAD_PRIVATE_PEM = _BAD_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)


def _b64url_int(n: int) -> str:
    """Base64url-encode a big-endian unsigned integer for a JWK."""
    length = max(1, (n.bit_length() + 7) // 8)
    raw = n.to_bytes(length, byteorder="big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _build_jwk() -> dict:
    public_numbers = _KEY.public_key().public_numbers()
    return {
        "kid": "k1",
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": _b64url_int(public_numbers.n),
        "e": _b64url_int(public_numbers.e),
    }


_JWK = _build_jwk()
_DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
    "token_endpoint": TOKEN_ENDPOINT,
    "jwks_uri": JWKS_URI,
}
_JWKS_DOC = {"keys": [_JWK]}

_IDP_STATE = {"id_token": None, "requests": []}


async def _handle_request(request: httpx.Request) -> httpx.Response:
    _IDP_STATE["requests"].append(request)
    url = str(request.url)

    if request.method == "GET" and url == f"{ISSUER}/.well-known/openid-configuration":
        return httpx.Response(200, json=_DISCOVERY)

    if request.method == "GET" and url == JWKS_URI:
        return httpx.Response(200, json=_JWKS_DOC)

    if request.method == "POST" and url == TOKEN_ENDPOINT:
        return httpx.Response(
            200,
            json={
                "id_token": _IDP_STATE["id_token"],
                "access_token": "x",
                "token_type": "bearer",
            },
        )

    return httpx.Response(404, json={"error": "not_found"})


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handle_request),
        timeout=10.0,
        follow_redirects=False,
    )


def _set_id_token(token: str) -> None:
    _IDP_STATE["id_token"] = token


@pytest.fixture(autouse=True)
def _reset_oidc_state():
    _IDP_STATE["id_token"] = None
    _IDP_STATE["requests"] = []
    oidc.reset_caches()
    session_cache.reset_memory_cache()
    oidc.set_client_factory(_client_factory)
    yield
    oidc.reset_client_factory()
    oidc.reset_caches()
    session_cache.reset_memory_cache()
    _IDP_STATE["id_token"] = None
    _IDP_STATE["requests"] = []


@pytest.fixture
def app_settings():
    return make_settings(
        credentials_master_key=Fernet.generate_key().decode(),
        public_base_url="https://app.example.com",
    )


@pytest.fixture
async def sso_client(engine, app_settings):
    application = create_app(app_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client



def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _id_token_claims(
    email: str,
    *,
    nonce: str,
    issuer: str = ISSUER,
    audience: str = CLIENT_ID,
    email_verified: bool = True,
    exp_delta_hours: int = 1,
    **overrides,
) -> dict:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "subject-123456",
        "email": email,
        "email_verified": email_verified,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=exp_delta_hours)).timestamp()),
        "nonce": nonce,
    }
    claims.update(overrides)
    return claims


def _make_id_token(
    email: str,
    *,
    nonce: str,
    private_pem: bytes = _PRIVATE_PEM,
    **kwargs,
) -> str:
    claims = _id_token_claims(email, nonce=nonce, **kwargs)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "k1"})


async def _create_owner_org(
    client: httpx.AsyncClient,
    session,
    *,
    domain: str = "corp.example.com",
) -> tuple[Org, str]:
    owner_email = f"owner-{uuid.uuid4().hex}@{domain}"
    token = await register_and_login(client, owner_email)
    org_dict = await create_org(client, token, f"SSO Org {uuid.uuid4().hex}")
    org = await session.get(Org, uuid.UUID(org_dict["id"]))
    assert org is not None
    return org, owner_email


async def _org_with_sso(
    client: httpx.AsyncClient,
    session,
    settings,
    *,
    enforce: bool = False,
    domain: str = "corp.example.com",
    sso_overrides: dict | None = None,
) -> Org:
    org, _owner_email = await _create_owner_org(client, session, domain=domain)
    encrypted = credentials_svc.encrypt(settings, {"client_secret": "s3cr3t"})
    sso = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret_encrypted": encrypted,
        "domain": domain,
        "enforce": enforce,
    }
    if sso_overrides:
        sso.update(sso_overrides)
    org.sso = sso
    await session.commit()
    await session.refresh(org)
    await session.commit()
    return org


async def _get_role(session, org: Org, name: str) -> Role:
    # Bind the tenant context here: every caller goes on to read or write TenantScoped
    # rows (OrgMembership / Role) for this same org, and the guard requires a context.
    set_org_context(session, org.id)
    stmt = (
        sa.select(Role)
        .where(
            Role.org_id == org.id,
            Role.name == name,
            Role.is_system.is_(True),
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return (await session.execute(stmt)).scalar_one()


async def _count(session, model, **where) -> int:
    # Counts a mapped column, never a bare count(). OrgMembership is TenantScoped, and
    # these probes deliberately run with no org bound, so the guard is opted out of
    # EXPLICITLY here rather than by binding a context that would hide a leak across orgs.
    stmt = sa.select(sa.func.count(model.id)).execution_options(allow_unscoped=True)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return (await session.execute(stmt)).scalar_one()


async def _snapshot_counts(session) -> dict:
    counts = {
        "users": await _count(session, User),
        "sessions": await _count(session, IdentitySession),
        "memberships": await _count(session, OrgMembership),
    }
    await session.commit()
    return counts


async def _start(client: httpx.AsyncClient, org_slug: str) -> tuple[str, dict]:
    r = await client.get(
        f"/api/v1/auth/sso/{org_slug}/start",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    return location, parse_qs(urlparse(location).query)


async def _callback(
    client: httpx.AsyncClient,
    state: str,
    code: str = "test-auth-code",
) -> httpx.Response:
    return await client.get(
        "/api/v1/auth/sso/callback",
        params={"code": code, "state": state},
    )


# --- start flow ---------------------------------------------------------------

async def test_start_unknown_org_404(sso_client):
    r = await sso_client.get(
        "/api/v1/auth/sso/does-not-exist/start",
        follow_redirects=False,
    )
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "Single sign-on is not configured"


async def test_start_org_without_sso_404(sso_client, session):
    org, _owner_email = await _create_owner_org(sso_client, session)
    r = await sso_client.get(
        f"/api/v1/auth/sso/{org.slug}/start",
        follow_redirects=False,
    )
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "Single sign-on is not configured"


async def test_start_redirects_with_state_and_nonce(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    location, query = await _start(sso_client, org.slug)

    assert urlparse(location).hostname == urlparse(AUTHORIZATION_ENDPOINT).hostname
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert "openid" in query["scope"][0]
    # D59: redirect_uri must be the SPA route, not the API path - the browser lands here
    # and SsoCallbackPage calls the real /api/v1/auth/sso/callback endpoint itself.
    assert query["redirect_uri"] == ["https://app.example.com/auth/sso/callback"]
    assert len(query["state"][0]) > 0
    assert len(query["nonce"][0]) > 0


async def test_start_requires_public_base_url(session):
    empty_settings = make_settings(
        credentials_master_key=Fernet.generate_key().decode(),
        public_base_url="",
    )
    application = create_app(empty_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        org = await _org_with_sso(client, session, empty_settings)
        r = await client.get(
            f"/api/v1/auth/sso/{org.slug}/start",
            follow_redirects=False,
        )
        assert r.status_code == 503


# --- callback rejection paths -------------------------------------------------

async def test_callback_without_code_or_state_rejected(sso_client, session):
    r = await sso_client.get("/api/v1/auth/sso/callback")
    assert r.status_code == 401
    assert await _snapshot_counts(session) == {
        "users": 0,
        "sessions": 0,
        "memberships": 0,
    }


async def test_callback_with_idp_error_param_rejected(sso_client, session):
    r = await sso_client.get(
        "/api/v1/auth/sso/callback",
        params={"error": "access_denied", "code": "x", "state": "y"},
    )
    assert r.status_code == 401
    assert await _snapshot_counts(session) == {
        "users": 0,
        "sessions": 0,
        "memberships": 0,
    }


async def test_callback_unknown_state_rejected(sso_client, session):
    r = await sso_client.get(
        "/api/v1/auth/sso/callback",
        params={"code": "x", "state": "unknown-state"},
    )
    assert r.status_code == 401
    assert await _snapshot_counts(session) == {
        "users": 0,
        "sessions": 0,
        "memberships": 0,
    }


async def test_callback_state_is_single_use(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    before_success = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 200, r.text
    after_success = await _snapshot_counts(session)

    assert after_success["users"] == before_success["users"] + 1
    assert after_success["sessions"] == before_success["sessions"] + 1
    assert after_success["memberships"] == before_success["memberships"] + 1

    replay = await _callback(sso_client, query["state"][0])
    assert replay.status_code == 401
    after_replay = await _snapshot_counts(session)
    assert after_replay == after_success


async def test_callback_nonce_mismatch_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce="not-the-nonce"))

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_bad_signature_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(
        _make_id_token(
            email,
            nonce=query["nonce"][0],
            private_pem=_BAD_PRIVATE_PEM,
        )
    )

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_alg_none_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    token = jwt.encode(_id_token_claims(email, nonce=query["nonce"][0]), None, algorithm="none")
    _set_id_token(token)

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_hs256_signed_with_public_key_rejected(
    sso_client, session, app_settings
):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    # PyJWT deliberately refuses to sign with an asymmetric PEM as an HMAC secret, so
    # the attack token is assembled by hand: header+payload signed with HMAC-SHA256
    # keyed by the PUBLIC key the server can fetch from the JWKS. A server that honours
    # the token's own `alg` would verify this happily - ours must not.
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = _b64url(json.dumps(_id_token_claims(email, nonce=query["nonce"][0])).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(
        hmac.new(_PUBLIC_PEM, signing_input, hashlib.sha256).digest()
    )
    _set_id_token(f"{header}.{payload}.{signature}")

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_wrong_issuer_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(
        _make_id_token(
            email,
            nonce=query["nonce"][0],
            issuer="https://evil.example.test",
        )
    )

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_wrong_audience_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(
        _make_id_token(
            email,
            nonce=query["nonce"][0],
            audience="another-client",
        )
    )

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_expired_id_token_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(
        _make_id_token(
            email,
            nonce=query["nonce"][0],
            exp_delta_hours=-1,
        )
    )

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_unverified_email_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(
        _make_id_token(
            email,
            nonce=query["nonce"][0],
            email_verified=False,
        )
    )

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 401
    assert await _snapshot_counts(session) == before


async def test_callback_domain_mismatch_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings, domain="corp.example.com")
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@other.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    before = await _snapshot_counts(session)
    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "sso_domain_mismatch"

    after = await _snapshot_counts(session)
    assert after["users"] == before["users"]
    assert after["memberships"] == before["memberships"]
    assert await _count(session, User, email=email) == 0

    events = list(
        (
            await session.execute(
                sa.select(LoginEvent).where(
                    LoginEvent.org_id == org.id,
                    LoginEvent.detail == "sso_domain_mismatch",
                    LoginEvent.email == email,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


# --- callback success ---------------------------------------------------------

async def test_callback_autoprovisions_member_with_agent_role(
    sso_client, session, app_settings
):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 200, r.text
    body = r.json()
    access_token = body["access_token"]
    assert access_token

    await session.commit()

    user = (
        await session.execute(sa.select(User).where(User.email == email))
    ).scalar_one()
    set_org_context(session, org.id)
    membership = (
        await session.execute(
            sa.select(OrgMembership).where(
                OrgMembership.org_id == org.id,
                OrgMembership.user_id == user.id,
            )
        )
    ).scalar_one()
    role = await session.get(Role, membership.role_id)
    assert role.name == "agent"

    # .scalar_one() IS the assertion: it raises unless exactly one row exists.
    _identity_session = (
        await session.execute(
            sa.select(IdentitySession).where(
                IdentitySession.user_id == user.id,
                IdentitySession.org_id == org.id,
            )
        )
    ).scalar_one()

    _event = (
        await session.execute(
            sa.select(LoginEvent).where(
                LoginEvent.user_id == user.id,
                LoginEvent.org_id == org.id,
                LoginEvent.outcome == "sso",
            )
        )
    ).scalar_one()

    me = await sso_client.get("/api/v1/auth/me", headers=auth_headers(access_token))
    assert me.status_code == 200, me.text


async def test_callback_existing_member_not_duplicated(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"

    for _ in range(2):
        _location, query = await _start(sso_client, org.slug)
        _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))
        r = await _callback(sso_client, query["state"][0])
        assert r.status_code == 200, r.text
        await session.commit()

    user = (
        await session.execute(sa.select(User).where(User.email == email))
    ).scalar_one()
    assert await _count(session, User, email=email) == 1
    assert await _count(
        session, OrgMembership, user_id=user.id, org_id=org.id
    ) == 1
    assert await _count(
        session, IdentitySession, user_id=user.id, org_id=org.id
    ) == 2


async def test_callback_uses_default_role_id_when_set(
    sso_client, session, app_settings
):
    org = await _org_with_sso(sso_client, session, app_settings)
    admin_role = await _get_role(session, org, "admin")
    # Assign a NEW dict: mutating the JSON column in place leaves SQLAlchemy unaware
    # and the change is silently never written.
    org.sso = {**org.sso, "default_role_id": str(admin_role.id)}
    await session.commit()

    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 200, r.text

    await session.commit()
    user = (
        await session.execute(sa.select(User).where(User.email == email))
    ).scalar_one()
    set_org_context(session, org.id)
    membership = (
        await session.execute(
            sa.select(OrgMembership).where(
                OrgMembership.org_id == org.id,
                OrgMembership.user_id == user.id,
            )
        )
    ).scalar_one()
    assert membership.role_id == admin_role.id


async def test_callback_inactive_user_rejected(sso_client, session, app_settings):
    org = await _org_with_sso(sso_client, session, app_settings)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    inactive_user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password("inactive-password"),
        full_name="Inactive",
        is_active=False,
    )
    session.add(inactive_user)
    await session.commit()

    _location, query = await _start(sso_client, org.slug)
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 403, r.text
    assert await _count(session, IdentitySession, user_id=inactive_user.id) == 0
    assert await _count(
        session, OrgMembership, user_id=inactive_user.id, org_id=org.id
    ) == 0


async def test_provisioned_user_cannot_password_login(
    sso_client, session, app_settings
):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 200, r.text

    for password in ("", "password", email):
        login = await sso_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login.status_code == 401, login.text


async def test_client_secret_never_appears_in_any_response(
    sso_client, session, app_settings
):
    org = await _org_with_sso(sso_client, session, app_settings)
    location, query = await _start(sso_client, org.slug)
    assert "s3cr3t" not in location

    failed = await sso_client.get(
        "/api/v1/auth/sso/callback",
        params={"state": query["state"][0]},
    )
    assert failed.status_code == 401
    assert "s3cr3t" not in failed.text

    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))
    ok = await _callback(sso_client, query["state"][0])
    assert ok.status_code == 200, ok.text
    assert "s3cr3t" not in ok.text


async def test_token_exchange_sends_secret_in_auth_header_not_query(
    sso_client, session, app_settings
):
    org = await _org_with_sso(sso_client, session, app_settings)
    _location, query = await _start(sso_client, org.slug)
    email = f"person-{uuid.uuid4().hex}@corp.example.com"
    _set_id_token(_make_id_token(email, nonce=query["nonce"][0]))

    r = await _callback(sso_client, query["state"][0])
    assert r.status_code == 200, r.text

    token_posts = [
        req
        for req in _IDP_STATE["requests"]
        if req.method == "POST" and str(req.url) == TOKEN_ENDPOINT
    ]
    assert len(token_posts) == 1
    post = token_posts[0]
    assert "s3cr3t" not in str(post.url)
    assert post.headers.get("authorization", "").startswith("Basic ")


# --- enforcement ---------------------------------------------------------------

async def test_password_login_blocked_when_sso_enforced(
    sso_client, session, app_settings
):
    org = await _org_with_sso(
        sso_client,
        session,
        app_settings,
        enforce=True,
    )
    email = f"member-{uuid.uuid4().hex}@corp.example.com"
    password = "correct-horse-battery"
    member = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password(password),
        full_name="Member",
        is_active=True,
    )
    session.add(member)
    await session.commit()

    role = await _get_role(session, org, "agent")
    set_org_context(session, org.id)
    session.add(OrgMembership(org_id=org.id, user_id=member.id, role_id=role.id))
    await session.commit()

    r = await sso_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "sso_required"


async def test_owner_exempt_from_sso_enforce(sso_client, session, app_settings):
    org, owner_email = await _create_owner_org(sso_client, session)
    org.sso = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret_encrypted": credentials_svc.encrypt(
            app_settings, {"client_secret": "s3cr3t"}
        ),
        "domain": "corp.example.com",
        "enforce": True,
    }
    await session.commit()

    r = await sso_client.post(
        "/api/v1/auth/login",
        json={"email": owner_email, "password": "correct-horse-battery"},
    )
    assert r.status_code == 200, r.text


async def test_enforce_does_not_block_other_domains(
    sso_client, session, app_settings
):
    org = await _org_with_sso(
        sso_client,
        session,
        app_settings,
        enforce=True,
        domain="corp.example.com",
    )
    email = f"member-{uuid.uuid4().hex}@other.example.com"
    password = "correct-horse-battery"
    member = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password(password),
        full_name="Member",
        is_active=True,
    )
    session.add(member)
    await session.commit()

    role = await _get_role(session, org, "agent")
    set_org_context(session, org.id)
    session.add(OrgMembership(org_id=org.id, user_id=member.id, role_id=role.id))
    await session.commit()

    r = await sso_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
