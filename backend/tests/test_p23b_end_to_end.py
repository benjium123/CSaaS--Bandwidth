"""P23b end-to-end: the two integration scenarios the phase plan asked for, walking one
call all the way through instead of exercising each seam in isolation (see
P23B_HANDOFF.md and the backend VERDICT's section 6 - both said this test was the one
gap left after the 41 piecewise tests).

Scenario 1: an inbound call on a number answered by an assistant -> the LiveKit
participant-joined hook dispatches the worker -> the worker posts a transcript and an
outcome -> the outcome's mapped extracted field lands on the contact -> the inbox
timeline renders the AI call card.

Scenario 2: an `ai_calls` campaign of three contacts in one tick - one answered (outcome
posted), one hits voicemail (AMD "machine": the dial attempt is flagged `voicemail` and
the config seam still hands the worker the right `voicemail_action`), one on the DNC
list (never dialled at all).

Uses only the existing fakes: FakeLiveKitApi + FakeVoiceCarrier (app_with_assistant,
lifted from test_p23b_assistant.py), the DR-13 `_start_call` injection seam already used
by test_p23b_wiring.py, and the same ORM-row-construction style test_p23b_outcomes.py
and test_dialer.py use for a leg/call/contact that never touches a real carrier.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from random import Random
from urllib.parse import quote

import sqlalchemy as sa

from app.compliance import service as compliance_svc
from app.db.base import set_org_context
from app.models import (
    AgentProfile,
    Call,
    CallLeg,
    Contact,
    ContactPhone,
    DialAttempt,
)
from app.services import agent as agent_svc
from app.services import assistant_dispatch
from app.services import dialer as dialer_svc
from tests.conftest import FROZEN_NOW, auth_headers, make_org_with_number
from tests.test_p23b_assistant import app_with_assistant  # noqa: F401 - fixture by name
from tests.test_p23b_wiring import A, B, C, OUR, _dial_campaign, _readiness, _ready_list
from tests.test_agent_seams import worker_headers, worker_token

THEIRS = "+19725550199"


def _readiness_patch(monkeypatch):
    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(True)

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)


async def test_inbound_assistant_call_flows_to_transcript_outcome_contact_and_timeline(
    app_with_assistant, session, monkeypatch
):
    client, application, api = app_with_assistant
    settings = application.state.settings
    _readiness_patch(monkeypatch)

    token, org, number = await make_org_with_number(
        client, "e2e-inbound@example.com", "Org E2E Inbound", OUR
    )
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Front Desk",
        system_prompt="",
        is_default=True,
        post_call_fields=[
            {"name": "company", "type": "text", "write_to_attribute": "company"},
            {"name": "internal_note", "type": "text"},
        ],
    )
    session.add(profile)
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="Caller", attributes={})
    contact_id = contact.id
    session.add(contact)
    session.add(
        ContactPhone(id=uuid.uuid4(), org_id=org_id, contact_id=contact.id, e164=THEIRS)
    )
    await session.commit()

    # Bind the number to this assistant through the real PATCH route - this is the
    # number -> flow -> profile half of the contract, not a shortcut around it.
    r = await client.patch(
        f"/api/v1/numbers/{number['id']}/answered-by",
        json={"mode": "assistant", "profile_id": str(profile.id)},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    # A brand-new inbound LiveKit room call, exactly as the webhook's own handler would
    # have left it a moment before the participant-joined event this test fires.
    set_org_context(session, org_id)
    room = f"room-{uuid.uuid4()}"
    sip_call_id = f"sip-{uuid.uuid4()}"
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="telnyx",
        status="in_progress",
        extra={"via": "livekit", "room": room},
    )
    session.add(call)
    await session.flush()
    session.add(
        CallLeg(
            id=uuid.uuid4(),
            org_id=org_id,
            call_id=call.id,
            provider_call_id=sip_call_id,
            to_e164=OUR,
            from_e164=THEIRS,
            status="answered",
        )
    )
    await session.commit()

    event = {
        "event": "participant_joined",
        "participant": {"attributes": {"sip.callID": sip_call_id}},
    }
    await assistant_dispatch.on_livekit_event(session, api, settings, event)

    # The worker was actually put in THIS call's room, carrying a call-bound token.
    assert len(api.dispatches) == 1
    dispatch = api.dispatches[0]
    assert dispatch["room"] == room
    metadata = json.loads(dispatch["metadata"])
    assert metadata["call_id"] == str(call.id)
    dispatch_token = metadata["worker_token"]
    assert dispatch_token

    set_org_context(session, org_id)
    await session.refresh(call)
    assert call.extra["assistant"]["profile_id"] == str(profile.id)
    assert call.extra["assistant"]["dispatched"] is True

    # The config seam resolves to the SAME profile the flow named, using the token that
    # actually travelled in the dispatch metadata - not a fresh one minted by the test.
    config = await client.get(
        f"/api/v1/agent/config/{call.id}", headers=worker_headers(dispatch_token)
    )
    assert config.status_code == 200, config.text
    assert config.json()["effective_prompt"] is not None

    # The worker posts a transcript and an outcome over the legacy global-token seams -
    # per D46, /transcript and /outcome are not yet migrated to call-bound tokens.
    transcript = await client.post(
        "/api/v1/agent/transcript",
        json={
            "call_id": str(call.id),
            "segments": [{"role": "agent", "text": "Hello, who am I speaking with?", "at_ms": 0}],
        },
        headers=worker_headers(worker_token()),
    )
    assert transcript.status_code == 200, transcript.text

    outcome = {
        "call_id": str(call.id),
        "summary": "Caller asked about pricing.",
        "disposition": "booked",
        "sentiment": "positive",
        "extracted": {"company": "ACME", "internal_note": "not applied"},
    }
    r_outcome = await client.post(
        "/api/v1/agent/outcome",
        json={"outcomes": [outcome]},
        headers=worker_headers(worker_token()),
    )
    assert r_outcome.status_code == 200, r_outcome.text
    assert r_outcome.json()["results"][0]["applied_attributes"] == ["company"]

    # Column-only select on purpose (test_p23b_outcomes.py's own note applies here too):
    # the app wrote the contact on ITS OWN session, and this session still holds the
    # stale pre-outcome instance from the setup commit above.
    attributes_after = (
        await session.execute(sa.select(Contact.attributes).where(Contact.id == contact_id))
    ).scalar_one()
    assert attributes_after == {"company": "ACME"}

    # And the card lands in the inbox timeline.
    url = f"/api/v1/conversations/{quote(THEIRS, safe='')}/timeline"
    r_timeline = await client.get(
        url, params={"our_e164": OUR}, headers=auth_headers(token, org_id)
    )
    assert r_timeline.status_code == 200, r_timeline.text
    body = r_timeline.json()
    items = body["items"] if isinstance(body, dict) else body
    ai_item = next(
        item for item in items if item.get("kind") == "call" and str(item.get("id")) == str(call.id)
    )
    assert ai_item["assistant"] == {
        "name": "Front Desk",
        "summary": "Caller asked about pricing.",
        "disposition": "booked",
        "sentiment": "positive",
        "has_transcript": True,
    }


async def test_ai_campaign_answered_voicemail_and_dnc_contacts(
    app_with_assistant, session, monkeypatch
):
    """One tick of an ai_calls campaign against three contacts: answered gets an
    outcome, voicemail keeps its own voicemail_action available to the worker, DNC is
    never dialled at all."""
    client, application, _api = app_with_assistant
    settings = application.state.settings
    _readiness_patch(monkeypatch)

    token, org, _number = await make_org_with_number(
        client, "e2e-campaign@example.com", "Org E2E Campaign", OUR
    )
    org_id = uuid.UUID(org["id"])

    answered_contact, voicemail_contact, dnc_contact = A, B, C

    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Dialer Assistant",
        system_prompt="",
        is_default=True,
        voicemail_action="hang_up",  # non-default, so the config assertion below means
        # something rather than matching the field's own default.
    )
    session.add(profile)
    await session.commit()

    lst = await _ready_list(
        session, org_id, [answered_contact, voicemail_contact, dnc_contact]
    )

    set_org_context(session, org_id)
    await compliance_svc.add_dnc(session, org_id, dnc_contact)
    await session.commit()

    # parallel/3 so all three rows are claimed in ONE tick, same reasoning as
    # test_p23b_wiring.py::test_ai_campaign_respects_quiet_hours_consent_and_dnc.
    campaign = await _dial_campaign(
        session,
        org_id,
        lst.id,
        channel="ai_calls",
        agent_profile_id=profile.id,
        dialer_mode="parallel",
        parallel_lines=3,
    )
    await dialer_svc.start_dial_campaign(session, campaign)

    # dialer_tick's parallel mode gathers every claimed row's _start_call CONCURRENTLY on
    # the one shared AsyncSession - a fake that itself adds/commits rows from inside that
    # gather races the session's own state machine (IllegalStateChangeError). So the two
    # calls this scenario needs are created up front, and the fake only ever READS.
    calls_by_contact: dict[str, uuid.UUID] = {
        answered_contact: uuid.uuid4(),
        voicemail_contact: uuid.uuid4(),
    }
    for contact_e164, call_id in calls_by_contact.items():
        session.add(
            Call(
                id=call_id,
                org_id=org_id,
                direction="outbound",
                contact_e164=contact_e164,
                our_e164=OUR,
                carrier="telnyx",
                status="completed",
                extra={
                    "via": "livekit",
                    "room": f"room-{uuid.uuid4()}",
                    "assistant": {"profile_id": str(profile.id), "is_test": False},
                },
                # Backdated well past dialer.py's CAP_WINDOW_HOURS (26h): the pacing gate's
                # _last_dial_at/_dial_recent_count read Call.created_at for THIS org+number,
                # and a real wall-clock default here (today) would read as "just dialed"
                # against the frozen compliance clock (2026-06-15) and starve every row.
                created_at=FROZEN_NOW - timedelta(days=2),
            )
        )
    await session.commit()

    called: list[str] = []

    async def fake_start_call(
        session,
        settings,
        bus,
        api,
        *,
        org_id,
        to_e164,
        from_e164,
        identity,
        **kwargs,
    ):
        called.append(to_e164)
        amd_verdict = "machine" if to_e164 == voicemail_contact else None
        return dialer_svc.DialOutcome(
            status="connected", call_id=calls_by_contact[to_e164], amd_verdict=amd_verdict
        )

    monkeypatch.setattr(dialer_svc, "_start_call", fake_start_call)

    counts = await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN_NOW)
    assert counts["connected"] == 1
    assert counts["voicemail"] == 1

    # The DNC contact was never handed to the dial seam at all.
    assert dnc_contact not in called
    assert set(called) == {answered_contact, voicemail_contact}

    set_org_context(session, org_id)
    rows = {
        row.e164: row
        for row in (
            await session.execute(
                sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id)
            )
        ).scalars().all()
    }
    assert rows[dnc_contact].status == "failed"
    assert rows[dnc_contact].disposition == "blocked"
    assert rows[dnc_contact].call_id is None

    assert rows[answered_contact].status == "connected"
    assert rows[voicemail_contact].status == "voicemail"
    assert rows[voicemail_contact].disposition == "voicemail"

    # Answered: the worker posts its outcome exactly like a real call.
    r_outcome = await client.post(
        "/api/v1/agent/outcome",
        json={
            "outcomes": [
                {
                    "call_id": str(calls_by_contact[answered_contact]),
                    "summary": "Answered and interested.",
                    "disposition": "booked",
                    "sentiment": "positive",
                }
            ]
        },
        headers=worker_headers(worker_token()),
    )
    assert r_outcome.status_code == 200, r_outcome.text
    assert r_outcome.json()["accepted"] == 1

    # Voicemail: the config seam still resolves to THIS profile and honours its
    # voicemail_action, using a token bound to the voicemail call.
    voicemail_call_id = calls_by_contact[voicemail_contact]
    voicemail_token = agent_svc.mint_call_worker_token(
        settings, call_id=voicemail_call_id, org_id=org_id
    )
    r_config = await client.get(
        f"/api/v1/agent/config/{voicemail_call_id}", headers=worker_headers(voicemail_token)
    )
    assert r_config.status_code == 200, r_config.text
    assert r_config.json()["voicemail_action"] == "hang_up"
