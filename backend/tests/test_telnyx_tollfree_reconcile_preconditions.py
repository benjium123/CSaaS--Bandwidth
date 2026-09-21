"""Precondition tests for ``reconcile_tollfree_filing_with_telnyx``.

Every case here proves the reconciler refuses invalid LOCAL state BEFORE it can make a
carrier request: the injected ``MockTransport`` handler fails the test if it is ever
called, so a leaked precondition surfaces as a test failure rather than a silent HTTP
call. The durable ``carrier_refs`` and ``status`` must be left exactly as they were
found - reconciliation never repairs an unreconcilable record.

The database is a real in-memory async SQLite engine with every model registered, under a
tenant context, mirroring how the service runs in production (row locking is a
production-only guarantee: SQLite ignores ``FOR UPDATE``).
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models
from app.db.base import Base, allow_unscoped, set_org_context
from app.errors import ConflictError, ValidationFailedError
from app.models.messaging import OrgNumber
from app.models.numbers import TollFreeVerification
from app.services.telnyx_tollfree_filing import reconcile_tollfree_filing_with_telnyx

pytestmark = pytest.mark.asyncio

SETTINGS = SimpleNamespace(telnyx_api_key="test-key")


def _register_all_models() -> None:
    """Import every module under ``app.models`` so ``create_all`` sees all tables."""
    for info in pkgutil.iter_modules(app.models.__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"app.models.{info.name}")


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    _register_all_models()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "a carrier request was made before the local preconditions passed: "
            f"{request.url}"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        yield c


def _marker(**overrides: object) -> dict:
    marker: dict = {
        "status": "pending",
        "attempt_id": str(uuid.uuid4()),
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    marker.update(overrides)
    return marker


async def _seed(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    status: str = "draft",
    refs: dict | None = None,
    carrier: str = "telnyx",
    number_type: str = "tollfree",
    is_active: bool = True,
    number_status: str = "active",
    e164: str = "+18005550123",
    number_org_id: uuid.UUID | None = None,
) -> tuple[OrgNumber, TollFreeVerification]:
    number = OrgNumber(
        org_id=number_org_id or org_id,
        e164=e164,
        carrier=carrier,
        is_active=is_active,
        number_type=number_type,
        status=number_status,
    )
    session.add(number)
    await session.flush()
    verification = TollFreeVerification(
        org_id=org_id,
        number_id=number.id,
        business_name="Acme Co",
        status=status,
        carrier_refs={} if refs is None else refs,
    )
    session.add(verification)
    await session.commit()
    return number, verification


async def _assert_refused(
    session: AsyncSession,
    client: httpx.AsyncClient,
    verification: TollFreeVerification,
    error: type[Exception],
    match: str,
) -> None:
    """Run reconciliation, require the refusal and prove durable state is untouched."""
    expected_refs = dict(verification.carrier_refs or {})
    expected_status = verification.status
    with pytest.raises(error, match=match):
        await reconcile_tollfree_filing_with_telnyx(
            session, SETTINGS, verification, client=client
        )
    reloaded = (
        await session.execute(
            sa.select(TollFreeVerification)
            .where(TollFreeVerification.id == verification.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reloaded.carrier_refs == expected_refs
    assert reloaded.status == expected_status


async def test_refuses_when_no_attempt_marker(session, client):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    _, verification = await _seed(session, org_id)
    await _assert_refused(
        session, client, verification, ConflictError,
        "No Telnyx toll-free filing attempt marker",
    )


async def test_refuses_when_marker_not_pending(session, client):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    _, verification = await _seed(
        session, org_id, refs={"telnyx_tfv_filing": _marker(status="sent")}
    )
    await _assert_refused(session, client, verification, ConflictError, "not pending")


@pytest.mark.parametrize(
    ("marker", "error", "match"),
    [
        pytest.param(
            {"status": "pending", "attempted_at": "2024-01-01T00:00:00+00:00"},
            ConflictError,
            "incomplete",
            id="missing-attempt-id",
        ),
        pytest.param(
            {"status": "pending", "attempt_id": "  ", "attempted_at": "2024-01-01T00:00:00+00:00"},
            ConflictError,
            "incomplete",
            id="blank-attempt-id",
        ),
        pytest.param(
            {"status": "pending", "attempt_id": "a"},
            ConflictError,
            "incomplete",
            id="missing-attempted-at",
        ),
        pytest.param(
            {"status": "pending", "attempt_id": "a", "attempted_at": "not-a-timestamp"},
            ValidationFailedError,
            "non-ISO",
            id="invalid-attempted-at",
        ),
        pytest.param(
            {"status": "pending", "attempt_id": "a", "attempted_at": "2024-01-01T00:00:00"},
            ValidationFailedError,
            "timezone-aware",
            id="naive-attempted-at",
        ),
    ],
)
async def test_refuses_corrupt_attempt_marker(session, client, marker, error, match):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    _, verification = await _seed(session, org_id, refs={"telnyx_tfv_filing": marker})
    await _assert_refused(session, client, verification, error, match)


async def test_refuses_when_request_id_already_recorded(session, client):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    _, verification = await _seed(
        session,
        org_id,
        refs={"telnyx": "req_123", "telnyx_tfv_filing": _marker()},
    )
    await _assert_refused(
        session, client, verification, ConflictError, "already recorded"
    )


@pytest.mark.parametrize("status", ["approved", "rejected"])
async def test_refuses_decided_verification(session, client, status):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    _, verification = await _seed(
        session, org_id, status=status, refs={"telnyx_tfv_filing": _marker()}
    )
    await _assert_refused(
        session, client, verification, ConflictError, f"already {status}"
    )


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        pytest.param(
            {"carrier": "bandwidth"},
            ValidationFailedError,
            "only be filed with Telnyx",
            id="non-telnyx-carrier",
        ),
        pytest.param(
            {"number_type": "local"},
            ValidationFailedError,
            "toll-free number",
            id="non-toll-free-number",
        ),
        pytest.param(
            {"is_active": False},
            ConflictError,
            "not active",
            id="inactive-number",
        ),
        pytest.param(
            {"number_status": "released"},
            ConflictError,
            "not active",
            id="released-number",
        ),
    ],
)
async def test_refuses_unusable_number(session, client, kwargs, error, match):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    _, verification = await _seed(
        session, org_id, refs={"telnyx_tfv_filing": _marker()}, **kwargs
    )
    await _assert_refused(session, client, verification, error, match)


async def test_refuses_cross_org_number_before_any_carrier_call(session, client):
    org_id = uuid.uuid4()
    set_org_context(session, org_id)
    allow_unscoped(session, True)
    try:
        _, verification = await _seed(
            session,
            org_id,
            refs={"telnyx_tfv_filing": _marker()},
            number_org_id=uuid.uuid4(),
        )
        await _assert_refused(
            session, client, verification, ConflictError, "different orgs"
        )
    finally:
        allow_unscoped(session, False)
