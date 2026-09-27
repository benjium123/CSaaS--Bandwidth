"""H3: an operator's read-only, time-boxed, logged view of a customer workspace."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.models import AccountAuditEntry, OperatorAuditEntry, OperatorViewSession
from app.services import mailer
from tests.conftest import auth_headers, create_contact, create_org, register_and_login
from tests.test_ops_console import _operator, ops, ops_settings  # noqa: F401

WHY = {"X-Ops-Reason": "owner reported missing messages"}


async def _workspace(client):
    token = await register_and_login(client, "owner-viewed@example.com")
    org = await create_org(client, token, "Viewed Co")
    await create_contact(client, token, org["id"], "Ada Lovelace", ["+12145550111"])
    return org


async def _start(client, token, org_id):
    return await client.post(
        "/api/v1/ops/view-as", json={"org_id": org_id}, headers={**auth_headers(token), **WHY}
    )


def _as(token, org_id, view_id):
    return {**auth_headers(token, org_id), "X-View-As": str(view_id)}


async def test_support_sees_the_workspace_read_only_and_every_request_is_logged(ops, session):  # noqa: F811
    org = await _workspace(ops)
    token = await _operator(ops, session, "support-view@example.com", role="support")
    mailer.outbox.clear()

    r = await _start(ops, token, org["id"])
    assert r.status_code == 201, r.text
    view = r.json()
    h = _as(token, org["id"], view["id"])

    listed = await ops.get("/api/v1/contacts", headers=h)
    assert listed.status_code == 200, listed.text
    assert [c["display_name"] for c in listed.json()] == ["Ada Lovelace"]

    write = await ops.post(
        "/api/v1/contacts", json={"display_name": "Nope", "phones": []}, headers=h
    )
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "view_as_read_only"

    session.expire_all()
    rows = (
        await session.execute(
            sa.select(OperatorAuditEntry).where(OperatorAuditEntry.org_id == uuid.UUID(org["id"]))
        )
    ).scalars().all()
    routes = sorted((r.method, r.route, r.status_code) for r in rows)
    assert ("GET", "/api/v1/contacts", 200) in routes
    assert ("POST", "/api/v1/contacts", 403) in routes
    assert ("POST", "/api/v1/ops/view-as", 201) not in routes  # no org_id path param

    # The owner is told: an email and an entry in their account activity.
    [msg] = [m for m in mailer.outbox if "owner-viewed@example.com" in m["To"]]
    assert "owner reported missing messages" in msg.get_body(("plain",)).get_content()
    activity = (
        await session.execute(
            sa.select(AccountAuditEntry).where(
                AccountAuditEntry.action == "support.viewed_workspace"
            )
        )
    ).scalars().all()
    assert len(activity) == 1 and activity[0].detail["reason"] == WHY["X-Ops-Reason"]


async def test_starting_a_view_needs_a_reason_and_the_support_permission(ops, session):  # noqa: F811
    org = await _workspace(ops)
    support = await _operator(ops, session, "support-noreason@example.com", role="support")
    r = await ops.post(
        "/api/v1/ops/view-as", json={"org_id": org["id"]}, headers=auth_headers(support)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ops_reason_required"

    billing = await _operator(ops, session, "billing-view@example.com", role="billing")
    assert (await _start(ops, billing, org["id"])).status_code == 403


async def test_an_expired_ended_or_borrowed_view_is_refused(ops, session):  # noqa: F811
    org = await _workspace(ops)
    token = await _operator(ops, session, "support-expire@example.com", role="support")
    other = await _operator(ops, session, "support-other@example.com", role="support")
    view = (await _start(ops, token, org["id"])).json()

    borrowed = await ops.get("/api/v1/contacts", headers=_as(other, org["id"], view["id"]))
    assert borrowed.status_code == 403
    assert borrowed.json()["error"]["code"] == "view_as_ended"

    row = await session.get(OperatorViewSession, uuid.UUID(view["id"]))
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    expired = await ops.get("/api/v1/contacts", headers=_as(token, org["id"], view["id"]))
    assert expired.json()["error"]["code"] == "view_as_ended"

    fresh = (await _start(ops, token, org["id"])).json()
    current = await ops.get("/api/v1/ops/view-as/current", headers=auth_headers(token))
    assert current.json()["view"]["id"] == fresh["id"]
    r = await ops.post(f"/api/v1/ops/view-as/{fresh['id']}/end", headers=auth_headers(token))
    assert r.status_code == 200
    ended = await ops.get("/api/v1/contacts", headers=_as(token, org["id"], fresh["id"]))
    assert ended.json()["error"]["code"] == "view_as_ended"
    current = await ops.get("/api/v1/ops/view-as/current", headers=auth_headers(token))
    assert current.json()["view"] is None


async def test_a_view_does_not_work_for_another_workspace(ops, session):  # noqa: F811
    org = await _workspace(ops)
    owner2 = await register_and_login(ops, "owner-two@example.com")
    org2 = await create_org(ops, owner2, "Other Co")
    token = await _operator(ops, session, "support-scope@example.com", role="support")
    view = (await _start(ops, token, org["id"])).json()
    r = await ops.get("/api/v1/contacts", headers=_as(token, org2["id"], view["id"]))
    assert r.status_code == 403
