"""Phase 5: the calls API - outbound dispatch, listing, transfer, hangup, tenancy.

Uses FakeVoiceCarrier (defined in test_voice_webhooks.py) so create_call/execute_commands
are deterministic and inspectable - the webhook-ingestion tests exercise the REAL adapters
instead, which is where parse/verify actually need proving.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import FeatureUnavailableError
from app.main import create_app
from app.models import CallLeg
from app.providers.voice import CreateCallResult
from app.providers.voice import Gather as GatherCmd
from app.providers.voice import Hangup as HangupCmd
from app.providers.voice import VoiceEvent as VE
from app.services import calls as calls_svc
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
    webhook_auth_headers,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

OUR = "+12145550100"
THEIRS = "+19725550199"

ANSWER_URL = "/api/v1/webhooks/bandwidth/voice/answer"
DISCONNECT_URL = "/api/v1/webhooks/bandwidth/voice/disconnect"


async def _fire(client: httpx.AsyncClient, fake: FakeVoiceCarrier, url: str, event: VE):
    """Drive one VoiceEvent through the REAL webhook route - FakeVoiceCarrier.parse_voice_
    webhook ignores the raw body and returns whatever `events_to_return` was just set to,
    but the route itself (org resolution, adoption, apply_voice_event) runs for real."""
    fake.events_to_return = [event]
    return await client.post(url, content=b"{}", headers=webhook_auth_headers())


@pytest.fixture
async def app_with_voice_carrier(engine):
    """App wired with a FakeVoiceCarrier named 'bandwidth' - local to this file (not
    imported as a fixture from test_voice_webhooks.py) so ruff does not mistake the test
    functions' `app_with_voice_carrier` parameter for shadowing a module-level import."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


# ---------------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------------
async def test_create_call_dispatches_via_carrier_and_returns_201(app_with_voice_carrier):
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "cc1@example.com", "Org C", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS, "tag": "lead-42"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["direction"] == "outbound"
    assert body["status"] == "initiated"
    assert body["contact_e164"] == THEIRS
    assert body["our_e164"] == OUR
    assert body["tag"] == "lead-42"
    assert len(body["legs"]) == 1
    assert body["legs"][0]["status"] == "dialing"
    assert body["legs"][0]["provider_call_id"] == "leg-1"
    assert fake.create_calls == [
        {"to": THEIRS, "from_": OUR, "machine_detection": "off", "tag": "lead-42"}
    ]


async def test_create_call_rejected_by_carrier_returns_201_with_failed_status(
    app_with_voice_carrier,
):
    client, fake, _ = app_with_voice_carrier
    fake.scripted_results = [CreateCallResult("rejected", None, "no route to destination")]
    token, org, _ = await make_org_with_number(client, "rej1@example.com", "Org R", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["legs"][0]["status"] == "failed"


async def test_create_call_with_from_not_owned_by_org_is_422(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "badfrom@example.com", "Org F", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS, "from": "+19995550000"}, headers=h
    )
    assert r.status_code == 422


