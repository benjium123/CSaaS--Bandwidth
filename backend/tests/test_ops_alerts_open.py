"""Ops alerts say who they are about, so the console can open the right workspace."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.models import Org, OrgMembership, SecurityAlert, User
from tests.conftest import auth_headers, register_and_login
from tests.test_ops_console import _operator, ops, ops_settings  # noqa: F401


async def test_alerts_carry_workspace_and_person(ops, session):  # noqa: F811
    token = await _operator(ops, session, "alerts-op@example.com")
    org = Org(id=uuid.uuid4(), name="Alert Org", slug=f"al-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()

    await register_and_login(ops, "flagged@example.com")
    user = (
        await session.execute(
            sa.select(User).where(User.email == "flagged@example.com").execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    member_org = (
        await session.execute(
            sa.select(OrgMembership.org_id)
            .where(OrgMembership.user_id == user.id)
            .execution_options(allow_unscoped=True)
        )
    ).scalars().first()

    by_org = SecurityAlert(id=uuid.uuid4(), kind="card_country_mismatch", org_id=org.id, detail={})
    by_user = SecurityAlert(id=uuid.uuid4(), kind="flagged_login", user_id=user.id, detail={})
    system = SecurityAlert(id=uuid.uuid4(), kind="monitor_health", detail={})
    session.add_all([by_org, by_user, system])
    await session.commit()

    r = await ops.get("/api/v1/ops/alerts", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    rows = {a["id"]: a for a in r.json()}

    assert rows[str(by_org.id)]["org_id"] == str(org.id)
    assert rows[str(by_org.id)]["org_name"] == "Alert Org"

    assert rows[str(by_user.id)]["user_email"] == "flagged@example.com"
    assert rows[str(by_user.id)]["org_id"] == (str(member_org) if member_org else None)

    assert rows[str(system.id)]["org_id"] is None
    assert rows[str(system.id)]["user_email"] is None
