"""P37a: managed Telnyx sub-account provisioning + managed number ordering.

Every Telnyx call is served by an ``httpx.MockTransport`` that RECORDS which key signed
it, so the tests can assert the thing a sub-account bug hides behind: that the master key
only ever creates/reads the managed account, and the SUB-ACCOUNT key does everything
inside it.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.errors import FeatureUnavailableError
from app.main import create_app
from app.models import OrgNumber, ProviderAccount
from app.models.telephony import TelephonyAccount
from app.services import credentials as credential_svc
from app.services import credits
from app.services import telephony_provisioning as provisioning_svc
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

MASTER_KEY = "KEY-master-platform-do-not-leak"
SUB_KEY = "KEY-sub-account-abc123"
MANAGED_ID = "managed-acct-1"
PROFILE_ID = "msg-profile-1"

#: telnyx number_mrc (1_000_000) + number_setup (1_000_000), each x the 30% default
#: traffic markup telephony_billing applies when an org has no explicit rate price.
NUMBER_MRC = 1_300_000
NUMBER_SETUP = 1_300_000


# ======================================================================================
# Mock Telnyx
# ======================================================================================
class FakeTelnyx:
    """Scriptable Telnyx. ``calls`` is [(method, path, bearer)] in order."""

    def __init__(self, **overrides) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.managed_account: dict = {"id": MANAGED_ID, "api_key": SUB_KEY}
        self.profile_status = 201
        self.probe_status = 200
        for key, value in overrides.items():
            setattr(self, key, value)

    @property
    def creates(self) -> list[tuple[str, str, str | None]]:
        """Only the calls that MAKE something at Telnyx - what idempotency is about."""
        return [c for c in self.calls if c[0] == "POST"]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        bearer = (request.headers.get("Authorization") or "").replace("Bearer ", "") or None
        self.calls.append((request.method, path, bearer))

        if request.method == "POST" and path.endswith("/managed_accounts"):
            return httpx.Response(201, json={"data": {"id": self.managed_account["id"]}})
        if request.method == "GET" and "/managed_accounts/" in path:
            return httpx.Response(200, json={"data": dict(self.managed_account)})
        if request.method == "POST" and path.endswith("/messaging_profiles"):
            if self.profile_status >= 400:
                return httpx.Response(self.profile_status, json={"errors": [{"detail": "nope"}]})
            return httpx.Response(self.profile_status, json={"data": {"id": PROFILE_ID}})
        # The provider-account probe (app/providers/probes.py::_probe_telnyx).
        if request.method == "GET" and path.endswith("/messaging_profiles"):
            return httpx.Response(self.probe_status, json={"data": []})
        if request.method == "POST" and path.endswith("/number_orders"):
            body = json.loads(request.content.decode() or "{}")
            e164 = body["phone_numbers"][0]["phone_number"]
            self.last_order_body = body
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "order-1",
                        "status": "success",
                        "phone_numbers": [
                            {"phone_number": e164, "features": [{"name": "sms"}]}
                        ],
                    }
                },
            )
        if request.method == "GET" and path.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"errors": [{"detail": f"unhandled {path}"}]})


def managed_settings(**overrides):
    base = {
        "telephony_managed_enabled": True,
        "telnyx_master_api_key": MASTER_KEY,
        "credentials_master_key": Fernet.generate_key().decode(),
        "public_base_url": "https://csaas.example.test",
    }
    base.update(overrides)
    return make_settings(**base)


@pytest.fixture
def telnyx() -> FakeTelnyx:
    return FakeTelnyx()


@pytest.fixture
async def http(telnyx: FakeTelnyx):
    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        yield c


async def _org_row(session, name: str = "Managed Org"):
    from app.models import Org

    org = Org(id=uuid.uuid4(), name=name, slug=f"mt-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    return org


async def _account(session, org_id) -> TelephonyAccount | None:
    return await provisioning_svc.get_account(session, org_id)


# ======================================================================================
# provision()
# ======================================================================================
async def test_happy_path_ends_with_an_active_encrypted_provider_account(
    session, telnyx, http
):
    settings = managed_settings()
    org = await _org_row(session)

    account = await provisioning_svc.provision(session, settings, org.id, http=http)

    assert account.status == "active"
    assert account.last_step == "provider_account"
    assert account.last_error is None
    assert account.managed_account_id == MANAGED_ID
    assert account.messaging_profile_id == PROFILE_ID

    # The master key created and read the managed account; the SUB-account key did
    # everything inside it. A mix-up here is the classic reseller bug.
    assert ("POST", "/v2/managed_accounts", MASTER_KEY) in telnyx.calls
    assert ("GET", f"/v2/managed_accounts/{MANAGED_ID}", MASTER_KEY) in telnyx.calls
    assert ("POST", "/v2/messaging_profiles", SUB_KEY) in telnyx.calls
    assert MASTER_KEY not in [
        bearer for method, path, bearer in telnyx.calls if "managed_accounts" not in path
    ]

    # The sub-account key is ENCRYPTED at rest - never the plaintext in the column.
    assert SUB_KEY not in (account.api_key_encrypted or "")
    assert credential_svc.decrypt(settings, account.api_key_encrypted)["api_key"] == SUB_KEY

    set_org_context(session, org.id)
    row = (
        await session.execute(sa.select(ProviderAccount).where(ProviderAccount.org_id == org.id))
    ).scalar_one()
    assert row.provider == "telnyx"
    assert row.status == "active"
    assert SUB_KEY not in row.credentials_encrypted
    creds = credential_svc.decrypt(settings, row.credentials_encrypted)
    assert creds["api_key"] == SUB_KEY
    assert creds["messaging_profile_id"] == PROFILE_ID


async def test_webhook_url_points_at_us_on_the_messaging_profile(session, telnyx):
    settings = managed_settings()
    org = await _org_row(session)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/messaging_profiles"):
            seen.update(json.loads(request.content.decode()))
        return telnyx._handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await provisioning_svc.provision(session, settings, org.id, http=c)

    assert seen["webhook_url"] == "https://csaas.example.test/api/v1/webhooks/telnyx/messaging"
    assert seen["webhook_failover_url"] == seen["webhook_url"]
    assert seen["webhook_api_version"] == "2"


async def test_second_provision_makes_zero_telnyx_create_calls(session, telnyx, http):
    settings = managed_settings()
    org = await _org_row(session)

    await provisioning_svc.provision(session, settings, org.id, http=http)
    first_creates = list(telnyx.creates)
    assert first_creates, "sanity: the first run really did create things"

    account = await provisioning_svc.provision(session, settings, org.id, http=http)

    assert account.status == "active"
    assert telnyx.creates == first_creates, "a re-run must never create a second account"
    set_org_context(session, org.id)
    assert (
        await session.execute(
            sa.select(sa.func.count(TelephonyAccount.id)).where(TelephonyAccount.org_id == org.id)
        )
    ).scalar_one() == 1


async def test_a_suspended_account_cannot_reactivate_itself_through_setup(
    session, telnyx, http
):
    """A suspended org (P37c non-payment) must not be able to un-suspend itself by
    re-running setup: every step is already stored, so the run would skip them all and
    end "active"."""
    settings = managed_settings()
    org = await _org_row(session)
    await provisioning_svc.provision(session, settings, org.id, http=http)
    account = await _account(session, org.id)
    account.status = "suspended"
    await session.commit()
    creates_before = list(telnyx.creates)

    with pytest.raises(FeatureUnavailableError) as exc_info:
        await provisioning_svc.provision(session, settings, org.id, http=http)

    assert "suspended" in str(exc_info.value).lower()
    account = await _account(session, org.id)
    assert account.status == "suspended"
    assert telnyx.creates == creates_before
    with pytest.raises(FeatureUnavailableError):
        await provisioning_svc.order_number(
            session, settings, org.id, "+14695550100", http=http
        )


async def test_messaging_profile_failure_is_plain_and_the_rerun_resumes(session):
    settings = managed_settings()
    org = await _org_row(session)
    telnyx = FakeTelnyx(profile_status=500)

    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        with pytest.raises(FeatureUnavailableError) as exc:
            await provisioning_svc.provision(session, settings, org.id, http=c)

    account = await _account(session, org.id)
    assert account.status == "failed"
    assert account.managed_account_id == MANAGED_ID
    assert account.messaging_profile_id is None
    assert account.last_step == "api_key"
    # A plain sentence - no raw JSON body, no key, no stack.
    assert "500" in account.last_error
    assert "{" not in account.last_error
    assert MASTER_KEY not in account.last_error and SUB_KEY not in account.last_error
    assert MASTER_KEY not in str(exc.value) and SUB_KEY not in str(exc.value)

    # The retry succeeds and creates exactly ONE more thing: the profile it failed on.
    telnyx.profile_status = 201
    before = len(telnyx.creates)
    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        account = await provisioning_svc.provision(session, settings, org.id, http=c)

    assert account.status == "active"
    assert account.messaging_profile_id == PROFILE_ID
    created_after = list(telnyx.creates[before:])
    assert [path for _m, path, _k in created_after] == ["/v2/messaging_profiles"]


async def test_managed_account_without_an_api_key_stops_with_the_exact_sentence(session):
    settings = managed_settings()
    org = await _org_row(session)
    telnyx = FakeTelnyx(managed_account={"id": MANAGED_ID})  # no api_key in the GET

    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        with pytest.raises(FeatureUnavailableError):
            await provisioning_svc.provision(session, settings, org.id, http=c)

    account = await _account(session, org.id)
    assert account.status == "failed"
    assert account.last_error == provisioning_svc.MISSING_API_KEY_MESSAGE
    # Nothing half-written that a re-run would duplicate: the managed account id IS
    # recorded (so no second sub-account is ever created), and nothing after it is.
    assert account.managed_account_id == MANAGED_ID
    assert account.api_key_encrypted is None
    assert account.messaging_profile_id is None

    telnyx.managed_account = {"id": MANAGED_ID, "api_key": SUB_KEY}
    before = len([c for c in telnyx.creates if c[1].endswith("/managed_accounts")])
    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        account = await provisioning_svc.provision(session, settings, org.id, http=c)
    assert account.status == "active"
    assert (
        len([c for c in telnyx.creates if c[1].endswith("/managed_accounts")]) == before
    ), "the retry must not create a second managed account"


async def test_probe_failure_leaves_the_account_not_active(session):
    """MUTATION GUARD: dropping the "only active once probed" check must fail here."""
    settings = managed_settings()
    org = await _org_row(session)
    telnyx = FakeTelnyx(probe_status=401)

    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        with pytest.raises(FeatureUnavailableError):
            await provisioning_svc.provision(session, settings, org.id, http=c)

    account = await _account(session, org.id)
    assert account.status == "failed"
    set_org_context(session, org.id)
    row = (
        await session.execute(sa.select(ProviderAccount).where(ProviderAccount.org_id == org.id))
    ).scalar_one()
    assert row.status != "active", "unverified credentials must never be marked active"


async def test_provision_refuses_when_the_flag_is_off_or_the_master_key_is_empty(session):
    org = await _org_row(session)
    with pytest.raises(Exception) as off:
        await provisioning_svc.provision(session, managed_settings(
            telephony_managed_enabled=False), org.id)
    assert "not enabled" in str(off.value)

    with pytest.raises(Exception) as unset:
        await provisioning_svc.provision(session, managed_settings(
            telnyx_master_api_key=""), org.id)
    assert "master API key" in str(unset.value)


# ======================================================================================
# order_number()
# ======================================================================================
async def _provisioned_org(session, settings, telnyx, name="Managed Org"):
    org = await _org_row(session, name)
    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        await provisioning_svc.provision(session, settings, org.id, http=c)
    return org


async def _enable_prepaid(session, org_id, balance: int = 0):
    from app.models import Org

    org = await session.get(Org, org_id)
    org.telephony_prepaid = True
    from datetime import datetime, timedelta, timezone

    org.telephony_prepaid_since = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    if balance:
        await credits.topup(session, org_id, balance, reference=f"topup-{uuid.uuid4()}")
        await session.commit()


async def test_order_number_is_refused_by_the_prepaid_gate_on_an_empty_balance(session, telnyx):
    settings = managed_settings()
    org = await _provisioned_org(session, settings, telnyx)
    await _enable_prepaid(session, org.id)  # gate on, balance 0
    orders_before = [c for c in telnyx.creates if c[1].endswith("/number_orders")]

    from app.services.telephony_billing import TelephonyCreditsError

    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        with pytest.raises(TelephonyCreditsError):
            await provisioning_svc.order_number(
                session, settings, org.id, "+12145550990", http=c
            )

    assert [c for c in telnyx.creates if c[1].endswith("/number_orders")] == orders_before, (
        "a refused order must never reach the carrier"
    )
    set_org_context(session, org.id)
    assert (await session.execute(sa.select(sa.func.count(OrgNumber.id)))).scalar_one() == 0


async def test_funded_order_uses_the_sub_account_key_and_charges_the_first_month(
    session, telnyx
):
    settings = managed_settings()
    org = await _provisioned_org(session, settings, telnyx)
    await _enable_prepaid(session, org.id, balance=10_000_000)

    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        number = await provisioning_svc.order_number(
            session, settings, org.id, "+12145550991", http=c
        )

    assert number.e164 == "+12145550991"
    assert number.carrier == "telnyx"
    assert number.provisioning == {"messaging_profile_id": PROFILE_ID}
    # Ordered INSIDE the sub-account, on its messaging profile.
    order_calls = [c for c in telnyx.calls if c[1].endswith("/number_orders")]
    assert order_calls and order_calls[-1][2] == SUB_KEY
    assert telnyx.last_order_body["messaging_profile_id"] == PROFILE_ID

    assert await credits.balance(session, org.id) == 10_000_000 - NUMBER_MRC - NUMBER_SETUP
    # The Inbox rides the same transaction, exactly as api/routes/numbers.py::order does.
    from app.models import Inbox

    set_org_context(session, org.id)
    assert (
        await session.execute(
            sa.select(sa.func.count(Inbox.id)).where(Inbox.number_id == number.id)
        )
    ).scalar_one() == 1


async def test_order_refused_while_the_account_is_still_failed(session, telnyx):
    """MUTATION GUARD: dropping the `status != "active"` check in order_number must fail
    here. The row below has a managed account AND a usable key - only its status says the
    run never finished - so nothing but that check stands between a half-provisioned org
    and a real purchase."""
    settings = managed_settings()
    org = await _provisioned_org(session, settings, telnyx)
    account = await _account(session, org.id)
    account.status = "failed"
    account.last_error = "Telnyx refused the request with HTTP 500"
    await session.commit()
    await _enable_prepaid(session, org.id, balance=10_000_000)
    orders_before = [c for c in telnyx.creates if c[1].endswith("/number_orders")]

    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        with pytest.raises(FeatureUnavailableError) as exc:
            await provisioning_svc.order_number(session, settings, org.id, "+12145550995", http=c)

    assert "not ready" in str(exc.value)
    assert [c for c in telnyx.creates if c[1].endswith("/number_orders")] == orders_before
    set_org_context(session, org.id)
    assert (await session.execute(sa.select(sa.func.count(OrgNumber.id)))).scalar_one() == 0


async def test_order_refused_before_setup_completes(session, telnyx):
    settings = managed_settings()
    org = await _org_row(session)  # never provisioned
    async with httpx.AsyncClient(transport=telnyx.transport()) as c:
        with pytest.raises(FeatureUnavailableError) as exc:
            await provisioning_svc.order_number(session, settings, org.id, "+12145550992", http=c)
    assert "not ready" in str(exc.value)


# ======================================================================================
# Routes
# ======================================================================================
async def _client(engine, settings):
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_routes_are_503_when_the_flag_is_off(engine):
    async with await _client(engine, managed_settings(telephony_managed_enabled=False)) as c:
        token = await register_and_login(c, "mt-off@example.com")
        org = await create_org(c, token, "MT Off")
        h = auth_headers(token, org["id"])
        for method, url in (
            ("post", "/api/v1/telephony/setup"),
            ("get", "/api/v1/telephony/status"),
            ("post", "/api/v1/telephony/numbers/order"),
        ):
            r = await getattr(c, method)(
                url, headers=h, **({"json": {"e164": "+12145550993"}} if method == "post" else {})
            )
            assert r.status_code == 503, (url, r.text)
            assert r.json()["error"]["code"] == "feature_unavailable"


async def test_routes_are_503_when_the_master_key_is_empty(engine):
    async with await _client(engine, managed_settings(telnyx_master_api_key="")) as c:
        token = await register_and_login(c, "mt-nokey@example.com")
        org = await create_org(c, token, "MT NoKey")
        h = auth_headers(token, org["id"])
        for method, url in (
            ("post", "/api/v1/telephony/setup"),
            ("get", "/api/v1/telephony/status"),
            ("post", "/api/v1/telephony/numbers/order"),
        ):
            r = await getattr(c, method)(
                url, headers=h, **({"json": {"e164": "+12145550996"}} if method == "post" else {})
            )
            assert r.status_code == 503, (url, r.text)
            assert "master API key" in r.text


async def test_status_never_leaks_a_key_or_a_raw_telnyx_id(engine, telnyx, monkeypatch):
    settings = managed_settings()
    monkeypatch.setattr(
        provisioning_svc.TelnyxManagedClient, "http", _mock_client_factory(telnyx)
    )

    async with await _client(engine, settings) as c:
        token = await register_and_login(c, "mt-status@example.com")
        org = await create_org(c, token, "MT Status")
        h = auth_headers(token, org["id"])

        r = await c.post("/api/v1/telephony/setup", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "active"
        assert [s["key"] for s in body["steps"]] == list(provisioning_svc.STEPS)
        assert all(s["done"] for s in body["steps"])

        r = await c.get("/api/v1/telephony/status", headers=h)
        assert r.status_code == 200, r.text
        raw = r.text
        for secret in (MASTER_KEY, SUB_KEY, MANAGED_ID, PROFILE_ID):
            assert secret not in raw, f"{secret} leaked into the status response"


async def test_org_b_cannot_read_or_order_into_org_a(engine, telnyx, monkeypatch):
    settings = managed_settings()
    monkeypatch.setattr(
        provisioning_svc.TelnyxManagedClient, "http", _mock_client_factory(telnyx)
    )

    async with await _client(engine, settings) as c:
        token_a = await register_and_login(c, "mt-a@example.com")
        org_a = await create_org(c, token_a, "MT A")
        assert (
            await c.post("/api/v1/telephony/setup", headers=auth_headers(token_a, org_a["id"]))
        ).status_code == 200

        token_b = await register_and_login(c, "mt-b@example.com")
        org_b = await create_org(c, token_b, "MT B")

        # B pointed at A's org id: refused by the org gate, never answered with A's data.
        r = await c.get("/api/v1/telephony/status", headers=auth_headers(token_b, org_a["id"]))
        assert r.status_code in (403, 404), r.text
        r = await c.post(
            "/api/v1/telephony/numbers/order",
            json={"e164": "+12145550994"},
            headers=auth_headers(token_b, org_a["id"]),
        )
        assert r.status_code in (403, 404), r.text

        # And B's own status is its own - A's provisioning is invisible to it.
        r = await c.get("/api/v1/telephony/status", headers=auth_headers(token_b, org_b["id"]))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "not_started"
        assert not any(s["done"] for s in r.json()["steps"])


def _mock_client_factory(telnyx: FakeTelnyx):
    """Hand the engine a MockTransport-backed client even though the ROUTE (unlike the
    service) takes no injectable http - the route path is exactly where a real outbound
    call would otherwise escape the test suite. Patching the ONE accessor also proves the
    claim the accessor exists for: the step-4 probe goes through the same client, so a
    route-driven setup makes no unmocked outbound request."""

    async def _http(self):  # noqa: ANN001
        if self._http is None:
            self._http = httpx.AsyncClient(transport=telnyx.transport())
        return self._http

    return _http


async def test_provisioned_org_sends_through_the_existing_per_org_registry(session, telnyx, http):
    """The whole design bet: after provisioning, the ORDINARY per-org carrier registry
    builds a Telnyx adapter from the sub-account's credentials. If this ever stops being
    true, someone has quietly introduced a second send path for managed orgs - which is
    exactly what the contract forbids."""
    from app.models import ProviderAccount as PA
    from app.providers import registry_org

    settings = managed_settings()
    org = await _org_row(session)
    await provisioning_svc.provision(session, settings, org.id, http=http)

    set_org_context(session, org.id)
    rows = list(
        (await session.execute(sa.select(PA).where(PA.status == "active"))).scalars().all()
    )
    registry, db_owned = registry_org.build_registry_for_org(settings, rows)

    assert "telnyx" in db_owned, "the managed sub-account must back the org's telnyx adapter"
    adapter = registry.get("telnyx")
    assert adapter.api_key == SUB_KEY
    assert adapter.messaging_profile_id == PROFILE_ID
