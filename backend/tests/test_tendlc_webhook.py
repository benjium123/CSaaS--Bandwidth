import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.db.session import get_sessionmaker
from app.models import TenDlcEvent, TenDlcRegistration
from app.services import tendlc, tendlc_events
from tests.test_tendlc_self_serve import (  # noqa: F401
    _checkout,
    _pay,
    _settings,
    _stage,
    _workspace,
    stripe_stub,
    telnyx,
)

_UNSCOPED = {ALLOW_UNSCOPED_KEY: True}


def _ev(event_type, payload, event_id):
    return tendlc_events.parse(
        {"data": {"id": event_id, "event_type": event_type, "payload": payload}}
    )


async def _to_campaign_filed(session, telnyx):
    org, brand, campaign, number = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    reg_id = reg.id
    await _pay(session, reg)
    sm = get_sessionmaker()
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_filed"
    return reg_id, sm


def test_parse_ignores_non_list_reasons():
    ev = _ev("10dlc.campaign.update", {"campaignId": "C1", "reasons": "oops"}, "x")
    assert ev is not None and ev.reasons == []


async def test_unknown_id_is_unmatched_and_writes_nothing(session):
    ev = _ev("10dlc.campaign.update", {"campaignId": "NOPE", "status": "ACCEPTED"}, "e0")
    sm = get_sessionmaker()
    async with sm() as s:
        assert await tendlc.handle_carrier_event(s, _settings(), ev, verified=True) == "unmatched"
    async with sm() as s:
        count = (
            await s.execute(
                sa.select(sa.func.count(TenDlcEvent.id)).execution_options(**_UNSCOPED)
            )
        ).scalar_one()
        assert count == 0


async def test_forged_approval_changes_nothing_while_telnyx_says_pending(
    session, stripe_stub, telnyx
):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    ev = _ev(
        "10dlc.campaign.update",
        {"campaignId": "C456", "type": "MNO_REVIEW", "status": "ACCEPTED"},
        "e1",
    )
    async with sm() as s:
        out = await tendlc.handle_carrier_event(
            s, _settings(), ev, verified=False, client=telnyx.client
        )
    assert out == "recorded"
    assert await _stage(reg_id) == "campaign_filed"  # FakeTelnyx still says TCR_PENDING


async def test_rejection_reason_is_saved_and_the_status_rechecked(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    ev = _ev(
        "10dlc.campaign.update",
        {
            "campaignId": "C456",
            "type": "MNO_REVIEW",
            "status": "REJECTED",
            "reasons": [{"description": "Opt-in is not described"}],
        },
        "e2",
    )
    async with sm() as s:
        await tendlc.handle_carrier_event(
            s, _settings(), ev, verified=True, client=telnyx.client
        )
    assert await _stage(reg_id) == "campaign_rejected"
    async with sm() as s:
        reg = (
            await s.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.id == reg_id)
                .execution_options(**_UNSCOPED)
            )
        ).scalar_one()
        assert reg.rejection["reasons"] == ["Opt-in is not described"]


async def test_the_same_event_twice_is_one_row(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    ev = _ev("10dlc.campaign.update", {"campaignId": "C456", "status": "ACCEPTED"}, "e3")
    async with sm() as s:
        assert (
            await tendlc.handle_carrier_event(
                s, _settings(), ev, verified=True, client=telnyx.client
            )
            == "recorded"
        )
    async with sm() as s:
        assert (
            await tendlc.handle_carrier_event(
                s, _settings(), ev, verified=True, client=telnyx.client
            )
            == "duplicate"
        )


async def test_route_answers_204_and_400(client):
    r = await client.post("/api/v1/webhooks/telnyx/10dlc", content=b"not json")
    assert r.status_code == 400
    r = await client.post(
        "/api/v1/webhooks/telnyx/10dlc",
        json={
            "data": {
                "id": "x",
                "event_type": "10dlc.brand.update",
                "payload": {"brandId": "NOPE"},
            }
        },
    )
    assert r.status_code == 204
