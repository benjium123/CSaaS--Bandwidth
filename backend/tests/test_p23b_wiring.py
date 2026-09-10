from __future__ import annotations

import uuid
from random import Random

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Contact,
    ContactList,
    ContactListRow,
    ContactPhone,
    DialAttempt,
    OrgNumber,
    OutboundCampaign,
)
from app.models.callflow import CallFlow
from app.services import agent as agent_svc
from app.services import dialer as dialer_svc
from app.services import flow_engine
from app.services import flows as flows_svc
from tests.conftest import FROZEN_NOW, auth_headers, make_org_with_number

OUR = "+12145550100"
SECOND_OUR = "+12145550111"
A = "+19725550101"
B = "+19725550102"
C = "+19725550103"


async def _make_org(client, email: str, name: str, e164: str):
    token, org, number = await make_org_with_number(client, email, name, e164)
    return token, uuid.UUID(org["id"]), uuid.UUID(number["id"]), number


async def _make_profile(session, org_id: uuid.UUID, name: str = "Assistant"):
    set_org_context(session, org_id)
    profile = await agent_svc.create_profile(session, org_id, name=name)
    await session.commit()
    return profile


def _assistant_definition(profile_id: uuid.UUID) -> dict:
    return {
        "entry": "assistant",
        "nodes": {
            "assistant": {
                "type": "assistant",
                "profile_id": str(profile_id),
            }
        },
    }


async def _ready_list(session, org_id: uuid.UUID, e164s: list[str]) -> ContactList:
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(),
        org_id=org_id,
        name="L",
        source_filename="l.csv",
        status="ready",
        total_rows=len(e164s),
        accepted_count=len(e164s),
    )
    session.add(lst)
    await session.flush()
    for e164 in e164s:
        contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name=e164)
        session.add(contact)
        await session.flush()
        session.add(
            ContactPhone(
                id=uuid.uuid4(),
                org_id=org_id,
                contact_id=contact.id,
                e164=e164,
                label="mobile",
                is_primary=True,
            )
        )
        await session.flush()
        session.add(
            ContactListRow(
                id=uuid.uuid4(),
                org_id=org_id,
                list_id=lst.id,
                row_number=1,
                raw={"phone": e164},
                e164=e164,
                contact_id=contact.id,
                status="accepted",
                fields={},
            )
        )
    await session.commit()
    return lst


async def _dial_campaign(
    session,
    org_id: uuid.UUID,
    list_id: uuid.UUID,
    *,
    channel: str,
    agent_profile_id: uuid.UUID | None = None,
    from_numbers: list[str] | None = None,
    dialer_mode: str = "power",
    parallel_lines: int = 1,
) -> OutboundCampaign:
    set_org_context(session, org_id)
    campaign = OutboundCampaign(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"{channel}-campaign-{uuid.uuid4().hex[:6]}",
        channel=channel,
        list_id=list_id,
        from_numbers=from_numbers or [OUR],
        dialer_mode=dialer_mode,
        parallel_lines=parallel_lines,
        max_attempts=2,
        retry_backoff_minutes=60,
        local_presence=False,
        agent_profile_id=agent_profile_id,
    )
    session.add(campaign)
    await session.commit()
    return campaign


def _readiness(ready: bool) -> dict:
    return {
        "ready": ready,
        "mode": "platform",
        "missing_kinds": [],
        "missing_platform_keys": [],
        "missing": [],
    }


async def _set_contact_timezone(session, list_id: uuid.UUID, e164: str, tz: str) -> None:
    contact_id = (
        await session.execute(
            sa.select(ContactListRow.contact_id).where(
                ContactListRow.list_id == list_id,
                ContactListRow.e164 == e164,
            )
        )
    ).scalar_one()
    await session.execute(
        sa.update(Contact).where(Contact.id == contact_id).values(timezone=tz)
    )
    await session.commit()


