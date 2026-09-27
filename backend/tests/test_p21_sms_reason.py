"""Phase 21 design item 2, SMS half: every outbound text can say WHY it went out that way.

These run against the REAL send path - `POST /api/v1/messages` -> `routing.plan_route` ->
`messaging.dispatch_with_failover` - not against a hand-built plan. That matters: the whole
value of `messages.route_reason` is that it is written by the code that actually chose the
route, so a test that constructed its own plan would prove nothing about production.

The fixture is the two-carrier one from `test_failover.py` for the same reason its gate uses
it: a single-carrier deployment cannot distinguish "sent via the only thing we have" from
"sent via the thing we picked".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.compliance import telnyx_approval
from app.db.base import set_org_context
from app.main import create_app
from app.models import OrgNumber
from app.models.numbers import Brand, Campaign
from app.providers.domain import CarrierError, SendResult
from app.providers.health import HealthRegistry
from app.providers.registry import CarrierRegistry
from tests.conftest import FakeCarrier, auth_headers, make_org_with_number

PRIMARY_NUM = "+12145550100"
FALLBACK_NUM = "+19725550300"
CONTACT = "+19725559999"


class FakeClock:
    """Deterministic, manually advanced - same shape as test_failover.py's."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def multi(engine, webhook_settings, clock):
    """bandwidth (primary) + telnyx (fallback), both fake, sharing ONE injected clock."""
    application = create_app(webhook_settings)
    bandwidth = FakeCarrier(name="bandwidth")
    telnyx = FakeCarrier(name="telnyx")
    health = HealthRegistry(clock=clock)
    registry = CarrierRegistry(
        {"bandwidth": bandwidth, "telnyx": telnyx}, primary="bandwidth", health=health
    )
    application.state.carriers = registry
    application.state.carrier = bandwidth
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, registry, bandwidth, telnyx


async def _register_telnyx_campaign(session, org_id, number_e164: str) -> None:
    """Commit 81df130 "feat(telnyx): enforce carrier-backed registration" made an
    unregistered Telnyx number a hard refusal for every send path, including automatic
    failover - so a test that actually expects a send to land ON the Telnyx fallback
    needs the same approved, carrier-confirmed campaign a real registration + Telnyx
    refresh would leave behind (mirrors tests/test_carrier_routing.py's
    ``_register_telnyx_campaign`` / tests/test_failover.py's copy of it)."""
    org_uuid = uuid.UUID(str(org_id))
    set_org_context(session, org_uuid)
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == number_e164))
    ).scalar_one()
    carrier_ref = f"CR-{uuid.uuid4().hex[:12]}"
    evidence = telnyx_approval.build_evidence(
        state=telnyx_approval.STATE_APPROVED,
        carrier_id=carrier_ref,
        checked_at=datetime.now(timezone.utc),
        source=telnyx_approval.SOURCE_STATUS_DECISION,
    )
    brand = Brand(id=uuid.uuid4(), name=f"Brand {number_e164}")
    session.add(brand)
    await session.flush()
    campaign = Campaign(
        id=uuid.uuid4(),
        brand_id=brand.id,
        name=f"Campaign {number_e164}",
        status="approved",
        carrier_refs={"telnyx": carrier_ref, telnyx_approval.TELNYX_APPROVAL_KEY: evidence},
    )
    session.add(campaign)
    await session.flush()
    number.campaign_id = campaign.id
    number.provisioning = {
        **(number.provisioning or {}),
        "telnyx_campaign_assignment": {
            "state": "assigned",
            "campaign_id": str(campaign.id),
            "carrier_id": carrier_ref,
        },
    }
    await session.commit()


async def _org_with_numbers(client, email: str, session=None) -> tuple[str, dict]:
    token, org, _ = await make_org_with_number(client, email, "Org SR", PRIMARY_NUM)
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": FALLBACK_NUM, "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 201, r.text
    if session is not None:
        await _register_telnyx_campaign(session, org["id"], FALLBACK_NUM)
    return token, org


async def _set_policy(client, h, **fields) -> dict:
    r = await client.patch("/api/v1/routing/policy", json=fields, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_sms_send_records_route_reason_sentence(multi):
    """A plain successful send carries a plain sentence naming the provider it used."""
    client, _registry, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client, "sr1@example.com")
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hello"}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted"

    reason = body["route_reason"]
    assert reason, "every outbound message must be able to say why it went out that way"
    assert reason.startswith("Sent via "), reason
    # The sentence names the provider that ACTUALLY carried it, not a guess.
    carrier_that_sent = "Bandwidth" if bandwidth.sent else "Telnyx"
    assert carrier_that_sent in reason, reason
    # Plain words only - no engineering vocabulary reaches a customer.
    for banned in ("carrier", "breaker", "score", "candidate", "None", "null"):
        assert banned not in reason, f"{banned!r} leaked into a customer-facing sentence"

    # And it is PERSISTED, not merely computed for this one response.
    listed = await client.get(f"/api/v1/messages/{body['id']}", headers=h)
    if listed.status_code == 200:
        assert listed.json()["route_reason"] == reason


async def test_sms_failover_records_failed_over_sentence(multi, session):
    """When the first provider's credential is dead and the send crosses to another, the
    sentence says so by name - that is the question a surprised operator actually asks."""
    client, _registry, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client, "sr2@example.com", session)
    h = auth_headers(token, org["id"])
    await _set_policy(client, h, allow_cross_carrier_failover=True)

    # A dead credential on the primary: carrier-own fault, so the walk continues.
    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("auth", "401", retryable=False))
    ]

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "failover please"}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["from_e164"] == FALLBACK_NUM, "it really did move to the other provider"
    assert len(bandwidth.sent) == 1
    assert len(telnyx.sent) == 1

    reason = body["route_reason"]
    assert reason == "Failed over to Telnyx — Bandwidth unavailable", reason


async def test_sms_route_reason_is_recorded_even_when_the_send_is_rejected(multi):
    """A message that never lands still explains where it TRIED to go. That is precisely
    when somebody goes looking, so the sentence is written before the attempt, not after."""
    client, _registry, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client, "sr3@example.com")
    h = auth_headers(token, org["id"])

    # invalid_request is OUR bug: it stops the walk, so nothing else is tried and the
    # message ends rejected on the first provider.
    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("invalid_request", "4302", retryable=False))
    ]
    telnyx.scripted = [
        SendResult("rejected", None, CarrierError("invalid_request", "4302", retryable=False))
    ]

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "doomed"}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["route_reason"], "a rejected message must still say where it tried to go"
    assert body["route_reason"].startswith("Sent via "), body["route_reason"]
