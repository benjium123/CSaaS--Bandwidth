import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_sessionmaker
from app.models import Campaign, TenDlcEvent, TenDlcRegistration
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
        assert "Opt-in is not described" in reg.detail


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


async def test_route_204_for_non_object_json(client):
    for body in (b"[]", b'"x"', b"null", b"5"):
        r = await client.post("/api/v1/webhooks/telnyx/10dlc", content=body)
        assert r.status_code == 204, body


async def test_unverified_event_does_not_overwrite_verified_rejection(
    session, stripe_stub, telnyx
):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    real = _ev(
        "10dlc.campaign.update",
        {"campaignId": "C456", "status": "REJECTED", "reasons": [{"description": "Real reason"}]},
        "v1",
    )
    forged = _ev(
        "10dlc.campaign.update",
        {"campaignId": "C456", "status": "REJECTED", "reasons": [{"description": "Forged"}]},
        "v2",
    )
    async with sm() as s:
        await tendlc.handle_carrier_event(s, _settings(), real, verified=True, client=telnyx.client)
    async with sm() as s:
        await tendlc.handle_carrier_event(
            s, _settings(), forged, verified=False, client=telnyx.client
        )
    async with sm() as s:
        reg = (
            await s.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.id == reg_id)
                .execution_options(**_UNSCOPED)
            )
        ).scalar_one()
        assert reg.rejection["reasons"] == ["Real reason"] and reg.rejection["verified"] is True


async def test_poll_only_rejection_keeps_the_generic_words(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_rejected"
    async with sm() as s:
        reg = (
            await s.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.id == reg_id)
                .execution_options(**_UNSCOPED)
            )
        ).scalar_one()
        assert reg.rejection is None
        assert reg.detail.startswith("The carriers did not approve this campaign")


async def test_a_failed_number_assignment_is_on_the_timeline(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.fail_assignment = True
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    sa.select(TenDlcEvent)
                    .where(TenDlcEvent.subject == "number")
                    .execution_options(**_UNSCOPED)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1 and rows[0].status == "FAILED"
    assert rows[0].reasons and rows[0].registration_id == reg_id


async def test_a_long_rejection_still_moves_the_stage_and_fits_last_error(
    session, stripe_stub, telnyx
):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    ev = _ev(
        "10dlc.campaign.update",
        {
            "campaignId": "C456",
            "status": "REJECTED",
            "reasons": [{"description": c * 400} for c in "abc"],
        },
        "long1",
    )
    async with sm() as s:
        await tendlc.handle_carrier_event(s, _settings(), ev, verified=True, client=telnyx.client)
    assert await _stage(reg_id) == "campaign_rejected"
    async with sm() as s:
        errors = (
            (
                await s.execute(
                    sa.select(Campaign.last_error).execution_options(**_UNSCOPED)
                )
            )
            .scalars()
            .all()
        )
    assert any(e for e in errors) and all(len(e or "") <= 500 for e in errors)


async def test_a_repeating_assignment_failure_is_one_timeline_row(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.fail_assignment = True
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    sa.select(TenDlcEvent)
                    .where(TenDlcEvent.subject == "number")
                    .execution_options(**_UNSCOPED)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_filings_carry_our_webhook_url_on_https(session, stripe_stub, telnyx):
    import json

    settings = _settings().model_copy(update={"public_base_url": "https://api.ringlite.test"})
    org, brand, campaign, _ = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    await _pay(session, reg)
    sm = get_sessionmaker()
    await tendlc.tick(sm, settings, client=telnyx.client)
    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    await tendlc.tick(sm, settings, client=telnyx.client)
    url = "https://api.ringlite.test/api/v1/webhooks/telnyx/10dlc"
    brand_body = json.loads(telnyx.posts("/10dlc/brand")[0].content)
    camp_body = json.loads(telnyx.posts("/10dlc/campaignBuilder")[0].content)
    assert brand_body["webhookURL"] == url and camp_body["webhookURL"] == url
    assert brand_body["webhookFailoverURL"] == url


def test_no_webhook_fields_without_https():
    settings = _settings().model_copy(update={"public_base_url": "http://localhost:8000"})
    assert tendlc_events.webhook_fields(settings) == {}


async def test_a_brand_rejection_does_not_leak_into_a_later_campaign_rejection(
    session, stripe_stub, telnyx
):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    async with sm() as s:
        reg = (
            await s.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.id == reg_id)
                .execution_options(**_UNSCOPED)
            )
        ).scalar_one()
        set_org_context(s, reg.org_id)
        reg.rejection = {"subject": "brand", "reasons": ["Bad EIN"], "verified": True, "at": "x"}
        await s.commit()
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_rejected"
    async with sm() as s:
        reg = (
            await s.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.id == reg_id)
                .execution_options(**_UNSCOPED)
            )
        ).scalar_one()
        assert "Bad EIN" not in reg.detail
        assert reg.detail.startswith("The carriers did not approve this campaign")


async def test_route_400_for_deeply_nested_json(client):
    r = await client.post("/api/v1/webhooks/telnyx/10dlc", content=b"[" * 100000)
    assert r.status_code == 400


async def test_route_matched_campaign_event_is_204_and_recorded(
    client, session, stripe_stub, telnyx
):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    r = await client.post(
        "/api/v1/webhooks/telnyx/10dlc",
        json={
            "data": {
                "id": "route-1",
                "event_type": "10dlc.campaign.update",
                "payload": {"campaignId": "C456", "status": "ACCEPTED"},
            }
        },
    )
    assert r.status_code == 204
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    sa.select(TenDlcEvent)
                    .where(TenDlcEvent.registration_id == reg_id)
                    .execution_options(**_UNSCOPED)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
