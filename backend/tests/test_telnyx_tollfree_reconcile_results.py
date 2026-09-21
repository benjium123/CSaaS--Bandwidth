"""Failure-matrix coverage for Telnyx toll-free filing reconciliation.

``reconcile_tollfree_filing_with_telnyx`` may adopt a carrier request id only when its ONE
read-only list call returns exactly one record matching this filing. Everything else - zero
or many matches, a page that disagrees with its own count, a missing identifier, a different
business name or phone number - must be refused with ``ConflictError``, and a timeout or
transport failure must surface as ``FeatureUnavailableError``. In every case the pending
attempt marker, the absence of a Telnyx request id and the original ``draft`` status must
stay durable in the database.

Nothing here contacts Telnyx: the transport is an injected ``httpx.MockTransport``.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models
from app.db.base import Base, set_org_context
from app.errors import ConflictError, FeatureUnavailableError
from app.models.messaging import OrgNumber
from app.models.numbers import TollFreeVerification
from app.services.telnyx_tollfree_filing import reconcile_tollfree_filing_with_telnyx

# Import every model module so Base.metadata carries the WHOLE schema - the foreign keys
# into campaigns / users / provider accounts included - before ``create_all`` runs.
for _discovered in pkgutil.iter_modules(app.models.__path__, "app.models."):
    importlib.import_module(_discovered.name)

pytestmark = pytest.mark.asyncio

NUMBER_E164 = "+18005550123"
BUSINESS_NAME = "Rocket Surgery LLC"
ATTEMPT_ID = "5f1d0f5e-0b1b-4c1e-9e4b-6b0f6d1a2c34"
ATTEMPTED_AT = "2026-02-01T12:00:00+00:00"
PENDING_MARKER = {
    "status": "pending",
    "attempt_id": ATTEMPT_ID,
    "attempted_at": ATTEMPTED_AT,
}


def _pending_refs() -> dict:
    return {"telnyx_tfv_filing": dict(PENDING_MARKER)}


def _record(**overrides: object) -> dict:
    """A carrier list record that matches this filing, unless overridden."""
    record: dict = {
        "id": "tfv-request-1",
        "businessName": BUSINESS_NAME,
        "phoneNumbers": [{"phoneNumber": NUMBER_E164}],
    }
    record.update(overrides)
    return record


#: Every list page that must be refused as ambiguous, malformed or mismatched.
CONFLICT_CASES: list[tuple[str, dict]] = [
    ("zero_matches", {"records": [], "total_records": 0}),
    (
        "multiple_matches",
        {"records": [_record(id="a"), _record(id="b")], "total_records": 2},
    ),
    ("total_exceeds_records", {"records": [_record()], "total_records": 2}),
    ("records_exceed_total", {"records": [_record()], "total_records": 0}),
    ("missing_id", {"records": [_record(id=None)], "total_records": 1}),
    ("blank_id", {"records": [_record(id="   ")], "total_records": 1}),
    (
        "business_name_mismatch",
        {"records": [_record(businessName="Someone Else Inc")], "total_records": 1},
    ),
    ("phone_numbers_mismatch", {
        "records": [_record(phoneNumbers=[{"phoneNumber": "+18005559999"}])],
        "total_records": 1,
    }),
    (
        "phone_numbers_not_a_list",
        {"records": [_record(phoneNumbers=NUMBER_E164)], "total_records": 1},
    ),
    ("phone_numbers_empty", {"records": [_record(phoneNumbers=[])], "total_records": 1}),
    (
        "phone_numbers_extra_number",
        {
            "records": [
                _record(
                    phoneNumbers=[
                        {"phoneNumber": NUMBER_E164},
                        {"phoneNumber": "+18005550000"},
                    ]
                )
            ],
            "total_records": 1,
        },
    ),
    (
        "phone_numbers_malformed_entry",
        {"records": [_record(phoneNumbers=[NUMBER_E164])], "total_records": 1},
    ),
    (
        "phone_numbers_missing_value",
        {"records": [_record(phoneNumbers=[{}])], "total_records": 1},
    ),
]


@dataclass(frozen=True)
class _Harness:
    session: AsyncSession
    maker: async_sessionmaker[AsyncSession]
    org_id: uuid.UUID
    verification_id: uuid.UUID


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[_Harness]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    try:
        async with maker() as session:
            # Tenant isolation is applied to the session itself, before any tenant write.
            set_org_context(session, org_id)
            number = OrgNumber(
                org_id=org_id,
                e164=NUMBER_E164,
                carrier="telnyx",
                is_active=True,
                number_type="tollfree",
                status="active",
            )
            session.add(number)
            await session.flush()
            verification = TollFreeVerification(
                org_id=org_id,
                number_id=number.id,
                business_name=BUSINESS_NAME,
                status="draft",
                carrier_refs=_pending_refs(),
            )
            session.add(verification)
            await session.commit()
            yield _Harness(
                session=session,
                maker=maker,
                org_id=org_id,
                verification_id=verification.id,
            )
    finally:
        await engine.dispose()


async def _pending_verification(harness: _Harness) -> TollFreeVerification:
    """Reload the ambiguous verification exactly as the caller would hand it over."""
    return (
        await harness.session.execute(
            sa.select(TollFreeVerification)
            .where(TollFreeVerification.id == harness.verification_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _reconcile(
    harness: _Harness,
    handler: Callable[[httpx.Request], httpx.Response],
    error: type[Exception],
    match: str | None = None,
) -> list[httpx.Request]:
    """Run one reconcile attempt against ``handler``, expecting ``error``; return the
    carrier calls that actually went out."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        verification = await _pending_verification(harness)
        with pytest.raises(error, match=match):
            await reconcile_tollfree_filing_with_telnyx(
                harness.session,
                SimpleNamespace(telnyx_api_key="test-key"),
                verification,
                client=client,
            )
    return calls


