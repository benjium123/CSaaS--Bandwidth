from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

from app.auth.security import create_access_token, decode_access_token
from app.errors import UnauthenticatedError
from app.main import create_app
from app.models.identity import LOGIN_OUTCOMES
from app.services import identity as identity_svc
from tests.conftest import TEST_JWT_SECRET, make_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
P25_MODULES = [
    "app/services/identity.py",
    "app/services/oidc.py",
    "app/services/session_cache.py",
    "app/api/routes/identity.py",
    "app/api/routes/sso.py",
]


def _router_paths(router) -> list[str]:
    prefix = str(router.prefix or "")
    paths = []
    for route in router.routes:
        path = str(route.path)
        if path.startswith(prefix):
            paths.append(path)
        else:
            paths.append(prefix + path)
    return paths


def test_p25_routes_registered_once():
    """Every P25 path is mounted exactly once, with the methods the handoff specifies.

    Read off the OpenAPI document rather than app.routes: this FastAPI version keeps
    included routers nested, so app.routes holds router objects, not endpoints.
    """
    app = create_app(make_settings())
    paths = app.openapi()["paths"]

    expected = {
        "/api/v1/me/sessions": {"get"},
        "/api/v1/me/sessions/{sid}": {"delete"},
        "/api/v1/me/sessions/revoke-all": {"post"},
        "/api/v1/me/login-events": {"get"},
        "/api/v1/orgs/current/members/{user_id}/sessions": {"delete"},
        "/api/v1/orgs/current/login-events": {"get"},
        "/api/v1/orgs/current/security": {"get", "patch"},
        "/api/v1/auth/sso/{org_slug}/start": {"get"},
        "/api/v1/auth/sso/callback": {"get"},
    }

    for path, methods in expected.items():
        assert path in paths, f"{path} is not mounted"
        assert set(paths[path]) == methods, (path, sorted(paths[path]))

    # The pre-existing /api/v1/me routes must survive sharing the prefix.
    assert "/api/v1/me/capabilities" in paths


def _has_bare_func_count(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "count"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "func"
            ):
                return True
    return False


def test_no_bare_count_in_p25_modules():
    for rel in P25_MODULES:
        source = (BACKEND_ROOT / rel).read_text()
        assert not _has_bare_func_count(source), rel


def test_no_new_third_party_imports():
    disallowed = {"authlib", "joserfc", "requests", "redis"}

    for rel in P25_MODULES:
        tree = ast.parse((BACKEND_ROOT / rel).read_text())
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in disallowed, rel
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    assert root not in disallowed, rel


def _jwt_with_claims(uid: uuid.UUID, secret: str, **overrides) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uid),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_decode_access_token_contract():
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    secret = TEST_JWT_SECRET

    token = create_access_token(uid, secret)
    assert decode_access_token(token, secret) == (uid, None)

    token = create_access_token(uid, secret, sid=sid)
    assert decode_access_token(token, secret) == (uid, sid)

    with pytest.raises(UnauthenticatedError):
        decode_access_token(_jwt_with_claims(uid, secret, sid="garbage"), secret)

    with pytest.raises(UnauthenticatedError):
        decode_access_token(_jwt_with_claims(uid, secret, scope="openid"), secret)


def test_login_outcomes_are_the_committed_set():
    assert LOGIN_OUTCOMES == (
        "ok",
        "bad_password",
        "bad_2fa",
        "locked",
        "sso",
        "blocked_ip",
        "revoked",
    )


def test_ip_in_allowlist_matrix():
    cases = [
        ([], "192.0.2.1", True),
        (["10.0.0.0/8"], "10.1.2.3", True),
        (["10.0.0.0/8"], "11.0.0.1", False),
        (["10.0.0.0/8"], None, False),
        (["10.0.0.0/8"], "not-an-ip", False),
        (["2001:db8::/32"], "2001:db8::1", True),
        (["2001:db8::/32"], "2001:db9::1", False),
    ]
    for allowlist, ip, expected in cases:
        assert identity_svc.ip_in_allowlist(ip, allowlist) is expected


def test_csv_response_escapes_injection():
    rows = [
        {"col": "=cmd"},
        {"col": "+sum"},
        {"col": "-1"},
        {"col": "@x"},
        {"col": "ordinary"},
    ]
    response = identity_svc.csv_response(rows, ["col"], "test.csv")
    body = response.body.decode()

    assert "'=cmd" in body
    assert "'+sum" in body
    assert "'-1" in body
    assert "'@x" in body
    assert "\nordinary" in body