async def test_create_call_bad_machine_detection_value_is_422(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "amdbad@example.com", "Org M", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls",
        json={"to": THEIRS, "machine_detection": "sync"},
        headers=h,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------------
async def test_list_and_filter_calls(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "list1@example.com", "Org L", OUR)
    h = auth_headers(token, org["id"])

    await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    await client.post("/api/v1/calls", json={"to": "+19725550001"}, headers=h)

    listed = await client.get("/api/v1/calls", headers=h)
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    filtered = await client.get(
        "/api/v1/calls", params={"contact_e164": THEIRS}, headers=h
    )
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["contact_e164"] == THEIRS

    by_status = await client.get("/api/v1/calls", params={"status": "initiated"}, headers=h)
    assert len(by_status.json()) == 2


async def test_get_call_detail_includes_legs_and_recordings(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "detail1@example.com", "Org D", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]

    detail = await client.get(f"/api/v1/calls/{call_id}", headers=h)
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == call_id
    assert len(body["legs"]) == 1
    assert body["recordings"] == []


async def test_get_missing_call_is_404(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "missing1@example.com", "Org D", OUR)
    h = auth_headers(token, org["id"])

    r = await client.get(f"/api/v1/calls/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404


# ---------------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------------
async def test_org_b_cannot_see_org_as_calls(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token_a, org_a, _ = await make_org_with_number(client, "tenA@example.com", "Org A", OUR)
    h_a = auth_headers(token_a, org_a["id"])
    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h_a)
    call_id = created.json()["id"]

    token_b, org_b, _ = await make_org_with_number(
        client, "tenB@example.com", "Org B", "+12145550101"
    )
    h_b = auth_headers(token_b, org_b["id"])

    listed = await client.get("/api/v1/calls", headers=h_b)
    assert listed.json() == []

    detail = await client.get(f"/api/v1/calls/{call_id}", headers=h_b)
    assert detail.status_code == 404


# ---------------------------------------------------------------------------------
# Hangup
# ---------------------------------------------------------------------------------
async def test_hangup_calls_carrier_execute_commands_with_the_active_leg(app_with_voice_carrier):
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "hup1@example.com", "Org H", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]
    provider_id = created.json()["legs"][0]["provider_call_id"]

    r = await client.post(f"/api/v1/calls/{call_id}/hangup", headers=h)
    assert r.status_code == 200, r.text
    assert fake.execute_calls[-1][0] == provider_id
    assert isinstance(fake.execute_calls[-1][1][0], HangupCmd)


async def test_hangup_unavailable_carrier_returns_422_and_explains(app_with_voice_carrier):
    client, fake, _ = app_with_voice_carrier
    fake.execute_commands_error = FeatureUnavailableError(
        "Bandwidth delivers voice commands in webhook responses"
    )
    token, org, _ = await make_org_with_number(client, "hup2@example.com", "Org H", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]

    r = await client.post(f"/api/v1/calls/{call_id}/hangup", headers=h)
    assert r.status_code == 422
    assert "webhook responses" in r.json()["error"]["message"]


# ---------------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------------
async def test_transfer_unavailable_carrier_returns_422_and_does_not_persist_a_leg(
    app_with_voice_carrier,
):
    client, fake, _ = app_with_voice_carrier
    fake.execute_commands_error = FeatureUnavailableError("no mid-call commands")
    token, org, _ = await make_org_with_number(client, "xfer2@example.com", "Org X", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]

    r = await client.post(
        f"/api/v1/calls/{call_id}/transfer", json={"to": "+19725550001"}, headers=h
    )
    assert r.status_code == 422

    detail = await client.get(f"/api/v1/calls/{call_id}", headers=h)
    assert all(leg["reason"] != "transfer" for leg in detail.json()["legs"]), (
        "a transfer we KNOW failed must not leave an orphan leg behind"
    )


async def test_blind_transfer_end_to_end_completes_only_when_last_leg_ends(
    app_with_voice_carrier, session
):
    """transfer command -> new leg -> old leg hungup -> call still answered -> second leg
    hungup -> call completed with BOTH legs in history.

    F1/F1b/F9a/F2: the B-leg's FIRST event is driven through the REAL webhook route (no
    direct DB writes) - the transfer target's own number is never one of ours, so org
    resolution must fall back to `from` (F1a/F9a, our own number) and then ADOPT the
    pending transfer leg (F1b) rather than mistake it for a brand new inbound call.
    """
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "xfer1@example.com", "Org X", OUR)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    assert created.status_code == 201, created.text
    call_id = created.json()["id"]
    original_provider_id = created.json()["legs"][0]["provider_call_id"]

    # The original leg must be answered before there is anything to transfer.
    r = await _fire(
        client,
        fake,
        ANSWER_URL,
        VE(
            event_type="call_answered",
            provider_call_id=original_provider_id,
            provider_event_id="ev-answer-1",
            to=THEIRS,
            from_=OUR,
        ),
    )
    assert r.status_code == 200
    detail = await client.get(f"/api/v1/calls/{call_id}", headers=h)
    assert detail.json()["status"] == "answered"

    transfer_target = "+19725550001"
    xfer = await client.post(
        f"/api/v1/calls/{call_id}/transfer", json={"to": transfer_target}, headers=h
    )
    assert xfer.status_code == 200, xfer.text
    assert fake.execute_calls[-1][0] == original_provider_id

    body = xfer.json()
    assert len(body["legs"]) == 2
    transfer_leg = next(leg for leg in body["legs"] if leg["reason"] == "transfer")
    assert transfer_leg["status"] == "created"
    assert transfer_leg["provider_call_id"] is None

    # The carrier's FIRST event for the B-leg: `to` is the transfer target (never one of
    # ours), `from` is OUR number - org resolution must use the `from` fallback, then
    # ADOPT this pending transfer leg rather than create a second Call.
    new_provider_id = "leg-transfer-confirmed"
    r = await _fire(
        client,
        fake,
        ANSWER_URL,
        VE(
            event_type="call_answered",
            provider_call_id=new_provider_id,
            provider_event_id="ev-xfer-answer",
            to=transfer_target,
            from_=OUR,
        ),
    )
    assert r.status_code == 200

    set_org_context(session, org_id)
    adopted = (
        await session.execute(sa.select(CallLeg).where(CallLeg.reason == "transfer"))
    ).scalar_one()
    assert adopted.provider_call_id == new_provider_id, "the B-leg must be ADOPTED"
    all_legs = (
        (await session.execute(sa.select(CallLeg).where(CallLeg.call_id == adopted.call_id)))
        .scalars()
        .all()
    )
    assert len(all_legs) == 2, "adoption must never create a third leg"

    # The OLD leg hangs up. The call must stay up: the new leg is still live.
    r = await _fire(
        client,
        fake,
        DISCONNECT_URL,
        VE(
            event_type="call_hungup",
            provider_call_id=original_provider_id,
            provider_event_id="ev-hangup-1",
            to=THEIRS,
            from_=OUR,
            hangup_cause="normal-clearing",
        ),
    )
    assert r.status_code == 200
    detail = await client.get(f"/api/v1/calls/{call_id}", headers=h)
    assert detail.json()["status"] == "answered", (
        "the surviving transfer leg must keep the call alive"
    )

    # The NEW leg hangs up. NOW the call completes.
    r = await _fire(
        client,
        fake,
        DISCONNECT_URL,
        VE(
            event_type="call_hungup",
            provider_call_id=new_provider_id,
            provider_event_id="ev-hangup-2",
            to=transfer_target,
            from_=OUR,
            hangup_cause="normal-clearing",
        ),
    )
    assert r.status_code == 200
    detail = await client.get(f"/api/v1/calls/{call_id}", headers=h)
    assert detail.json()["status"] == "completed"
    assert len(detail.json()["legs"]) == 2, "both legs must remain in history"


