"""Deterministic success test for the explicit Telnyx toll-free filing reconciliation.

Exercises ``reconcile_tollfree_filing_with_telnyx`` end to end against a real in-memory
async SQLite session and an injected ``httpx.MockTransport`` - no live Telnyx call is ever
made. One unambiguous list page adopts the carrier request id, removes the pending attempt
marker and advances the local status to ``submitted``, and nothing else.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models
from app.db.base import Base, set_org_context
from app.models.messaging import OrgNumber
from app.models.numbers import TollFreeVerification, can_send
from app.services.telnyx_tollfree_filing import reconcile_tollfree_filing_with_telnyx

# Register every model module so all tables (and their foreign-key targets) exist before
# ``Base.metadata.create_all`` runs.
for _module_info in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{_module_info.name}")

API_KEY = "test-telnyx-api-key"
E164 = "+18885550123"
BUSINESS_NAME = "Acme Toll-Free Co"
REQUEST_ID = "tfv-request-0001"
ATTEMPT_ID = "6a1f0f52-0000-4000-8000-000000000001"
ATTEMPTED_AT_ISO = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).isoformat()
LIST_PATH = "/v2/messaging_tollfree/verification/requests"

LIST_RESPONSE = {
    "records": [
        {
            "id": REQUEST_ID,
            "businessName": BUSINESS_NAME,
            "phoneNumbers": [{"phoneNumber": E164}],
        }
    ],
    "total_records": 1,
}


@pytest.mark.asyncio
async def test_reconcile_adopts_single_exact_match_and_clears_pending_marker() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        org_id = uuid.uuid4()

        async with session_factory() as session:
            set_org_context(session, org_id)

            number = OrgNumber(
                org_id=org_id,
                e164=E164,
                carrier="telnyx",
                number_type="tollfree",
                status="active",
                is_active=True,
            )
            session.add(number)
            await session.flush()

            verification = TollFreeVerification(
                org_id=org_id,
                number_id=number.id,
                business_name=BUSINESS_NAME,
                status="draft",
                carrier_refs={
                    "telnyx_tfv_filing": {
                        "status": "pending",
                        "attempt_id": ATTEMPT_ID,
                        "attempted_at": ATTEMPTED_AT_ISO,
                    }
                },
            )
            session.add(verification)
            await session.commit()
            tfv_id = verification.id

            seen: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(200, json=LIST_RESPONSE)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                result = await reconcile_tollfree_filing_with_telnyx(
                    session,
                    SimpleNamespace(telnyx_api_key=API_KEY),
                    verification,
                    client=client,
                )
                # The caller owns an injected client, so it must remain open.
                assert client.is_closed is False
            finally:
                await client.aclose()

            # Exactly one read, and never a POST/retry of the original submission.
            assert [request.method for request in seen] == ["GET"]
            request = seen[0]
            assert request.url.path == LIST_PATH
            assert request.headers["authorization"] == f"Bearer {API_KEY}"
            params = request.url.params
            assert {name for name, _ in params.multi_items()} == {
                "page",
                "page_size",
                "phone_number",
                "business_name",
                "date_start",
            }
            assert params["page"] == "1"
            assert params["page_size"] == "100"
            assert params["phone_number"] == E164
            assert params["business_name"] == BUSINESS_NAME
            assert params["date_start"] == ATTEMPTED_AT_ISO

            # The returned row is already advanced, but only to `submitted`.
            assert result.status == "submitted"
            assert can_send(result.status) is False

        # Durably stored: read the row back through a separate session.
        async with session_factory() as check:
            set_org_context(check, org_id)
            stored = (
                await check.execute(
                    sa.select(TollFreeVerification).where(TollFreeVerification.id == tfv_id)
                )
            ).scalar_one()
            assert stored.status == "submitted"
            assert can_send(stored.status) is False
            # Exact carrier id recorded, pending attempt marker gone - nothing else.
            assert stored.carrier_refs == {"telnyx": REQUEST_ID}
    finally:
        await engine.dispose()
