"""Tenant isolation — the P0 gate.

These tests must PROVE isolation, not inspect for it. The endpoint they exercise
(``/orgs/current/roles``) deliberately contains no ``where(org_id == ...)``; the session
guard supplies it. If the guard regresses, these tests go red rather than the product
leaking silently.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import MissingTenantContextError
from app.models import Role
from app.repositories.base import TenantRepository
from tests.conftest import auth_headers, create_org, register_and_login


async def test_unscoped_query_raises(session):
    set_org_context(session, None)
    with pytest.raises(MissingTenantContextError):
        await session.execute(sa.select(Role))


async def test_unscoped_query_allowed_with_explicit_optout(session):
    set_org_context(session, None)
    result = await session.execute(
        sa.select(Role).execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    assert result.scalars().all() == []


async def test_cross_tenant_write_rejected(session):
    from app.models import Org

    org_a = Org(id=uuid.uuid4(), name="A", slug=f"a-{uuid.uuid4().hex[:8]}")
    org_b = Org(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    session.add_all([org_a, org_b])
    await session.flush()

    set_org_context(session, org_a.id)
    session.add(Role(id=uuid.uuid4(), org_id=org_b.id, name="sneaky", permissions=[]))
    with pytest.raises(MissingTenantContextError):
        await session.flush()


async def test_org_b_cannot_read_org_a_rows(client, session):
    """THE GATE TEST."""
    token1 = await register_and_login(client, "user1@example.com")
    token2 = await register_and_login(client, "user2@example.com")

    org_a = await create_org(client, token1, "Org A")
    org_b = await create_org(client, token2, "Org B")
    assert org_a["id"] != org_b["id"]

    # 1. user2 presenting org A's id is refused at the membership check, before any query.
    r = await client.get(
        "/api/v1/orgs/current/roles", headers=auth_headers(token2, org_a["id"])
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"

    # 2. user1 sees only A's roles; user2 only B's. Zero intersection.
    ra = await client.get(
        "/api/v1/orgs/current/roles", headers=auth_headers(token1, org_a["id"])
    )
    rb = await client.get(
        "/api/v1/orgs/current/roles", headers=auth_headers(token2, org_b["id"])
    )
    assert ra.status_code == 200 and rb.status_code == 200
    a_ids = {r["id"] for r in ra.json()}
    b_ids = {r["id"] for r in rb.json()}
    assert len(a_ids) == 3 and len(b_ids) == 3  # owner, admin, agent seeded per org
    assert a_ids.isdisjoint(b_ids)

    # 3. Repository scoped to B cannot fetch one of A's rows by primary key.
    a_role_id = uuid.UUID(next(iter(a_ids)))
    repo = TenantRepository(session, uuid.UUID(org_b["id"]))
    assert await repo.get(Role, a_role_id) is None


async def test_missing_org_header_is_400(client):
    token = await register_and_login(client, "noheader@example.com")
    await create_org(client, token, "Some Org")
    r = await client.get("/api/v1/orgs/current/roles", headers=auth_headers(token))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


async def test_malformed_org_header_is_rejected(client):
    token = await register_and_login(client, "badheader@example.com")
    r = await client.get(
        "/api/v1/orgs/current/roles", headers=auth_headers(token, "not-a-uuid")
    )
    assert r.status_code == 422


@pytest.mark.pg_only
async def test_isolation_on_postgres(client, session):
    """Re-run the gate on real Postgres: proves GUID/JSONB variants and
    with_loader_criteria behave identically there."""
    await test_org_b_cannot_read_org_a_rows(client, session)