async def test_assistant_node_validates_profile_belongs_to_org(
    app_with_loopback, session
):
    """Creating a flow whose assistant node names another org's profile is a 422, while
    the same definition with this org's own profile saves."""
    client, _carrier, _app = app_with_loopback
    token, org_id, _number_id, _number = await _make_org(
        client, "p23a1@example.com", "Org A", OUR
    )
    _token_b, org_b_id, _number_b_id, _number_b = await _make_org(
        client, "p23b1@example.com", "Org B", "+12145550101"
    )

    own = await _make_profile(session, org_id, "Own")
    foreign = await _make_profile(session, org_b_id, "Foreign")

    bad_definition = _assistant_definition(foreign.id)
    r = await client.post(
        "/api/v1/flows",
        json={"name": "Assistant Flow", "definition": bad_definition},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 422
    assert "assistant" in r.text

    good_definition = _assistant_definition(own.id)
    r = await client.post(
        "/api/v1/flows",
        json={"name": "Assistant Flow", "definition": good_definition},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text


def test_assistant_node_is_terminal_in_the_engine():
    """The engine returns terminal='assistant' and one HandToAssistant action."""
    profile_id = str(uuid.uuid4())
    definition = _assistant_definition(uuid.UUID(profile_id))
    result = flow_engine.start(definition)

    assert result.awaiting is None
    assert result.terminal == "assistant"
    assert result.actions == (flow_engine.HandToAssistant(profile_id=profile_id),)


async def test_activating_a_flow_with_an_unready_assistant_is_422(
    app_with_loopback, session, monkeypatch
):
    """Activation is gated on go_live_readiness, then succeeds once ready."""
    client, _carrier, _app = app_with_loopback
    token, org_id, _number_id, _number = await _make_org(
        client, "p23a3@example.com", "Org A", OUR
    )
    profile = await _make_profile(session, org_id, "Not Ready Assistant")

    definition = _assistant_definition(profile.id)
    r = await client.post(
        "/api/v1/flows",
        json={"name": "Gate Flow", "definition": definition},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    flow_id = r.json()["id"]

    ready_state = {"ready": False}

    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(ready_state["ready"])

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)

    r = await client.post(
        f"/api/v1/flows/{flow_id}/activate",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 422
    assert "not ready to answer calls" in r.text

    ready_state["ready"] = True
    r = await client.post(
        f"/api/v1/flows/{flow_id}/activate",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text


async def test_answered_by_assistant_binds_a_one_node_flow(
    app_with_loopback, session, monkeypatch
):
    """PATCH answered-by creates/activates a one-node assistant flow and derives the
    answered_by fields on the number."""
    client, _carrier, _app = app_with_loopback
    token, org_id, number_id, number = await _make_org(
        client, "p23a4@example.com", "Org A", OUR
    )
    profile = await _make_profile(session, org_id, "Assistant")

    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(True)

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)

    r = await client.patch(
        f"/api/v1/numbers/{number_id}/answered-by",
        json={"mode": "assistant", "profile_id": str(profile.id)},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    # Fable's wire shape: one nested object carrying the mode, the id AND the name, so a
    # console can render "Answered by: Assistant" without a second request per number.
    assert r.json()["answered_by"] == {
        "mode": "assistant",
        "profile_id": str(profile.id),
        "profile_name": "Assistant",
    }

    r = await client.get("/api/v1/numbers", headers=auth_headers(token, org_id))
    assert r.status_code == 200
    row = next(n for n in r.json() if n["id"] == str(number_id))
    assert row["answered_by"] == {
        "mode": "assistant",
        "profile_id": str(profile.id),
        "profile_name": "Assistant",
    }

    set_org_context(session, org_id)
    n = await session.get(OrgNumber, number_id)
    flow = await session.get(CallFlow, n.call_flow_id)
    assert flow.status == "active"
    assert flows_svc.assistant_profile_ids(flow.definition) == [str(profile.id)]
    assert flow.definition == {
        "entry": "assistant",
        "nodes": {
            "assistant": {
                "type": "assistant",
                "profile_id": str(profile.id),
            }
        },
    }


async def test_answered_by_human_restores_the_default_flow(
    app_with_loopback, session, monkeypatch
):
    """Switching back to human rebinds the number to the org's active Default flow."""
    client, _carrier, _app = app_with_loopback
    token, org_id, number_id, number = await _make_org(
        client, "p23a5@example.com", "Org A", OUR
    )
    profile = await _make_profile(session, org_id, "Assistant")

    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(True)

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)

    await client.patch(
        f"/api/v1/numbers/{number_id}/answered-by",
        json={"mode": "assistant", "profile_id": str(profile.id)},
        headers=auth_headers(token, org_id),
    )

    r = await client.patch(
        f"/api/v1/numbers/{number_id}/answered-by",
        json={"mode": "human", "profile_id": None},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["answered_by"] == {
        "mode": "human",
        "profile_id": None,
        "profile_name": None,
    }

    set_org_context(session, org_id)
    n = await session.get(OrgNumber, number_id)
    default_flow_id = (
        await session.execute(
            sa.select(CallFlow.id).where(
                CallFlow.org_id == org_id,
                CallFlow.name == "Default",
                CallFlow.status == "active",
            )
        )
    ).scalar_one_or_none()
    assert n.call_flow_id == default_flow_id


async def test_answered_by_is_tenant_scoped(app_with_loopback, session, monkeypatch):
    """Another org's number id and another org's profile id are both 404."""
    client, _carrier, _app = app_with_loopback
    token, org_id, number_id, _number = await _make_org(
        client, "p23a6@example.com", "Org A", OUR
    )
    _token_b, org_b_id, number_b_id, _number_b = await _make_org(
        client, "p23b6@example.com", "Org B", "+12145550101"
    )
    own = await _make_profile(session, org_id, "Own")
    foreign = await _make_profile(session, org_b_id, "Foreign")

    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(True)

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)

    r = await client.patch(
        f"/api/v1/numbers/{number_b_id}/answered-by",
        json={"mode": "assistant", "profile_id": str(own.id)},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 404

    r = await client.patch(
        f"/api/v1/numbers/{number_id}/answered-by",
        json={"mode": "assistant", "profile_id": str(foreign.id)},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 404


async def test_ai_campaign_requires_a_ready_assistant(
    app_with_loopback, session, monkeypatch
):
    """Creating an ai_calls campaign requires an assistant and readiness."""
    client, _carrier, _app = app_with_loopback
    token, org_id, _number_id, _number = await _make_org(
        client, "p23a7@example.com", "Org A", OUR
    )
    _token_b, org_b_id, _number_b_id, _number_b = await _make_org(
        client, "p23b7@example.com", "Org B", "+12145550101"
    )
    lst = await _ready_list(session, org_id, [A])
    own = await _make_profile(session, org_id, "Own")
    foreign = await _make_profile(session, org_b_id, "Foreign")

    def payload(**overrides):
        data = {
            "name": "AI Calls",
            "channel": "ai_calls",
            "list_id": str(lst.id),
            "dialer_mode": "power",
            "agent_profile_id": None,
        }
        data.update(overrides)
        return data

    r = await client.post(
        "/api/v1/outbound/campaigns",
        json=payload(),
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/outbound/campaigns",
        json=payload(agent_profile_id=str(foreign.id)),
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 404

    ready_state = {"ready": False}

    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(ready_state["ready"])

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)

    r = await client.post(
        "/api/v1/outbound/campaigns",
        json=payload(agent_profile_id=str(own.id)),
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 422
    assert "not ready to answer calls" in r.text

    ready_state["ready"] = True
    r = await client.post(
        "/api/v1/outbound/campaigns",
        json=payload(agent_profile_id=str(own.id)),
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text


async def test_ai_campaign_dials_through_the_assistant_path(
    app_with_loopback, session, monkeypatch
):
    """The dialer passes agent_profile_id only for ai_calls campaigns, not voice."""
    client, _carrier, _app = app_with_loopback
    _token, org_id, _number_id, _number = await _make_org(
        client, "p23a8@example.com", "Org A", OUR
    )
    profile = await _make_profile(session, org_id, "Assistant")

    ai_contact = A
    voice_contact = B

    ai_list = await _ready_list(session, org_id, [ai_contact])
    voice_list = await _ready_list(session, org_id, [voice_contact])

    # The two campaigns must dial from DIFFERENT numbers. dialer_tick's pacing state is
    # keyed on (org, from_number) and the first campaign's outcome loop advances it, so a
    # second campaign sharing the number is correctly held back until the next tick. That
    # is real behaviour this test has no business papering over.
    r = await client.post(
        "/api/v1/numbers",
        json={"e164": SECOND_OUR},
        headers=auth_headers(_token, str(org_id)),
    )
    assert r.status_code == 201, r.text

    ai_campaign = await _dial_campaign(
        session,
        org_id,
        ai_list.id,
        channel="ai_calls",
        agent_profile_id=profile.id,
        from_numbers=[OUR],
    )
    voice_campaign = await _dial_campaign(
        session,
        org_id,
        voice_list.id,
        channel="voice",
        from_numbers=[SECOND_OUR],
    )

    await dialer_svc.start_dial_campaign(session, ai_campaign)
    await dialer_svc.start_dial_campaign(session, voice_campaign)

    calls: list[dict] = []

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
        calls.append(
            {
                "to": to_e164,
                "agent_profile_id": kwargs.get("agent_profile_id"),
            }
        )
        return dialer_svc.DialOutcome(status="connected")

    monkeypatch.setattr(dialer_svc, "_start_call", fake_start_call)

    counts = await dialer_svc.dialer_tick(
        session, None, None, None, Random(1), now=FROZEN_NOW
    )
    assert counts["connected"] == 2

    ai_call = next(c for c in calls if c["to"] == ai_contact)
    voice_call = next(c for c in calls if c["to"] == voice_contact)
    assert ai_call["agent_profile_id"] == profile.id
    assert voice_call["agent_profile_id"] is None


async def test_ai_campaign_respects_quiet_hours_consent_and_dnc(
    app_with_loopback, session, monkeypatch
):
    """An ai_calls campaign uses the same compliance precheck as voice."""
    client, _carrier, app = app_with_loopback
    _token, org_id, _number_id, _number = await _make_org(
        client, "p23a9@example.com", "Org A", OUR
    )
    profile = await _make_profile(session, org_id, "Assistant")

    dnc_contact = A
    quiet_contact = B
    allowed_contact = C

    lst = await _ready_list(session, org_id, [dnc_contact, quiet_contact, allowed_contact])
    await _set_contact_timezone(session, lst.id, quiet_contact, "Europe/Moscow")

    from app.compliance import service as compliance_svc

    set_org_context(session, org_id)
    await compliance_svc.add_dnc(session, org_id, dnc_contact)
    await session.commit()

    # parallel/3 so all three rows are claimed in ONE tick - power mode claims one row per
    # tick, and the point of this test is what the compliance gate did to all three.
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

    await dialer_svc.dialer_tick(
        session, None, None, None, Random(1), now=FROZEN_NOW
    )

    assert called == [allowed_contact]

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
    assert rows[dnc_contact].next_attempt_at is None

    assert rows[quiet_contact].status == "queued"
    assert rows[quiet_contact].next_attempt_at is not None

    assert rows[allowed_contact].status == "connected"


async def test_numbers_list_does_not_go_n_plus_1_on_assistants(
    app_with_loopback, session, monkeypatch, query_counter
):
    """Two numbers answered by two different assistants cost the SAME number of queries as
    one - the flow rows and the assistant names are both fetched in one query each."""
    client, _carrier, _app = app_with_loopback
    token, org_id, number_id, _number = await _make_org(
        client, "p23a-n1@example.com", "Org N", OUR
    )
    r = await client.post(
        "/api/v1/numbers", json={"e164": SECOND_OUR}, headers=auth_headers(token, org_id)
    )
    assert r.status_code == 201, r.text
    second_id = r.json()["id"]

    first = await _make_profile(session, org_id, "First Assistant")
    second = await _make_profile(session, org_id, "Second Assistant")

    async def fake_go_live(session, settings, *, org, profile):
        return _readiness(True)

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_go_live)

    for target, profile in ((number_id, first), (second_id, second)):
        r = await client.patch(
            f"/api/v1/numbers/{target}/answered-by",
            json={"mode": "assistant", "profile_id": str(profile.id)},
            headers=auth_headers(token, org_id),
        )
        assert r.status_code == 200, r.text

    query_counter.reset()
    r = await client.get("/api/v1/numbers", headers=auth_headers(token, org_id))
    assert r.status_code == 200, r.text
    two_number_queries = query_counter.count

    by_id = {n["id"]: n for n in r.json()}
    assert by_id[str(number_id)]["answered_by"]["profile_name"] == "First Assistant"
    assert by_id[str(second_id)]["answered_by"]["profile_name"] == "Second Assistant"

    # Delete the second number and count again: an N+1 would drop by at least two here.
    r = await client.delete(
        f"/api/v1/numbers/{second_id}", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    query_counter.reset()
    r = await client.get("/api/v1/numbers", headers=auth_headers(token, org_id))
    assert r.status_code == 200
    one_number_queries = query_counter.count

    assert two_number_queries - one_number_queries <= 1
