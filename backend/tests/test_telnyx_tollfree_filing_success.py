"""Success-path coverage for ``file_tollfree_verification_with_telnyx``.

Deterministic and fully offline: the carrier is an injected ``httpx.MockTransport`` and
the database is a per-test in-memory async SQLite engine. Nothing here talks to Telnyx and
nothing relies on the ambient environment.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base, set_org_context
from app.models.messaging import OrgNumber
from app.models.numbers import TollFreeVerification
from app.services.telnyx_tollfree_filing import file_tollfree_verification_with_telnyx


def _load_all_models() -> None:
    """Import every module under ``app.models`` so ``Base.metadata`` is complete before we
    call ``create_all``. The models reference one another (and other P-number tables) by
    foreign key, and SQLAlchemy refuses to sort DDL for a table whose FK target has not been
    registered."""
    import app.models as models_pkg

    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"{models_pkg.__name__}.{module.name}")


_load_all_models()

#: A valid NANP toll-free number the carrier can verify (regex: +1 888 + 7 digits).
TOLLFREE_E164 = "+18885551234"

OPT_IN_IMAGE_URL = "https://acme-health.example.com/optin.png"
CARRIER_REQUEST_ID = "40017f5a-3f2b-4d61-9c2e-8d5f1a2b3c4d"
TEST_API_KEY = "telnyx-test-key-do-not-use"

#: A fully valid business (non sole-proprietor) filing using the documented camelCase
#: request fields and the exact ``useCase`` / ``messageVolume`` enum strings.
TOLLFREE_FIELDS: dict[str, Any] = {
    "businessName": "Acme Health Alerts LLC",
    "corporateWebsite": "https://acme-health.example.com",
    "businessAddr1": "123 Main Street",
    "businessCity": "Austin",
    "businessState": "TX",
    "businessZip": "78701",
    "businessContactFirstName": "Dana",
    "businessContactLastName": "Reed",
    "businessContactEmail": "dana.reed@acme-health.example.com",
    "businessContactPhone": "+15125550123",
    "useCaseSummary": "Appointment reminders for patients who opted in.",
    "productionMessageContent": (
        "Acme Health: reminder for your appointment on Tuesday at 9am. "
        "Reply STOP to opt out."
    ),
    "optInWorkflow": (
        "Patients check the SMS consent box on our website booking form."
    ),
    "additionalInformation": "No additional information.",
    "useCase": "Appointments",
    "messageVolume": "10,000",
    "optInWorkflowImageURLs": [OPT_IN_IMAGE_URL],
    "businessRegistrationNumber": "12-3456789",
    "businessRegistrationType": "EIN",
    "businessRegistrationCountry": "US",
}

#: The body the builder must emit for the above fields and number.
EXPECTED_BODY: dict[str, Any] = {
    **TOLLFREE_FIELDS,
    "optInWorkflowImageURLs": [{"url": OPT_IN_IMAGE_URL}],
    "phoneNumbers": [{"phoneNumber": TOLLFREE_E164}],
}


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A per-test in-memory SQLite schema with the full model metadata created."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def settings() -> Any:
    """A settings object carrying a deterministic Telnyx key.

    Built with ``model_construct`` when available (pydantic v2) so we neither read a
    developer ``.env`` nor depend on unrelated required config fields, falling back to a
    minimal attribute stand-in otherwise. The service only reads ``telnyx_api_key`` off the
    resolved settings, and the fresh test database holds no active Telnyx provider account,
    so ``provider_accounts.settings_like_for`` is never reached.
    """
    build = getattr(Settings, "model_construct", None)
    if callable(build):
        return build(telnyx_api_key=TEST_API_KEY)
    return SimpleNamespace(telnyx_api_key=TEST_API_KEY)


@pytest.mark.asyncio
async def test_file_tollfree_verification_success(session: AsyncSession, settings: Any) -> None:
    org_id = uuid.uuid4()
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164=TOLLFREE_E164,
        carrier="telnyx",
        is_active=True,
        number_type="tollfree",
        status="active",
        capabilities={},
    )
    verification = TollFreeVerification(
        id=uuid.uuid4(),
        org_id=org_id,
        number_id=number.id,
        business_name="Acme Health Alerts LLC",
        use_case="MIXED",
        status="draft",
        carrier_refs={},
    )

    # The tenant guard refuses any write without an org context; set it explicitly rather
    # than bypassing it, exactly as the request-scoped session would in production.
    set_org_context(session, org_id)

    session.add_all([number, verification])
    await session.commit()

    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": CARRIER_REQUEST_ID})

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as client:
        filed = await file_tollfree_verification_with_telnyx(
            session,
            settings,
            verification,
            fields=TOLLFREE_FIELDS,
            sole_proprietor=False,
            client=client,
        )

        # Exactly one POST to the documented endpoint, with bearer auth and the built body.
        assert len(captured) == 1
        request = captured[0]
        assert request.method == "POST"
        assert str(request.url) == (
            "https://api.telnyx.com/v2/messaging_tollfree/verification/requests"
        )
        assert request.headers.get("authorization") == f"Bearer {TEST_API_KEY}"
        assert json.loads(request.content) == EXPECTED_BODY

        # The injected client is not owned by the service, so it stays open and usable.
        assert client.is_closed is False
        probe = await client.get("https://api.telnyx.com/v2/health")
        assert probe.status_code == 200
        assert client.is_closed is False

    # The carrier id is recorded under the telnyx key; the attempt marker is cleared and
    # the local status advances to submitted (never approved).
    assert filed.carrier_refs["telnyx"] == CARRIER_REQUEST_ID
    assert "telnyx_tfv_filing" not in filed.carrier_refs
    assert filed.status == "submitted"

    # ... and it is durable, not just in the returned instance.
    reloaded = (
        await session.execute(
            sa.select(TollFreeVerification)
            .where(TollFreeVerification.id == verification.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reloaded.status == "submitted"
    assert reloaded.carrier_refs["telnyx"] == CARRIER_REQUEST_ID
    assert "telnyx_tfv_filing" not in reloaded.carrier_refs
