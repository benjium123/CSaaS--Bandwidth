"""Fail-closed coverage for ``file_tollfree_verification_with_telnyx``.

Every case in this module is a SAFETY case:

* invalid payload data must fail BEFORE the durable attempt marker is written and must
  make ZERO POSTs;
* an ambiguous POST failure (timeout / transport error) must leave the
  ``carrier_refs["telnyx_tfv_filing"]`` marker durable, must NOT store
  ``carrier_refs["telnyx"]``, and must preserve the prior local status - and the marker
  must then refuse an immediate retry;
* a repeat call where either the marker or the Telnyx request id already exists must be
  refused with ZERO POSTs;
* a cross-org pair, a non-Telnyx carrier, a non-toll-free number and an inactive/
  released number must be refused before any POST;
* an ``approved``/``rejected`` local record must be refused before any POST.

The Telnyx transport is always replaced by an ``httpx.MockTransport``, so nothing in
this module ever contacts the network. All durable-state assertions are made after a
fresh read of the row (never from the possibly-stale in-memory instance).

Tenant scoping: ordinary cases set a real org context with
``set_org_context(session, org_id)`` before any tenant-scoped write and before invoking
the service. Only the deliberately corrupt cross-org fixture lifts tenant enforcement,
via a tightly scoped ``allow_unscoped`` block that is restored on exit.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models as _models_package
from app.db.base import (
    Base,
    allow_unscoped,
    set_org_context,
)
from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models.messaging import OrgNumber
from app.models.numbers import TollFreeVerification
from app.services.telnyx_tollfree_filing import file_tollfree_verification_with_telnyx

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------------------
# Model registration + a self-contained async SQLite session (offline, deterministic).
# --------------------------------------------------------------------------------------
def _register_all_models() -> None:
    """Import every module under ``app.models`` so ``Base.metadata`` knows about every
    table before ``create_all`` resolves cross-model foreign keys."""
    for info in pkgutil.walk_packages(
        _models_package.__path__, prefix=f"{_models_package.__name__}."
    ):
        try:
            importlib.import_module(info.name)
        except Exception:  # pragma: no cover - optional module whose deps are absent
            continue


_register_all_models()


@asynccontextmanager
async def _db_session():
    """A fresh in-memory SQLite database and async session for a single test.

    StaticPool keeps the single in-memory connection alive for the lifetime of the
    engine, so the schema created below is still visible to the yielded session.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------------------
# Injected transport (MockTransport only - never Telnyx).
# --------------------------------------------------------------------------------------
def _respond_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "req_test_123"})


def _respond_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.TimeoutException("simulated POST timeout", request=request)


def _respond_unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("simulated transport failure", request=request)


