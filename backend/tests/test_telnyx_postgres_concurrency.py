"""PostgreSQL-only concurrency regression for Telnyx brand filing.

``file_brand_with_telnyx`` promises a brand reaches Telnyx *exactly once*: concurrent
callers are serialised by a Postgres ``SELECT ... FOR UPDATE`` row lock plus an attempt
marker committed BEFORE the non-refundable carrier POST. SQLite ignores ``FOR UPDATE``
and the guard tests patch ``_lock_brand`` out, so only this module can observe the race:
two independent ``AsyncSession``s contest the same committed row.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ConflictError
from app.models import Org
from app.models.numbers import Brand
from app.services import telnyx_brand_filing as filing

#: conftest skips pg_only items (with a clear reason) unless TEST_DATABASE_URL is Postgres.
pytestmark = [pytest.mark.pg_only, pytest.mark.asyncio]

#: Deterministic identifier the mocked Telnyx transport returns; no network is used.
CARRIER_BRAND_ID = "TLX-1"


class _CarrierPostCounter:
    """Network-free Telnyx transport recording every request that reaches it.

    Each task gets its own client; all share this counter - the POST count source of truth.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def count(self) -> int:
        return len(self.requests)

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            # Appending is atomic under asyncio (no await between the check and the add).
            self.requests.append(request)
            return httpx.Response(200, json={"brandId": CARRIER_BRAND_ID})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _TwoPartyGate:
    """Release two tasks at ~the same instant.

    Otherwise the loop could run a whole filing before the second task reaches its lock,
    so the test would pass without exercising it. The timeout only stops a hang.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self._event = asyncio.Event()
        self._arrived = 0
        self._timeout = timeout

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= 2:
            self._event.set()
        await asyncio.wait_for(self._event.wait(), timeout=self._timeout)


async def _file_in_own_session(
    gate: _TwoPartyGate,
    counter: _CarrierPostCounter,
    settings: Any,
    org_id: Any,
    brand_id: Any,
) -> Brand:
    """One full filing: its own session, tenant context and client, racing at the gate."""
    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        await gate.wait()
        client = counter.client()
        try:
            return await filing.file_brand_with_telnyx(
                session=session,
                settings=settings,
                brand=SimpleNamespace(id=brand_id),
                company_name="Acme",
                first_name="Ana",
                last_name="Lopez",
                brand_relationship="is",
                client=client,
            )
        finally:
            await client.aclose()


async def test_two_concurrent_filings_post_to_carrier_once(engine, monkeypatch):
    counter = _CarrierPostCounter()

    # Keep this test about the concurrency guard. Payload building and local pre-flight
    # validation are covered by test_telnyx_brand_filing_guard.py and would otherwise
    # force a fully populated 10DLC brand; the account lookup is stubbed so no real
    # Telnyx key or provider-account row is needed.
    monkeypatch.setattr(filing, "validate_brand_for_submission", lambda brand: None)
    monkeypatch.setattr(
        filing, "build_brand_payload", lambda brand, **kwargs: {"entityType": "X"}
    )

    settings = SimpleNamespace(telnyx_api_key="test-key")

    async def _resolve(session, base):
        return settings

    monkeypatch.setattr(filing, "_resolve_telnyx_settings", _resolve)

    # One committed, tenant-scoped brand row for both tasks to contend over.
    async with get_sessionmaker()() as setup:
        org = Org(name="Concurrency Org", slug=f"concurrency-{uuid.uuid4().hex[:12]}")
        setup.add(org)
        await setup.flush()
        org_id = org.id
        set_org_context(setup, org_id)
        brand = Brand(
            org_id=org_id, name="Concurrency Brand", status="draft", carrier_refs={}
        )
        setup.add(brand)
        await setup.flush()
        brand_id = brand.id
        await setup.commit()

    gate = _TwoPartyGate()
    results = await asyncio.gather(
        _file_in_own_session(gate, counter, settings, org_id, brand_id),
        _file_in_own_session(gate, counter, settings, org_id, brand_id),
        return_exceptions=True,
    )

    # The row lock plus the committed attempt marker let exactly one carrier POST through.
    assert counter.count == 1, results

    winners = [r for r in results if isinstance(r, Brand)]
    refusals = [r for r in results if isinstance(r, ConflictError)]
    assert len(winners) == 1, results
    assert len(refusals) == 1, results

    async with get_sessionmaker()() as check:
        set_org_context(check, org_id)
        row = (
            await check.execute(sa.select(Brand).where(Brand.id == brand_id))
        ).scalar_one()
        refs = dict(row.carrier_refs or {})
        status = row.status

    # The loser was refused without a duplicate carrier mutation: one brandId, no leftover
    # pending marker, and the status advanced exactly once by the winner.
    assert refs == {filing._BRAND_ID_KEY: CARRIER_BRAND_ID}
    assert status == "submitted"