async def test_transfer_with_no_active_leg_is_a_conflict(app_with_voice_carrier, session):
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "xfer3@example.com", "Org X", OUR)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]
    provider_id = created.json()["legs"][0]["provider_call_id"]

    # Hang the only leg up before anyone tries to transfer it.
    await calls_svc.apply_voice_event(
        session,
        "bandwidth",
        VE(event_type="call_hungup", provider_call_id=provider_id, provider_event_id="ev-1"),
        org_id,
    )

    r = await client.post(
        f"/api/v1/calls/{call_id}/transfer", json={"to": "+19725550001"}, headers=h
    )
    assert r.status_code == 409


async def test_call_with_no_org_number_available_is_422(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "noflow@example.com", "Org N", OUR)
    h = auth_headers(token, org["id"])
    # Release the only number so nothing is active.
    numbers = (await client.get("/api/v1/numbers", headers=h)).json()
    await client.delete(f"/api/v1/numbers/{numbers[0]['id']}", headers=h)

    r = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    assert r.status_code == 422


# ---------------------------------------------------------------------------------
# F15: carrier override is honoured or refused, never silently substituted
# ---------------------------------------------------------------------------------
async def test_create_call_carrier_mismatch_with_explicit_from_is_422(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "mismatch1@example.com", "Org M", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls",
        json={"to": THEIRS, "from": OUR, "carrier": "telnyx"},
        headers=h,
    )
    assert r.status_code == 422
    assert "does not own" in r.json()["error"]["message"]