@asynccontextmanager
async def _http_client(responder):
    """Borrow an injected httpx client that records every request it receives."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield client, calls


# --------------------------------------------------------------------------------------
# Row + settings helpers.
# --------------------------------------------------------------------------------------
_TOLLFREE_E164 = "+18005551234"


def _settings() -> SimpleNamespace:
    """A stand-in for ``Settings`` carrying only the attribute the service reads when no
    active Telnyx provider account exists (it falls back to the base settings).
    """
    return SimpleNamespace(telnyx_api_key="test-telnyx-key")


def _valid_fields() -> dict:
    """Fully valid business (non-sole-proprietor) carrier fields."""
    return {
        "businessName": "Acme Widgets LLC",
        "corporateWebsite": "https://acme.example.com",
        "businessAddr1": "1 Market Street",
        "businessCity": "San Francisco",
        "businessState": "CA",
        "businessZip": "94105",
        "businessContactFirstName": "Dana",
        "businessContactLastName": "Lee",
        "businessContactEmail": "dana@acme.example.com",
        "businessContactPhone": "+14155550123",
        "useCase": "Mixed",
        "useCaseSummary": "Order and delivery updates for opted-in customers.",
        "productionMessageContent": "Acme: your order #1234 shipped today.",
        "optInWorkflow": "Customers opt in at checkout via the SMS consent checkbox.",
        "optInWorkflowImageURLs": [{"url": "https://acme.example.com/optin.png"}],
        "messageVolume": "1,000",
        "additionalInformation": "No additional information.",
        "businessRegistrationNumber": "123456789",
        "businessRegistrationType": "EIN",
        "businessRegistrationCountry": "US",
    }


async def _add_number(session, *, org_id, **overrides) -> OrgNumber:
    values = {
        "org_id": org_id,
        "e164": _TOLLFREE_E164,
        "carrier": "telnyx",
        "is_active": True,
        "number_type": "tollfree",
        "status": "active",
    }
    values.update(overrides)
    number = OrgNumber(**values)
    session.add(number)
    await session.flush()
    await session.commit()
    return number


async def _add_verification(session, number, **overrides) -> TollFreeVerification:
    values = {
        "org_id": number.org_id,
        "number_id": number.id,
        "business_name": "Acme Widgets LLC",
        "use_case": "Mixed",
        "status": "draft",
    }
    values.update(overrides)
    verification = TollFreeVerification(**values)
    session.add(verification)
    await session.flush()
    await session.commit()
    return verification


async def _reload(session, verification_id) -> TollFreeVerification:
    """Read the durable row back from the database, never from the identity map."""
    result = await session.execute(
        sa.select(TollFreeVerification)
        .where(TollFreeVerification.id == verification_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# --------------------------------------------------------------------------------------
# Tenant-scoping helper.
# --------------------------------------------------------------------------------------
@contextmanager
def _unscoped(session):
    """Tightly scoped platform-operator window used ONLY to build the corrupt cross-org
    fixture (two tenant rows with mismatched orgs) and to reach the service's explicit
    same-org guard. Normal tenant scoping is restored on exit."""
    allow_unscoped(session, True)
    try:
        yield
    finally:
        allow_unscoped(session, False)


# --------------------------------------------------------------------------------------
# Field modifiers for the invalid-payload matrix.
# --------------------------------------------------------------------------------------
def _drop(key):
    def _modify(fields):
        fields.pop(key, None)

    return _modify


def _blank(key):
    def _modify(fields):
        fields[key] = "   "

    return _modify


def _set(key, value):
    def _modify(fields):
        fields[key] = value

    return _modify


# --------------------------------------------------------------------------------------
# 1. Invalid/missing payload data fails before any marker and makes zero POSTs.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "modify",
    [
        pytest.param(_drop("businessName"), id="missing-business-name"),
        pytest.param(_drop("optInWorkflow"), id="missing-opt-in-workflow"),
        pytest.param(_blank("corporateWebsite"), id="blank-website"),
        pytest.param(_set("useCase", "Definitely Not A Use Case"), id="unknown-use-case"),
        pytest.param(_set("messageVolume", "54321"), id="unknown-message-volume"),
        pytest.param(
            _set("optInWorkflowImageURLs", [{"url": "ftp://acme.example.com/optin.png"}]),
            id="non-http-image-url",
        ),
        pytest.param(_drop("businessRegistrationNumber"), id="missing-registration-number"),
        pytest.param(_set("verificationStatus", "approved"), id="response-only-field"),
    ],
)
async def test_invalid_payload_is_refused_before_marker_and_posts_nothing(modify):
    async with _db_session() as session:
        org_id = uuid.uuid4()
        set_org_context(session, org_id)
        number = await _add_number(session, org_id=org_id)
        verification = await _add_verification(session, number)

        fields = _valid_fields()
        modify(fields)

        async with _http_client(_respond_ok) as (client, calls):
            with pytest.raises(ValidationFailedError):
                await file_tollfree_verification_with_telnyx(
                    session,
                    _settings(),
                    verification,
                    fields=fields,
                    sole_proprietor=False,
                    client=client,
                )
            assert calls == []

        durable = await _reload(session, verification.id)
        assert "telnyx_tfv_filing" not in durable.carrier_refs
        assert "telnyx" not in durable.carrier_refs


# --------------------------------------------------------------------------------------
# 2. An ambiguous POST failure leaves the marker durable, stores no request id and keeps
#    the prior status; the durable marker then refuses an immediate retry.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "responder",
    [
        pytest.param(_respond_timeout, id="post-timeout"),
        pytest.param(_respond_unreachable, id="transport-error"),
    ],
)
async def test_ambiguous_post_failure_keeps_marker_and_prior_status(responder):
    async with _db_session() as session:
        org_id = uuid.uuid4()
        set_org_context(session, org_id)
        number = await _add_number(session, org_id=org_id)
        verification = await _add_verification(session, number, status="submitted")

        async with _http_client(responder) as (client, calls):
            with pytest.raises(FeatureUnavailableError):
                await file_tollfree_verification_with_telnyx(
                    session,
                    _settings(),
                    verification,
                    fields=_valid_fields(),
                    sole_proprietor=False,
                    client=client,
                )
            assert len(calls) == 1

            # The committed marker must refuse the retry that would otherwise duplicate
            # the (possibly already-accepted) carrier request. No second POST.
            durable_before = await _reload(session, verification.id)
            with pytest.raises(ConflictError):
                await file_tollfree_verification_with_telnyx(
                    session,
                    _settings(),
                    durable_before,
                    fields=_valid_fields(),
                    sole_proprietor=False,
                    client=client,
                )
            assert len(calls) == 1

        durable = await _reload(session, verification.id)
        marker = durable.carrier_refs.get("telnyx_tfv_filing")
        assert isinstance(marker, dict)
        assert marker["status"] == "pending"
        assert "telnyx" not in durable.carrier_refs
        assert durable.status == "submitted"


# --------------------------------------------------------------------------------------
# 3. A repeat when the marker or the Telnyx request id already exists is refused, zero
#    POSTs, and the durable state is unchanged.
# --------------------------------------------------------------------------------------
_PENDING_MARKER = {
    "status": "pending",
    "attempt_id": "11111111-1111-1111-1111-111111111111",
    "attempted_at": "2026-01-01T00:00:00+00:00",
}


@pytest.mark.parametrize(
    "carrier_refs",
    [
        pytest.param({"telnyx_tfv_filing": dict(_PENDING_MARKER)}, id="attempt-marker"),
        pytest.param({"telnyx": "req_already_filed"}, id="request-id"),
        pytest.param(
            {"telnyx": "req_already_filed", "telnyx_tfv_filing": dict(_PENDING_MARKER)},
            id="marker-and-request-id",
        ),
    ],
)
async def test_repeat_with_marker_or_request_id_is_refused(carrier_refs):
    async with _db_session() as session:
        org_id = uuid.uuid4()
        set_org_context(session, org_id)
        number = await _add_number(session, org_id=org_id)
        verification = await _add_verification(session, number, carrier_refs=carrier_refs)

        async with _http_client(_respond_ok) as (client, calls):
            with pytest.raises(ConflictError):
                await file_tollfree_verification_with_telnyx(
                    session,
                    _settings(),
                    verification,
                    fields=_valid_fields(),
                    sole_proprietor=False,
                    client=client,
                )
            assert calls == []

        durable = await _reload(session, verification.id)
        assert durable.carrier_refs == carrier_refs


# --------------------------------------------------------------------------------------
# 4a. Carrier / number-type / number-state refusals (same org) post nothing.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "number_kwargs,expected",
    [
        pytest.param({"carrier": "bandwidth"}, ValidationFailedError, id="non-telnyx-carrier"),
        pytest.param({"number_type": "local"}, ValidationFailedError, id="non-tollfree-number"),
        pytest.param({"is_active": False}, ConflictError, id="inactive-number"),
        pytest.param({"status": "released"}, ConflictError, id="released-number"),
    ],
)
async def test_number_refusals_post_nothing(number_kwargs, expected):
    async with _db_session() as session:
        org_id = uuid.uuid4()
        set_org_context(session, org_id)
        number = await _add_number(session, org_id=org_id, **number_kwargs)
        verification = await _add_verification(session, number)

        async with _http_client(_respond_ok) as (client, calls):
            with pytest.raises(expected):
                await file_tollfree_verification_with_telnyx(
                    session,
                    _settings(),
                    verification,
                    fields=_valid_fields(),
                    sole_proprietor=False,
                    client=client,
                )
            assert calls == []

        durable = await _reload(session, verification.id)
        assert "telnyx_tfv_filing" not in durable.carrier_refs
        assert "telnyx" not in durable.carrier_refs


# --------------------------------------------------------------------------------------
# 4b. Cross-org relation is refused before any POST. The corrupt pair (number owned by one
#     org, verification by another) can only be built - and the service's explicit
#     same-org guard reached - under the unscoped platform-operator window, which is
#     restored the moment the fixture is done.
# --------------------------------------------------------------------------------------
async def test_cross_org_relation_is_refused_post_nothing():
    async with _db_session() as session:
        org_owner = uuid.uuid4()
        org_other = uuid.uuid4()

        with _unscoped(session):
            number = await _add_number(session, org_id=org_owner)
            verification = await _add_verification(session, number, org_id=org_other)
            verification_id = verification.id

            async with _http_client(_respond_ok) as (client, calls):
                with pytest.raises(ConflictError):
                    await file_tollfree_verification_with_telnyx(
                        session,
                        _settings(),
                        verification,
                        fields=_valid_fields(),
                        sole_proprietor=False,
                        client=client,
                    )
                assert calls == []

        # Normal tenant scoping is back: the corrupt pair is still readable under the
        # verification's own org, and nothing durable was written.
        set_org_context(session, org_other)
        durable = await _reload(session, verification_id)
        assert "telnyx_tfv_filing" not in durable.carrier_refs
        assert "telnyx" not in durable.carrier_refs
        assert durable.status == "draft"


# --------------------------------------------------------------------------------------
# 5. Approved/rejected local records are refused before any POST.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["approved", "rejected"])
async def test_terminal_local_status_is_refused_before_post(status):
    async with _db_session() as session:
        org_id = uuid.uuid4()
        set_org_context(session, org_id)
        number = await _add_number(session, org_id=org_id)
        verification = await _add_verification(session, number, status=status)

        async with _http_client(_respond_ok) as (client, calls):
            with pytest.raises(ConflictError):
                await file_tollfree_verification_with_telnyx(
                    session,
                    _settings(),
                    verification,
                    fields=_valid_fields(),
                    sole_proprietor=False,
                    client=client,
                )
            assert calls == []

        durable = await _reload(session, verification.id)
        assert durable.status == status
        assert "telnyx_tfv_filing" not in durable.carrier_refs
        assert "telnyx" not in durable.carrier_refs
