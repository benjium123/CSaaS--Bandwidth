"""H1: every change a platform operator makes lands in operator_audit_log (reads do not)."""

from __future__ import annotations

import sqlalchemy as sa

from app.models import OperatorAuditEntry
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, auth_headers, register_and_login
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401


async def _rows(session):
    session.expire_all()
    return list(
        (await session.execute(sa.select(OperatorAuditEntry).order_by(OperatorAuditEntry.at)))
        .scalars()
        .all()
    )


async def test_an_operator_change_is_recorded_with_route_org_status_and_reason(ops, session):  # noqa: F811
    token = await _operator(ops, session, "audit-admin@example.com")
    org_id = await _new_org(session, "Audited Org")
    h = auth_headers(token)

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/fax",
        json={"enabled": False},
        headers={**h, "X-Ops-Reason": "customer asked to switch fax off"},
    )
    assert r.status_code == 200, r.text
    # A read is not a change.
    read = await ops.get(f"/api/v1/ops/console/orgs/{org_id}/features", headers=h)
    assert read.status_code == 200

    [row] = await _rows(session)
    assert row.operator_email == "audit-admin@example.com"
    assert row.operator_role == "admin"
    assert row.method == "PUT"
    assert row.route == "/api/v1/ops/console/orgs/{org_id}/features/{key}"
    assert row.path_params == {"org_id": str(org_id), "key": "fax"}
    assert row.org_id == org_id
    assert row.status_code == 200
    assert row.reason == "customer asked to switch fax off"

    listed = await ops.get(f"/api/v1/ops/console/audit?org_id={org_id}", headers=h)
    assert listed.status_code == 200, listed.text
    [entry] = listed.json()["entries"]
    assert entry["route"] == row.route and entry["reason"] == row.reason


async def test_a_refused_operator_change_is_recorded_too(ops, session):  # noqa: F811
    token = await _operator(ops, session, "audit-admin2@example.com")
    org_id = await _new_org(session, "Audited Org 2")
    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/not-a-feature",
        json={"enabled": True},
        headers=auth_headers(token),
    )
    assert r.status_code >= 400
    [row] = await _rows(session)
    assert row.status_code == r.status_code


async def test_non_operators_leave_no_rows_and_cannot_read_the_log(ops, session):  # noqa: F811
    token = await register_and_login(ops, "not-an-operator@example.com")
    org_id = await _new_org(session, "Audited Org 3")
    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/fax",
        json={"enabled": False},
        headers=auth_headers(token),
    )
    assert r.status_code == 403
    listed = await ops.get("/api/v1/ops/console/audit", headers=auth_headers(token))
    assert listed.status_code == 403
    assert await _rows(session) == []


async def test_reviewers_cannot_read_the_log(ops, session):  # noqa: F811
    token = await _operator(ops, session, "audit-reviewer@example.com", role="reviewer")
    r = await ops.get("/api/v1/ops/console/audit", headers=auth_headers(token))
    assert r.status_code == 403


async def test_the_shared_ops_token_is_recorded_without_a_person(ops, session):  # noqa: F811
    r = await ops.put(
        "/api/v1/platform/billing/rates",
        json={},
        headers={"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN},
    )
    rows = await _rows(session)
    assert [(x.operator_role, x.operator_user_id, x.status_code) for x in rows] == [
        ("ops_token", None, r.status_code)
    ]
