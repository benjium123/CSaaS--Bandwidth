"""P42: LiveKit trunk numbers stay in sync with org_numbers instead of requiring manual
trunk surgery. Unit-level with a hand-rolled `FakeLk` (trunk_sync only ever calls the
four thin LiveKitApi wrappers, so a real LiveKitApi/MockTransport is unnecessary here -
test_voice_plane.py and test_p40_signalwire_trunk.py already exercise the real Twirp
bodies for the other LiveKitApi methods), plus two hook tests against the real
poll_pending_number_orders / DELETE numbers route with trunk_sync patched out.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Org, OrgNumber
from app.providers.registry import CarrierRegistry
from app.services.number_orders import poll_pending_number_orders
from app.voice_plane import trunk_sync
from app.voice_plane.livekit_api import LiveKitApiError
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

TELNYX_INBOUND = "t-in"
TELNYX_OUTBOUND = "t-out"


def make_trunk_settings(**overrides):
    """A settings object with both telnyx trunk ids configured by default (override with
    empty strings to test the unconfigured-carrier path)."""
    base = {
        "livekit_sip_telnyx_inbound_trunk_id": TELNYX_INBOUND,
        "livekit_sip_outbound_trunk_id": TELNYX_OUTBOUND,
    }
    base.update(overrides)
    return make_settings(**base)


class FakeLk:
    """Records every call trunk_sync makes and mutates an in-memory numbers list per
    trunk id, the same way the real LiveKit server would. `raise_on` simulates the
    running v1.8.4 server's `bad_route` 404 on a named trunk id - the get_ and update_
    methods for that trunk id both raise."""

    def __init__(
        self,
        trunks: dict[str, list[str]] | None = None,
        raise_on: set[str] | None = None,
    ):
        self.trunks = {k: list(v) for k, v in (trunks or {}).items()}
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, str, dict | None]] = []

    def _maybe_raise(self, trunk_id: str) -> None:
        if trunk_id in self.raise_on:
            raise LiveKitApiError(404, '{"code":"bad_route"}')

    def _apply(self, trunk_id: str, update: dict) -> None:
        numbers = list(self.trunks.get(trunk_id, []))
        spec = (update or {}).get("numbers", {})
        if "set" in spec:
            numbers = list(spec["set"])
        else:
            for n in spec.get("add", []):
                if n not in numbers:
                    numbers.append(n)
            for n in spec.get("remove", []):
                if n in numbers:
                    numbers.remove(n)
        self.trunks[trunk_id] = numbers

    async def get_sip_inbound_trunk(self, trunk_id: str) -> dict:
        self.calls.append(("get_sip_inbound_trunk", trunk_id, None))
        self._maybe_raise(trunk_id)
        return {"trunk": {"sip_trunk_id": trunk_id, "numbers": list(self.trunks.get(trunk_id, []))}}

    async def get_sip_outbound_trunk(self, trunk_id: str) -> dict:
        self.calls.append(("get_sip_outbound_trunk", trunk_id, None))
        self._maybe_raise(trunk_id)
        return {"trunk": {"sip_trunk_id": trunk_id, "numbers": list(self.trunks.get(trunk_id, []))}}

    async def update_sip_inbound_trunk(self, trunk_id: str, update: dict) -> dict:
        self.calls.append(("update_sip_inbound_trunk", trunk_id, update))
        self._maybe_raise(trunk_id)
        self._apply(trunk_id, update)
        return {}

    async def update_sip_outbound_trunk(self, trunk_id: str, update: dict) -> dict:
        self.calls.append(("update_sip_outbound_trunk", trunk_id, update))
        self._maybe_raise(trunk_id)
        self._apply(trunk_id, update)
        return {}


# ======================================================================================
# ensure_number
# ======================================================================================
async def test_ensure_number_adds_to_both_trunks_when_absent():
    settings = make_trunk_settings()
    lk = FakeLk(trunks={TELNYX_INBOUND: [], TELNYX_OUTBOUND: []})

    result = await trunk_sync.ensure_number(lk, settings, "telnyx", "+12025550001")

    assert result is True
    assert lk.calls == [
        ("get_sip_inbound_trunk", TELNYX_INBOUND, None),
        ("update_sip_inbound_trunk", TELNYX_INBOUND, {"numbers": {"add": ["+12025550001"]}}),
        ("get_sip_outbound_trunk", TELNYX_OUTBOUND, None),
        ("update_sip_outbound_trunk", TELNYX_OUTBOUND, {"numbers": {"add": ["+12025550001"]}}),
    ]
    assert lk.trunks[TELNYX_INBOUND] == ["+12025550001"]
    assert lk.trunks[TELNYX_OUTBOUND] == ["+12025550001"]


async def test_ensure_number_makes_no_update_call_when_already_present():
    settings = make_trunk_settings()
    lk = FakeLk(trunks={TELNYX_INBOUND: ["+12025550001"], TELNYX_OUTBOUND: ["+12025550001"]})

    result = await trunk_sync.ensure_number(lk, settings, "telnyx", "+12025550001")

    assert result is False
    assert lk.calls == [
        ("get_sip_inbound_trunk", TELNYX_INBOUND, None),
        ("get_sip_outbound_trunk", TELNYX_OUTBOUND, None),
    ]
    assert not any(name.startswith("update_") for name, _, _ in lk.calls)


async def test_ensure_number_is_silent_noop_when_carrier_has_no_configured_trunk():
    settings = make_settings()  # no livekit_sip_* overrides: every id defaults to ""
    lk = FakeLk()

    result = await trunk_sync.ensure_number(lk, settings, "telnyx", "+12025550001")

    assert result is False
    assert lk.calls == []


async def test_ensure_number_returns_false_and_does_not_raise_on_bad_route():
    settings = make_trunk_settings()
    lk = FakeLk(trunks={TELNYX_INBOUND: [], TELNYX_OUTBOUND: []}, raise_on={TELNYX_INBOUND})

    result = await trunk_sync.ensure_number(lk, settings, "telnyx", "+12025550001")

    assert result is False
    # The outbound trunk was never reached - the whole operation aborts on the first
    # LiveKitApiError, same as the plan's "wrap the whole operation" contract.
    assert lk.calls == [("get_sip_inbound_trunk", TELNYX_INBOUND, None)]


async def test_ensure_number_none_lk_is_a_noop():
    settings = make_trunk_settings()
    assert await trunk_sync.ensure_number(None, settings, "telnyx", "+12025550001") is False


# ======================================================================================
# remove_number
# ======================================================================================
async def test_remove_number_sends_remove_to_both_trunks_when_present():
    settings = make_trunk_settings()
    lk = FakeLk(trunks={TELNYX_INBOUND: ["+12025550001"], TELNYX_OUTBOUND: ["+12025550001"]})

    result = await trunk_sync.remove_number(lk, settings, "telnyx", "+12025550001")

    assert result is True
    assert lk.calls == [
        ("get_sip_inbound_trunk", TELNYX_INBOUND, None),
        ("update_sip_inbound_trunk", TELNYX_INBOUND, {"numbers": {"remove": ["+12025550001"]}}),
        ("get_sip_outbound_trunk", TELNYX_OUTBOUND, None),
        ("update_sip_outbound_trunk", TELNYX_OUTBOUND, {"numbers": {"remove": ["+12025550001"]}}),
    ]
    assert lk.trunks[TELNYX_INBOUND] == []
    assert lk.trunks[TELNYX_OUTBOUND] == []


async def test_remove_number_is_silent_noop_when_carrier_has_no_configured_trunk():
    settings = make_settings()
    lk = FakeLk()

    result = await trunk_sync.remove_number(lk, settings, "signalwire", "+12025550001")

    assert result is False
    assert lk.calls == []


# ======================================================================================
# reconcile
# ======================================================================================
async def test_reconcile_sends_sorted_active_numbers_and_skips_unconfigured_carriers(session):
    settings = make_trunk_settings()  # signalwire trunk ids left unset
    lk = FakeLk(trunks={TELNYX_INBOUND: [], TELNYX_OUTBOUND: []})

    org = Org(id=uuid.uuid4(), name="P42 Reconcile Org", slug=f"p42-rc-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()

    def _num(**kw):
        return OrgNumber(org_id=org.id, **kw)

    set_org_context(session, org.id)
    session.add_all(
        [
            _num(carrier="telnyx", e164="+12025550002", is_active=True, status="active"),
            _num(carrier="telnyx", e164="+12025550001", is_active=True, status="active"),
            _num(carrier="telnyx", e164="+12025550003", is_active=True, status="released"),
            _num(carrier="telnyx", e164="+12025550004", is_active=False, status="active"),
            # signalwire has no trunk id configured - must never be queried/acted on.
            _num(carrier="signalwire", e164="+12025550005", is_active=True, status="active"),
        ]
    )
    await session.commit()
    set_org_context(session, None)

    result = await trunk_sync.reconcile(session, lk, settings)

    expected_update = {"numbers": {"set": ["+12025550001", "+12025550002"]}}
    assert result == {"telnyx": 2}
    assert lk.calls == [
        ("update_sip_inbound_trunk", TELNYX_INBOUND, expected_update),
        ("update_sip_outbound_trunk", TELNYX_OUTBOUND, expected_update),
    ]


async def test_reconcile_none_lk_is_a_noop():
    settings = make_trunk_settings()
    assert await trunk_sync.reconcile(None, None, settings) == {}


# ======================================================================================
# Hook: number order poll settling to active
# ======================================================================================
class _FakeOrderCarrier:
    name = "telnyx"

    def __init__(self, status: str = "active"):
        self.status = status

    async def order_status(self, provider_ref: str):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Result:
            status: str
            detail: str | None = None

        return _Result(status=self.status)


async def test_number_order_poll_settling_active_triggers_ensure_number_once(session, monkeypatch):
    org = Org(id=uuid.uuid4(), name="P42 Org", slug=f"p42-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()

    number_id = uuid.uuid4()
    set_org_context(session, org.id)
    number = OrgNumber(
        id=number_id,
        org_id=org.id,
        e164="+12145550199",
        carrier="telnyx",
        status="pending",
        provider_ref="tx-order-1",
        capabilities={},
    )
    session.add(number)
    await session.commit()
    set_org_context(session, None)

    calls: list[tuple] = []

    async def fake_ensure_number(lk, settings, carrier, e164):
        calls.append((lk, settings, carrier, e164))
        return True

    monkeypatch.setattr(trunk_sync, "ensure_number", fake_ensure_number)

    registry = CarrierRegistry({"telnyx": _FakeOrderCarrier("active")}, primary="telnyx")
    settings = make_trunk_settings()
    sentinel_lk = object()

    polled = await poll_pending_number_orders(
        session, registry, settings=settings, livekit=sentinel_lk
    )

    assert polled == 1
    assert calls == [(sentinel_lk, settings, "telnyx", "+12145550199")]

    session.expire_all()
    status = (
        await session.execute(
            sa.select(OrgNumber.status)
            .where(OrgNumber.id == number_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert status == "active"


async def test_number_order_poll_pending_never_triggers_ensure_number(session, monkeypatch):
    org = Org(id=uuid.uuid4(), name="P42 Org 2", slug=f"p42b-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()

    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12145550198",
            carrier="telnyx",
            status="pending",
            provider_ref="tx-order-2",
            capabilities={},
        )
    )
    await session.commit()
    set_org_context(session, None)

    calls: list[tuple] = []

    async def fake_ensure_number(lk, settings, carrier, e164):
        calls.append((carrier, e164))
        return True

    monkeypatch.setattr(trunk_sync, "ensure_number", fake_ensure_number)

    registry = CarrierRegistry({"telnyx": _FakeOrderCarrier("pending")}, primary="telnyx")
    await poll_pending_number_orders(
        session, registry, settings=make_trunk_settings(), livekit=None
    )

    assert calls == []


# ======================================================================================
# Hook: DELETE /numbers/{id} (release)
# ======================================================================================
async def test_release_route_triggers_remove_number_and_a_failure_does_not_error_response(
    client, monkeypatch
):
    token = await register_and_login(client, "p42-release@example.com")
    org = await create_org(client, token, "P42 Release Org")
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/numbers", json={"e164": "+12145550177", "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 201, r.text
    number_id = r.json()["id"]

    calls: list[tuple] = []

    async def failing_remove_number(lk, settings, carrier, e164):
        calls.append((carrier, e164))
        raise RuntimeError("simulated trunk_sync bug")

    monkeypatch.setattr(trunk_sync, "remove_number", failing_remove_number)

    r = await client.delete(f"/api/v1/numbers/{number_id}", headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "released"
    assert calls == [("telnyx", "+12145550177")]
