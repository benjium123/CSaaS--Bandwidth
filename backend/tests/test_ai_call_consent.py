"""AI outbound-call consent (TCPA / FCC Feb 2024).

Since the FCC's Feb 2024 declaratory ruling an AI voice is an "artificial voice" under the
TCPA, so an ``ai_calls`` campaign may only dial a contact with a recorded PRIOR consent for
AI calls - consent to be called by a person (``voice``) is NOT consent to be called by an
AI. These tests pin the service primitive, the two console endpoints, and the dialer gate;
a plain ``voice`` campaign (a human dialing) is deliberately unaffected.

Follows tests/test_p23b_end_to_end.py's fixtures/imports and reuses the list/campaign
helpers from tests/test_p23b_wiring.py.

NOTE: the "403 for a user without compliance:manage" case is not covered here - no test in
this repository's context shows how to create an org member that lacks that permission
(registration always makes its creator an owner), so inventing one would be guesswork about
the invite/role seam rather than a real assertion.
"""

from __future__ import annotations

import uuid
from random import Random

import sqlalchemy as sa

from app.compliance import service as compliance_svc
from app.db.base import set_org_context
from app.models import AgentProfile, DialAttempt
from app.services import dialer as dialer_svc
from tests.conftest import FROZEN_NOW, auth_headers, make_org_with_number
from tests.test_p23b_wiring import OUR, A, B, C, _dial_campaign, _ready_list


async def test_ai_call_consent_service_true_then_revoke(app_with_loopback, session):
    """false -> opt_in -> true -> opt_out -> false: the LATEST ai_voice event wins."""
    client, _carrier, _app = app_with_loopback
    _token, org, _number = await make_org_with_number(
        client, "ai-consent-svc@example.com", "Org AI Consent", OUR
    )
    org_id = uuid.UUID(org["id"])
    contact = A

    set_org_context(session, org_id)
    assert await compliance_svc.has_ai_call_consent(session, contact) is False

    await compliance_svc.record_ai_call_consent(
        session,
        org_id,
        contact,
        granted=True,
        source="manual",
        evidence="web form ringlite.io/quote 2026-10-01",
        actor_user_id=None,
    )
    await session.commit()
    assert await compliance_svc.has_ai_call_consent(session, contact) is True

    # An opt-out recorded AFTER an opt-in wins: consent is the LATEST event, not any event.
    await compliance_svc.record_ai_call_consent(
        session,
        org_id,
        contact,
        granted=False,
        source="manual",
        evidence="contact emailed asking to stop AI calls 2026-10-02",
        actor_user_id=None,
    )
    await session.commit()
    assert await compliance_svc.has_ai_call_consent(session, contact) is False


async def test_ai_consent_endpoints_grant_and_revoke(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    token, org, _number = await make_org_with_number(
        client, "ai-consent-api@example.com", "Org AI Consent API", OUR
    )
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, org_id)
    contact = A

    # evidence is required and must be 1..500 chars.
    r = await client.post(
        "/api/v1/compliance/ai-consent", json={"e164": contact}, headers=headers
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/compliance/ai-consent",
        json={"e164": contact, "evidence": ""},
        headers=headers,
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/compliance/ai-consent",
        json={"e164": contact, "source": "import", "evidence": "csv consent 2026-10-01"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"contact_e164": contact, "ai_call_consent": True}

    set_org_context(session, org_id)
    assert await compliance_svc.has_ai_call_consent(session, contact) is True

    r = await client.delete(f"/api/v1/compliance/ai-consent/{contact}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"contact_e164": contact, "ai_call_consent": False}

    set_org_context(session, org_id)
    assert await compliance_svc.has_ai_call_consent(session, contact) is False


async def test_ai_calls_tick_blocks_unconsented_contact(
    app_with_loopback, session, monkeypatch
):
    """An ai_calls campaign tick never dials a contact without recorded AI consent: the
    row lands ``failed``/``no_ai_consent`` (terminal, never retried), while a consented
    contact is dialed."""
    client, _carrier, _app = app_with_loopback
    _token, org, _number = await make_org_with_number(
        client, "ai-consent-dial@example.com", "Org AI Dial", OUR
    )
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(), org_id=org_id, name="Dialer AI", system_prompt="", is_default=True
    )
    session.add(profile)
    await session.commit()

    consented, not_consented = A, B
    lst = await _ready_list(session, org_id, [consented, not_consented])

    set_org_context(session, org_id)
    await compliance_svc.record_ai_call_consent(
        session,
        org_id,
        consented,
        granted=True,
        source="manual",
        evidence="web form ringlite.io/quote 2026-10-01",
        actor_user_id=None,
    )
    await session.commit()

    campaign = await _dial_campaign(
        session,
        org_id,
        lst.id,
        channel="ai_calls",
        agent_profile_id=profile.id,
        dialer_mode="parallel",
        parallel_lines=2,
    )
    await dialer_svc.start_dial_campaign(session, campaign)

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
        return dialer_svc.DialOutcome(status="connected")

    monkeypatch.setattr(dialer_svc, "_start_call", fake_start_call)

    await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN_NOW)

    assert called == [consented]

    set_org_context(session, org_id)
    rows = {
        row.e164: row
        for row in (
            await session.execute(
                sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id)
            )
        ).scalars().all()
    }
    assert rows[not_consented].status == "failed"
    assert rows[not_consented].disposition == "no_ai_consent"
    assert rows[not_consented].call_id is None
    assert rows[not_consented].next_attempt_at is None
    assert rows[consented].status == "connected"


async def test_voice_campaign_dials_without_ai_consent(
    app_with_loopback, session, monkeypatch
):
    """A plain ``voice`` campaign (a human dialing) still dials a contact with no recorded
    AI consent - the AI gate must not leak into the human path."""
    client, _carrier, _app = app_with_loopback
    _token, org, _number = await make_org_with_number(
        client, "voice-no-ai@example.com", "Org Voice", OUR
    )
    org_id = uuid.UUID(org["id"])

    contact = C
    lst = await _ready_list(session, org_id, [contact])

    campaign = await _dial_campaign(session, org_id, lst.id, channel="voice")
    await dialer_svc.start_dial_campaign(session, campaign)

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
        return dialer_svc.DialOutcome(status="connected")

    monkeypatch.setattr(dialer_svc, "_start_call", fake_start_call)

    await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN_NOW)

    assert called == [contact]

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id)
        )
    ).scalar_one()
    assert row.status == "connected"
