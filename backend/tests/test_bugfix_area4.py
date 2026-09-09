"""Regression tests for BUGFIX_LEDGER_2026-09.md Area 4 (numbers / providers / spend /
sweeper), + 1.1 folded into this batch."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Message, Org, OrgNumber, ProviderSpendDaily
from app.providers.numbers import OrderResult
from app.services import spend
from tests.conftest import auth_headers, create_org, register_and_login


class FakeNumberProvider:
    """Satisfies NumberProvider (search/order/release) AND lookup_owned_number - the
    shape add_number's 1.1 ownership gate actually asks for."""

    name = "fake"

    def __init__(self, lookup_result=True):
        self.lookup_result = lookup_result
        self.lookup_calls: list[str] = []
        self.release_calls: list[tuple] = []
        self.order_calls: list[str] = []
        self.order_side_effect = None

    async def search_numbers(self, query):
        return []

    async def order_number(self, e164):
        self.order_calls.append(e164)
        if self.order_side_effect:
            await self.order_side_effect(e164)
        return OrderResult(e164=e164, provider_ref="order-id-1", status="active", capabilities={})

    async def release_number(self, e164, provider_ref=None):
        self.release_calls.append((e164, provider_ref))

    async def lookup_owned_number(self, e164):
        self.lookup_calls.append(e164)
        return self.lookup_result


async def _app_with_number_provider(engine, settings, lookup_result=True):
    from app.main import create_app
    from app.providers.registry import CarrierRegistry

    provider = FakeNumberProvider(lookup_result=lookup_result)
    application = create_app(settings)
    application.state.carriers = CarrierRegistry({provider.name: provider}, primary=provider.name)
    application.state.carrier = provider
    transport = httpx.ASGITransport(app=application)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, provider, application


# ----------------------------------------------------------------------------------
# 1.1: add_number ownership verification
# ----------------------------------------------------------------------------------
async def test_1_1_add_number_rejects_unowned_number(engine, settings):
    client, provider, _ = await _app_with_number_provider(engine, settings, lookup_result=False)
    async with client:
        token = await register_and_login(client, "area41-owner@example.com")
        org = await create_org(client, token, "Area41 Org")
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": "+12145550100"},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 422, r.text
        assert provider.lookup_calls == ["+12145550100"]


async def test_1_1_add_number_accepts_when_owned(engine, settings):
    client, provider, _ = await _app_with_number_provider(engine, settings, lookup_result=True)
    async with client:
        token = await register_and_login(client, "area41-owner2@example.com")
        org = await create_org(client, token, "Area41 Org2")
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": "+12145550100"},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 201, r.text


async def test_1_1_add_number_unverifiable_requires_opt_in(engine, settings):
    # None (unverifiable) -> 422 unless allow_unverified_number_add.
    client2, _provider2, _app2 = await _app_with_number_provider(
        engine, settings, lookup_result=None
    )
    async with client2:
        token2 = await register_and_login(client2, "area41-owner3@example.com")
        org2 = await create_org(client2, token2, "Area41 Org3")
        r = await client2.post(
            "/api/v1/numbers",
            json={"e164": "+12145550100"},
            headers=auth_headers(token2, org2["id"]),
        )
        assert r.status_code == 422, r.text

    client3, _provider3, app3 = await _app_with_number_provider(
        engine, settings, lookup_result=None
    )
    app3.state.settings.allow_unverified_number_add = True
    async with client3:
        token3 = await register_and_login(client3, "area41-owner4@example.com")
        org3 = await create_org(client3, token3, "Area41 Org4")
        r = await client3.post(
            "/api/v1/numbers",
            json={"e164": "+12145550100"},
            headers=auth_headers(token3, org3["id"]),
        )
        assert r.status_code == 201, r.text


