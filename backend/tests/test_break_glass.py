"""H4: the shared ops token is for machines only; break-glass admin access is temporary and loud."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.errors import ValidationFailedError
from app.models import OperatorAuditEntry, PlatformOperator, SecurityAlert, User
from app.services import break_glass, mailer
from app.services import operators as operators_svc
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, auth_headers, make_settings, register_and_login
from tests.test_operator_roles import ROUTES
from tests.test_ops_console import _operator, ops, ops_settings  # noqa: F401


def test_the_shared_token_reaches_no_console_route():
    """Every operator route is a require_operator_permission route (no token path)."""
    assert all(perm is not None for _, _, perm, _ in ROUTES)
    platform = [(m, p) for m, p, _, _ in ROUTES if p.startswith("/api/v1/platform/")]
    assert len(platform) == 7


async def test_platform_routes_refuse_the_token(ops):  # noqa: F811
    r = await ops.get(
        "/api/v1/platform/messaging/health",
        headers={"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN},
    )
    assert r.status_code in (401, 403)


async def test_break_glass_is_temporary_audited_alerted_and_mailed(ops, session):  # noqa: F811
    await _operator(ops, session, "boss@example.com")  # an existing admin gets the email
    await register_and_login(ops, "rescuer@example.com")
    mailer.outbox.clear()

    row = await break_glass.grant(
        session,
        make_settings(),
        email="rescuer@example.com",
        reason="all admins lost their passkeys",
        minutes=30,
        granted_by="root@box",
    )
    user = (
        await session.execute(sa.select(User).where(User.email == "rescuer@example.com"))
    ).scalar_one()
    assert (await operators_svc.get_active(session, user.id)).role == "admin"
    assert row.expires_at is not None

    audit = (
        await session.execute(
            sa.select(OperatorAuditEntry).where(OperatorAuditEntry.operator_role == "break_glass")
        )
    ).scalar_one()
    assert audit.reason == "all admins lost their passkeys"
    alert = (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == "break_glass"))
    ).scalar_one()
    assert alert.user_id == user.id
    [msg] = mailer.outbox
    assert "boss@example.com" in msg["To"] and "rescuer@example.com" not in msg["To"]

    # It ends by itself.
    stored = await session.get(PlatformOperator, row.id)
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    assert await operators_svc.get_active(session, user.id) is None


async def test_break_glass_is_capped_and_refuses_weak_or_pointless_grants(ops, session):  # noqa: F811
    await register_and_login(ops, "capped@example.com")
    row = await break_glass.grant(
        session, make_settings(), email="capped@example.com", reason="restore console access",
        minutes=600,
    )
    assert row.expires_at - datetime.now(timezone.utc) <= timedelta(hours=1, seconds=5)

    with pytest.raises(ValidationFailedError):  # already an active operator
        await break_glass.grant(
            session, make_settings(), email="capped@example.com", reason="restore console access"
        )
    await register_and_login(ops, "weak@example.com")
    with pytest.raises(ValidationFailedError):
        await break_glass.grant(session, make_settings(), email="weak@example.com", reason="x")


async def test_a_normal_grant_clears_an_expiry(ops, session):  # noqa: F811
    await register_and_login(ops, "promoted@example.com")
    await break_glass.grant(
        session, make_settings(), email="promoted@example.com", reason="temporary cover for ops"
    )
    row = await operators_svc.grant(session, email="promoted@example.com", role="support")
    await session.commit()
    assert row.expires_at is None


async def test_the_console_refuses_an_expired_break_glass_admin(ops, session):  # noqa: F811
    token = await _operator(ops, session, "expired-bg@example.com")
    op = (
        await session.execute(
            sa.select(PlatformOperator)
            .join(User, User.id == PlatformOperator.user_id)
            .where(User.email == "expired-bg@example.com")
        )
    ).scalar_one()
    op.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    r = await ops.get("/api/v1/ops/console/prices", headers=auth_headers(token))
    assert r.status_code == 403
