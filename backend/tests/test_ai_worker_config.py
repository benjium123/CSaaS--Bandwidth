"""AI agents v2, work package W: worker config fields, per-number disclosure switch and the
/tools registry."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import AgentProfile, AuditLogEntry, Call, OrgNumber
from app.services import agent as agent_svc
from app.services import entitlements
from tests.conftest import auth_headers, make_settings, register_and_login
from tests.test_agent_tools import _room_call
from tests.test_ops_console import _new_org, _operator, _why, ops, ops_settings  # noqa: F401
from tests.test_p23a_builder import (
    _insert_call,
    _make_app,
    create_org,
    worker_headers,
    worker_token,
)

OUR = "+12145550100"


async def _number(session, org_id, e164=OUR, provisioning=None) -> OrgNumber:
    set_org_context(session, org_id)
    number = OrgNumber(
        id=uuid.uuid4(), org_id=org_id, e164=e164, provisioning=provisioning or {}
    )
    session.add(number)
    await session.commit()
    return number


async def _call(session, org_id) -> Call:
    call_id = await _insert_call(session, org_id)
    set_org_context(session, org_id)
    return await session.get(Call, call_id)


async def _config(session, org_id, call, **profile_fields) -> dict:
    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(), org_id=org_id, name="p", system_prompt="Be nice.", **profile_fields
    )
    return await agent_svc.resolve_worker_config(
        session, make_settings(), call=call, profile=profile, include_keys=False
    )


async def test_config_returns_every_contract_field(session):
    org_id = await _new_org(session, "Config Co")
    await _number(session, org_id)
    call = await _call(session, org_id)
    cfg = await _config(
        session,
        org_id,
        call,
        greeting="Hi there",
        max_call_seconds=300,
        silence_timeout_seconds=7,
        interrupt_sensitivity="high",
        language="es",
    )
    for key in (
        "org_name", "contact_e164", "direction", "system_prompt", "extra_rules", "greeting",
        "voice_id", "max_call_seconds", "silence_timeout_seconds", "interrupt_sensitivity",
        "language", "ai_disclosure", "disclosure_text", "llm_provider", "llm_model",
        "llm_base_url", "tools", "post_call_fields",
    ):
        assert key in cfg, key
    assert cfg["org_name"] == "Config Co"
    assert cfg["max_call_seconds"] == 300
    assert cfg["silence_timeout_seconds"] == 7
    assert cfg["interrupt_sensitivity"] == "high"
    assert cfg["language"] == "es"
    assert cfg["ai_disclosure"] is True
    assert cfg["disclosure_text"] == (
        "This call is answered by an automated assistant for Config Co."
    )


async def test_disclosure_off_by_org_feature(session):
    org_id = await _new_org(session, "Feature Off")
    await _number(session, org_id)
    call = await _call(session, org_id)
    await entitlements.set_feature(
        session, org_id, "ai_disclosure", enabled=False,
        price_override_micros=None, actor_user_id=None,
    )
    await session.commit()
    assert (await _config(session, org_id, call))["ai_disclosure"] is False


async def test_disclosure_off_by_number_only(session):
    org_id = await _new_org(session, "Number Off")
    await _number(session, org_id, provisioning={"ai_disclosure": False})
    call = await _call(session, org_id)
    assert (await _config(session, org_id, call))["ai_disclosure"] is False


async def test_ops_switches_disclosure_per_number_and_audits(ops, session, ops_settings):  # noqa: F811
    org_id = await _new_org(session, "Switch Co")
    number_id = (await _number(session, org_id)).id
    admin = await _operator(ops, session)
    reviewer = await _operator(ops, session, email="revw@example.com", role="reviewer")
    path = f"/api/v1/ops/orgs/{org_id}/numbers/{number_id}/ai-disclosure"

    r = await ops.patch(path, json={"enabled": False}, headers=auth_headers(reviewer))
    assert r.status_code == 403, r.text
    customer = await register_and_login(ops, "cust-disclosure@example.com")
    r = await ops.patch(path, json={"enabled": False}, headers=auth_headers(customer))
    assert r.status_code in (401, 403), r.text

    r = await ops.patch(path, json={"enabled": False}, headers=_why(admin))
    assert r.status_code == 200, r.text
    session.expire_all()
    set_org_context(session, org_id)
    row = await session.get(OrgNumber, number_id)
    assert row.provisioning["ai_disclosure"] is False
    call = await _call(session, org_id)
    assert (await _config(session, org_id, call))["ai_disclosure"] is False

    r = await ops.patch(path, json={"enabled": True}, headers=_why(admin))
    assert r.status_code == 200, r.text
    session.expire_all()
    set_org_context(session, org_id)
    assert "ai_disclosure" not in (await session.get(OrgNumber, number_id)).provisioning
    audits = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "number_ai_disclosure.updated")
        )
    ).scalars().all()
    assert len(audits) == 2
    assert audits[0].target_id == str(number_id)

    r = await ops.patch(
        f"/api/v1/ops/orgs/{org_id}/numbers/{uuid.uuid4()}/ai-disclosure",
        json={"enabled": True}, headers=_why(admin),
    )
    assert r.status_code == 404


async def test_tools_registry_routes_book_and_transfer(engine, session):
    async with _make_app(engine) as (client, _):
        token = await register_and_login(client, "ai-w-tools@example.com")
        org = await create_org(client, token, "Tools Co")
        org_id = uuid.UUID(org["id"])
        call_id = await _insert_call(session, org_id)
        wh = worker_headers(worker_token())

        booked = await client.post(
            "/api/v1/agent/tools/book_appointment",
            json={"call_id": str(call_id), "arguments": {"raw_when": "tomorrow at 3pm"}},
            headers=wh,
        )
        assert booked.status_code == 200, booked.text
        assert booked.json()["ok"] is True
        assert booked.json()["result"]["raw_when"] == "tomorrow at 3pm"

        missing_when = await client.post(
            "/api/v1/agent/tools/book_appointment",
            json={"call_id": str(call_id), "arguments": {}},
            headers=wh,
        )
        assert missing_when.status_code == 422, missing_when.text

        room_call = await _room_call(session, org_id)
        transfer = await client.post(
            "/api/v1/agent/tools/transfer",
            json={"call_id": str(room_call.id), "arguments": {"reason": "wants a human"}},
            headers=wh,
        )
        assert transfer.status_code == 200, transfer.text
        assert transfer.json()["result"]["published"] is True

        sms = await client.post(
            "/api/v1/agent/tools/send_followup_sms",
            json={"call_id": str(call_id), "arguments": {}},
            headers=wh,
        )
        assert sms.status_code == 501