async def test_1_1_add_number_skips_check_for_non_number_provider_carrier(client):
    """A messaging-only carrier (the common test/legacy shape, no search/order/release)
    is not a NumberProvider at all - add_number must keep its pre-1.1 manual-add
    behaviour for it unchanged, not 503/crash trying to verify ownership."""
    token = await register_and_login(client, "area41-legacy@example.com")
    org = await create_org(client, token, "Area41 Legacy Org")
    r = await client.post(
        "/api/v1/numbers",
        json={"e164": "+12145550100"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text


# ----------------------------------------------------------------------------------
# B1 (4.21): a caller-named carrier the registry cannot resolve must reject with 422,
# not silently skip the 1.1 ownership gate and accept the number anyway.
# ----------------------------------------------------------------------------------
async def test_b1_add_number_rejects_unconfigured_named_carrier(engine, settings):
    """Deployment only has "fake" configured; naming "twilio" explicitly must 422, never
    fall through to an unchecked 201."""
    client, provider, _ = await _app_with_number_provider(engine, settings, lookup_result=True)
    async with client:
        token = await register_and_login(client, "b1-owner@example.com")
        org = await create_org(client, token, "B1 Org")
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": "+12145550100", "carrier": "twilio"},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 422, r.text
        assert provider.lookup_calls == []


# ----------------------------------------------------------------------------------
# E3: naming a REAL but non-NumberProvider carrier, when some OTHER configured
# carrier IS NumberProvider-capable, must also 422 - not just an unresolvable name.
# ----------------------------------------------------------------------------------
async def test_e3_add_number_rejects_named_carrier_that_cannot_verify_ownership(
    engine, settings
):
    from app.main import create_app
    from app.providers.registry import CarrierRegistry

    class _NonProviderCarrier:
        """Resolves fine, but implements no search/order/release/lookup_owned_number -
        e.g. a voice-only or messaging-only adapter."""

        name = "bandwidth"

    fake_provider = FakeNumberProvider(lookup_result=True)
    non_provider = _NonProviderCarrier()
    application = create_app(settings)
    application.state.carriers = CarrierRegistry(
        {fake_provider.name: fake_provider, non_provider.name: non_provider},
        primary=fake_provider.name,
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = await register_and_login(client, "e3-owner@example.com")
        org = await create_org(client, token, "E3 Org")
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": "+12145550100", "carrier": "bandwidth"},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 422, r.text
        assert fake_provider.lookup_calls == []

        # The verifiable carrier itself still works fine, named explicitly.
        r2 = await client.post(
            "/api/v1/numbers",
            json={"e164": "+12145550101", "carrier": "fake"},
            headers=auth_headers(token, org["id"]),
        )
        assert r2.status_code == 201, r2.text


# ----------------------------------------------------------------------------------
# 4.5: toll-free prefix always derives number_type on manual add
# ----------------------------------------------------------------------------------
async def test_4_5_tollfree_add_derives_number_type(engine, settings):
    client, _provider, _ = await _app_with_number_provider(engine, settings, lookup_result=True)
    async with client:
        token = await register_and_login(client, "area45@example.com")
        org = await create_org(client, token, "Area45 Org")
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": "+18005550100", "number_type": "local"},
            headers=auth_headers(token, org["id"]),
        )
        assert r.status_code == 201, r.text
        assert r.json()["number_type"] == "tollfree"


# ----------------------------------------------------------------------------------
# 4.6: failed orders never accrue MRC/setup
# ----------------------------------------------------------------------------------
async def test_4_6_failed_orders_do_not_accrue_mrc_or_setup(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area46 Org", slug="area46-org"))
    await session.flush()
    set_org_context(session, org_id)

    day = date(2026, 6, 15)
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164="+12145550100",
        carrier="telnyx",
        status="failed",
        monthly_cost_cents=1000,
        purchase_cost_cents=2000,
        purchased_at=day_start,
        number_type="local",
    )
    session.add(number)
    await session.commit()

    await spend.rollup_day(session, org_id, day)

    rows = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == day,
            )
        )
    ).scalars().all()
    assert rows == []


async def test_4_6_active_orders_still_accrue_mrc(session):
    """Sanity companion to the failed-order test - a real active number must still
    accrue MRC, so the fix is a status filter, not an accidental no-op."""
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area46B Org", slug="area46b-org"))
    await session.flush()
    set_org_context(session, org_id)

    day = date(2026, 6, 15)
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164="+12145550101",
        carrier="telnyx",
        status="active",
        monthly_cost_cents=1000,
        purchased_at=day_start,
        number_type="local",
    )
    session.add(number)
    await session.commit()

    await spend.rollup_day(session, org_id, day)

    rows = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "number_mrc",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


# ----------------------------------------------------------------------------------
# 4.8: a purchase that cannot be stored releases at the provider (never orphaned)
# ----------------------------------------------------------------------------------
async def test_4_8_order_conflict_releases_at_provider(engine, settings):
    """The pre-check only catches the number already being registered BEFORE the order
    call - this exercises the rarer TOCTOU race where a concurrent insert wins DURING
    the carrier call, so the pre-check passes but our own flush hits IntegrityError."""
    client, provider, _ = await _app_with_number_provider(engine, settings, lookup_result=True)
    async with client:
        token = await register_and_login(client, "area48@example.com")
        org = await create_org(client, token, "Area48 Org")
        e164 = "+12145550100"

        from app.db.session import get_sessionmaker

        async with get_sessionmaker()() as session:
            set_org_context(session, uuid.UUID(org["id"]))
            number_id = uuid.uuid4()

            async def insert_conflict(_e164):
                session.add(
                    OrgNumber(
                        id=number_id,
                        org_id=uuid.UUID(org["id"]),
                        e164=e164,
                        carrier=provider.name,
                        number_type="local",
                    )
                )
                await session.flush()

            provider.order_side_effect = insert_conflict

            r2 = await client.post(
                "/api/v1/numbers/order",
                json={"e164": e164},
                headers=auth_headers(token, org["id"]),
            )
            assert r2.status_code == 409, r2.text
            assert any(e == e164 for e, _ in provider.release_calls)


