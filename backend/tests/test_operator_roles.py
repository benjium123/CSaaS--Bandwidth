"""H2: operator roles as permissions, and major actions that only a person may take."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.main import create_app
from app.models import Session as IdentitySession
from app.models import User
from app.services import operators as operators_svc
from app.services.telephony_billing import PLATFORM_PRICE_MICROS
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, auth_headers, make_settings
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401

REASON = {"X-Ops-Reason": "owner asked us to switch fax off"}


def _operator_routes() -> list[tuple[str, str, str | None, bool]]:
    """(method, path, permission, major) for every route behind an operator guard."""
    out = []

    def deps_of(dep, acc):
        for sub in dep.dependencies:
            acc.append(sub.call)
            deps_of(sub, acc)

    def walk(routes, prefix):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes, prefix + route.include_context.prefix)
                continue
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            calls: list = []
            deps_of(dependant, calls)
            for call in calls:
                name = getattr(call, "__qualname__", "")
                if name.startswith(("require_operator_permission.", "require_operator.")):
                    free = inspect.getclosurevars(call).nonlocals
                    for method in route.methods - {"HEAD", "OPTIONS"}:
                        out.append(
                            (
                                method,
                                prefix + route.path,
                                free.get("permission"),
                                bool(free.get("major", False)),
                            )
                        )

    walk(create_app(make_settings()).routes, "")
    return out


ROUTES = _operator_routes()


def test_reviewer_and_admin_keep_what_they_had_and_new_roles_are_narrow():
    perms = operators_svc.ROLE_PERMISSIONS
    assert perms["admin"] == frozenset(operators_svc.OPS_PERMISSIONS)
    assert perms["reviewer"] == {"ops:read", "ops:kyc", "ops:site"}
    assert perms["read_only"] == {"ops:read"}
    assert "ops:admin" not in perms["support"] | perms["billing"]


def test_every_operator_route_is_guarded_by_a_permission():
    assert len(ROUTES) >= 83
    legacy = [(m, p) for m, p, perm, _ in ROUTES if perm is None]
    assert legacy == [], "route still on the old role guard"
    reads_that_change = [(m, p) for m, p, perm, _ in ROUTES if m != "GET" and perm == "ops:read"]
    assert reads_that_change == []


def test_the_major_actions_are_marked():
    major = {(m, p) for m, p, _, is_major in ROUTES if is_major}
    for expected in [
        ("POST", "/api/v1/ops/applications/{org_id}/suspend"),
        ("POST", "/api/v1/ops/customer-accounts/{user_id}/delete"),
        ("POST", "/api/v1/ops/monitoring/orgs/{org_id}/suspend"),
        ("POST", "/api/v1/ops/monitoring/orgs/{org_id}/unpause"),
        ("PUT", "/api/v1/ops/console/orgs/{org_id}/features/{key}"),
        ("POST", "/api/v1/ops/ban-list"),
        ("POST", "/api/v1/ops/users/{user_id}/deactivate"),
    ]:
        assert expected in major
    assert all(perm == "ops:admin" for _, _, perm, is_major in ROUTES if is_major)


async def test_read_only_reads_but_changes_nothing(ops, session):  # noqa: F811
    token = await _operator(ops, session, "ro@example.com", role="read_only")
    h = auth_headers(token)
    assert (await ops.get("/api/v1/ops/console/prices", headers=h)).status_code == 200
    r = await ops.put(
        "/api/v1/ops/console/prices/sms_segment", json={"price_micros": 1}, headers=h
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "operator_permission_denied"


async def test_billing_changes_prices_but_not_features(ops, session):  # noqa: F811
    token = await _operator(ops, session, "billing@example.com", role="billing")
    h = auth_headers(token)
    metric = next(iter(PLATFORM_PRICE_MICROS))
    r = await ops.put(
        f"/api/v1/ops/console/prices/{metric}", json={"price_micros": 1234}, headers=h
    )
    assert r.status_code == 200, r.text
    org_id = await _new_org(session, "Billing Only")
    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/fax",
        json={"enabled": False},
        headers={**h, **REASON},
    )
    assert r.status_code == 403


async def test_a_major_action_needs_a_reason_and_a_fresh_second_factor(ops, session):  # noqa: F811
    token = await _operator(ops, session, "major@example.com")
    org_id = await _new_org(session, "Major Co")
    path = f"/api/v1/ops/console/orgs/{org_id}/features/fax"
    h = auth_headers(token)

    r = await ops.put(path, json={"enabled": False}, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ops_reason_required"

    # A second factor proved an hour ago is not fresh enough for a major action.
    user = (
        await session.execute(
            sa.select(User)
            .where(User.email == "major@example.com")
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    live = (
        await session.execute(
            sa.select(IdentitySession).where(IdentitySession.user_id == user.id)
        )
    ).scalars().all()
    for row in live:
        row.second_factor_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    r = await ops.put(path, json={"enabled": False}, headers={**h, **REASON})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "step_up_required"

    for row in live:
        row.second_factor_at = datetime.now(timezone.utc)
    await session.commit()
    r = await ops.put(path, json={"enabled": False}, headers={**h, **REASON})
    assert r.status_code == 200, r.text


async def test_the_shared_token_never_reaches_a_major_action(ops, session):  # noqa: F811
    org_id = await _new_org(session, "Token Co")
    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/fax",
        json={"enabled": False},
        headers={"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN, **REASON},
    )
    assert r.status_code in (401, 403)


async def test_me_lists_the_operator_permissions(ops, session):  # noqa: F811
    token = await _operator(ops, session, "support@example.com", role="support")
    me = (await ops.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert me["operator_role"] == "support"
    assert me["operator_permissions"] == ["ops:read", "ops:site", "ops:support"]