def _assert_one_list_get_no_post(calls: list[httpx.Request]) -> None:
    assert [call.method for call in calls] == ["GET"]
    assert calls[0].url.path.endswith("/messaging_tollfree/verification/requests")


async def _assert_state_untouched(harness: _Harness) -> None:
    """Read the row back through a FRESH session: the state must be durable, not just
    in the identity map."""
    async with harness.maker() as fresh:
        set_org_context(fresh, harness.org_id)
        row = (
            await fresh.execute(
                sa.select(TollFreeVerification).where(
                    TollFreeVerification.id == harness.verification_id
                )
            )
        ).scalar_one()
    assert row.status == "draft"
    assert "telnyx" not in row.carrier_refs
    assert row.carrier_refs["telnyx_tfv_filing"] == PENDING_MARKER


@pytest.mark.parametrize(
    ("case", "payload"), CONFLICT_CASES, ids=[case for case, _ in CONFLICT_CASES]
)
async def test_reconcile_refuses_ambiguous_carrier_results(
    harness: _Harness, case: str, payload: dict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    calls = await _reconcile(harness, handler, ConflictError)

    _assert_one_list_get_no_post(calls)
    await _assert_state_untouched(harness)


TRANSPORT_FAILURES: list[tuple[str, Callable[[], Exception], str]] = [
    ("timeout", lambda: httpx.ReadTimeout("carrier never answered"), "timed out"),
    ("unreachable", lambda: httpx.ConnectError("connection refused"), "unreachable"),
]


@pytest.mark.parametrize(
    ("case", "make_error", "fragment"),
    TRANSPORT_FAILURES,
    ids=[case for case, _, _ in TRANSPORT_FAILURES],
)
async def test_reconcile_surfaces_transport_failure(
    harness: _Harness,
    case: str,
    make_error: Callable[[], Exception],
    fragment: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise make_error()

    calls = await _reconcile(harness, handler, FeatureUnavailableError, match=fragment)

    _assert_one_list_get_no_post(calls)
    await _assert_state_untouched(harness)