async def test_4_8_order_pre_check_never_reaches_the_carrier(engine, settings):
    """The pre-check must reject BEFORE calling the carrier at all - order_calls stays
    empty for an already-registered number."""
    client, provider, _ = await _app_with_number_provider(engine, settings, lookup_result=True)
    async with client:
        token = await register_and_login(client, "area48b@example.com")
        org = await create_org(client, token, "Area48B Org")
        e164 = "+12145550111"

        r = await client.post(
            "/api/v1/numbers", json={"e164": e164}, headers=auth_headers(token, org["id"])
        )
        assert r.status_code == 201, r.text

        r2 = await client.post(
            "/api/v1/numbers/order",
            json={"e164": e164},
            headers=auth_headers(token, org["id"]),
        )
        assert r2.status_code == 409, r2.text
        assert provider.order_calls == []


# ----------------------------------------------------------------------------------
# 4.9: probe uses the org's DB-backed provider account, not env credentials
# ----------------------------------------------------------------------------------
async def test_4_9_probe_uses_org_db_account_when_present(client, session, monkeypatch):
    from app.providers import probes
    from app.providers import registry_org

    token = await register_and_login(client, "area49@example.com")
    org = await create_org(client, token, "Area49 Org")
    org_id = uuid.UUID(org["id"])

    called_env_probe = False

    async def fake_env_probe(name, settings, **kwargs):
        nonlocal called_env_probe
        called_env_probe = True
        return probes.ProbeResult(name=name, ok=False, detail="env probe should not run", checked="")

    monkeypatch.setattr(probes, "probe", fake_env_probe)
    monkeypatch.setattr(registry_org, "db_backed_providers", lambda oid: frozenset({"telnyx"}))

    async def fake_probe_account(session_, settings_, account, **kwargs):
        account.status = "active"
        account.last_probe_detail = "org account probed"
        account.last_probe_at = datetime.now(timezone.utc)
        return account

    from app.models.provider_accounts import ProviderAccount
    from app.services import provider_accounts as provider_accounts_svc

    set_org_context(session, org_id)
    # active_account_for() (used by both /numbers/order attribution and this probe
    # route) only ever returns the org's currently ACTIVE account - the one actually
    # live in build_registry_for_org's registry, which is the one worth re-probing.
    account = ProviderAccount(
        id=uuid.uuid4(), org_id=org_id, provider="telnyx", label="org acct",
        credentials_encrypted="x", status="active",
    )
    session.add(account)
    await session.commit()

    monkeypatch.setattr(provider_accounts_svc, "probe_account", fake_probe_account)

    r = await client.post(
        "/api/v1/routing/carriers/telnyx/probe", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "org account probed"
    assert called_env_probe is False


# ----------------------------------------------------------------------------------
# 4.18: an expired org registry cache entry is still authoritative - never env fallback
# ----------------------------------------------------------------------------------
async def test_4_18_expired_registry_never_falls_back_to_env():
    from app.providers import registry_org
    from app.providers.registry import CarrierRegistry

    org_id = uuid.uuid4()
    # current_version() defaults to 0 for an org that has never had bump_version()
    # called - the cache key here must match that, or the lookup misses entirely.
    version = registry_org.current_version(org_id)
    registry_org._ORG_REGISTRY_CACHE[(org_id, version)] = (
        CarrierRegistry({"fake": None}, primary="fake"),
        {"fake": None},
        -3600.0,  # expired long ago
    )

    registry_org.CURRENT_ORG_ID.set(org_id)
    try:
        proxy = registry_org.CarrierRegistryProxy(CarrierRegistry({"env": None}, primary="env"))
        resolved = proxy._resolve()
        assert "fake" in resolved.names()
        assert "env" not in resolved.names()

        # B4: db_backed_providers() must mirror _resolve() exactly - an expired entry is
        # still authoritative for both, or order/probe (which dispatches via _resolve())
        # would keep using the org's real DB-backed adapter while this told the caller
        # it was "env", diverging from what actually happened on the wire.
        assert registry_org.db_backed_providers(org_id) == frozenset({"fake"})
    finally:
        registry_org.CURRENT_ORG_ID.set(None)
        registry_org._ORG_REGISTRY_CACHE.pop((org_id, version), None)


# ----------------------------------------------------------------------------------
# 4.23: /healthz does not leak env/version to an unauthenticated caller
# ----------------------------------------------------------------------------------
async def test_4_23_healthz_does_not_leak_env_or_version(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "env" not in body
    assert "version" not in body
    assert "status" in body
    assert "db" in body


# ----------------------------------------------------------------------------------
# 4.26: released numbers excluded from the reputation report
# ----------------------------------------------------------------------------------
async def test_4_26_released_numbers_excluded_from_reputation(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area426 Org", slug="area426-org"))
    await session.flush()

    set_org_context(session, org_id)
    active = OrgNumber(
        id=uuid.uuid4(), org_id=org_id, e164="+12145550100", carrier="telnyx",
        status="active", number_type="local",
    )
    released = OrgNumber(
        id=uuid.uuid4(), org_id=org_id, e164="+12145550101", carrier="telnyx",
        status="released", number_type="local",
    )
    session.add_all([active, released])
    await session.flush()

    from app.services import messaging as messaging_svc

    for number in (active, released):
        thread = await messaging_svc.upsert_thread(session, org_id, number.e164, "+19725550101")
        session.add(
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
                status="delivered", from_e164=number.e164, to_e164="+19725550101",
                carrier=number.carrier,
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
    await session.commit()

    from app.services import reputation

    stats = await reputation.compute_number_stats(session, org_id)
    e164s = {s.e164 for s in stats}
    assert active.e164 in e164s
    assert released.e164 not in e164s


# ----------------------------------------------------------------------------------
# 4.29: primary_provider preference is honoured when multiple accounts are active
# ----------------------------------------------------------------------------------
async def test_4_29_primary_provider_preference_honoured(settings):
    settings.primary_provider = "signalwire"
    from types import SimpleNamespace

    accounts = [
        SimpleNamespace(org_id=uuid.uuid4(), provider="bandwidth", status="active"),
        SimpleNamespace(org_id=uuid.uuid4(), provider="telnyx", status="active"),
        SimpleNamespace(org_id=uuid.uuid4(), provider="signalwire", status="active"),
    ]
    from app.providers import registry_org as ro

    old_construct = ro._construct_provider
    old_settings_like_for = ro.settings_like_for
    ro._construct_provider = lambda name, src, base: SimpleNamespace(name=name)
    # settings_like_for normally decrypts real ProviderAccount credentials - these
    # SimpleNamespace stand-ins carry none, so bypass it too; this test is only about
    # the primary-selection logic, not credential decryption.
    ro.settings_like_for = lambda settings_, account_: settings_
    try:
        registry, _db_owned = ro.build_registry_for_org(settings, accounts, global_registry=None)
    finally:
        ro._construct_provider = old_construct
        ro.settings_like_for = old_settings_like_for

    assert registry.primary_name == "signalwire"


# ----------------------------------------------------------------------------------
# 4.16: voice spend buckets by ended_at, matching services/usage.py
# ----------------------------------------------------------------------------------
async def test_4_16_voice_spend_buckets_by_ended_at_not_created_at(session):
    from app.models.voice import Call

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area416 Org", slug="area416-org"))
    await session.flush()
    set_org_context(session, org_id)

    day = date(2026, 6, 15)
    # created just before midnight, ENDED just after - must bucket to day+1, not day.
    created = datetime(2026, 6, 14, 23, 59, tzinfo=timezone.utc)
    ended = datetime(2026, 6, 15, 0, 5, tzinfo=timezone.utc)
    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="outbound", contact_e164="+19725550101",
        our_e164="+12145550100", carrier="telnyx", status="completed",
        duration_seconds=120, created_at=created, ended_at=ended,
    )
    session.add(call)
    await session.commit()

    n_prev = await spend.rollup_day(session, org_id, date(2026, 6, 14))
    n_day = await spend.rollup_day(session, org_id, day)

    rows_prev = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == date(2026, 6, 14),
                ProviderSpendDaily.metric == "voice_min_out",
            )
        )
    ).scalars().all()
    rows_day = (
        await session.execute(
            sa.select(ProviderSpendDaily).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date == day,
                ProviderSpendDaily.metric == "voice_min_out",
            )
        )
    ).scalars().all()
    assert rows_prev == []
    assert len(rows_day) == 1