async def test_create_call_carrier_matching_explicit_from_is_honoured(app_with_voice_carrier):
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "match1@example.com", "Org M", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls",
        json={"to": THEIRS, "from": OUR, "carrier": "bandwidth"},
        headers=h,
    )
    assert r.status_code == 201, r.text


async def test_create_call_carrier_filter_without_from_picks_a_number_on_that_carrier(
    app_with_voice_carrier,
):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "carrfilter1@example.com", "Org C", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS, "carrier": "bandwidth"}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["our_e164"] == OUR


async def test_create_call_carrier_filter_with_no_matching_number_is_422(app_with_voice_carrier):
    client, _, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "carrfilter2@example.com", "Org C", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS, "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------------
# F3e: DTMF gather
# ---------------------------------------------------------------------------------
async def test_gather_calls_carrier_execute_commands_with_the_active_leg(app_with_voice_carrier):
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "gather1@example.com", "Org G", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]
    provider_id = created.json()["legs"][0]["provider_call_id"]

    r = await client.post(
        f"/api/v1/calls/{call_id}/gather",
        json={"max_digits": 4, "terminating_digit": "*", "timeout_seconds": 5,
              "prompt_text": "Enter your PIN", "action_tag": "pin-entry"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert fake.execute_calls[-1][0] == provider_id
    gather_cmd = fake.execute_calls[-1][1][0]
    assert isinstance(gather_cmd, GatherCmd)
    assert gather_cmd.max_digits == 4
    assert gather_cmd.terminating_digit == "*"
    assert gather_cmd.timeout_seconds == 5
    assert gather_cmd.action_tag == "pin-entry"
    assert gather_cmd.prompt is not None and gather_cmd.prompt.text == "Enter your PIN"


async def test_gather_unavailable_carrier_returns_422_and_explains(app_with_voice_carrier):
    client, fake, _ = app_with_voice_carrier
    fake.execute_commands_error = FeatureUnavailableError(
        "Bandwidth delivers voice commands in webhook responses"
    )
    token, org, _ = await make_org_with_number(client, "gather2@example.com", "Org G", OUR)
    h = auth_headers(token, org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]

    r = await client.post(f"/api/v1/calls/{call_id}/gather", json={}, headers=h)
    assert r.status_code == 422
    assert "webhook responses" in r.json()["error"]["message"]


async def test_gather_with_no_active_leg_is_a_conflict(app_with_voice_carrier, session):
    client, fake, _ = app_with_voice_carrier
    token, org, _ = await make_org_with_number(client, "gather3@example.com", "Org G", OUR)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])

    created = await client.post("/api/v1/calls", json={"to": THEIRS}, headers=h)
    call_id = created.json()["id"]
    provider_id = created.json()["legs"][0]["provider_call_id"]

    await calls_svc.apply_voice_event(
        session,
        "bandwidth",
        VE(event_type="call_hungup", provider_call_id=provider_id, provider_event_id="ev-1"),
        org_id,
    )

    r = await client.post(f"/api/v1/calls/{call_id}/gather", json={}, headers=h)
    assert r.status_code == 409
