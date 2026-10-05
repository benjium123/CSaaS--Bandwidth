"""P46 slice B: action routes refuse with 403 feature_disabled when an operator switches the
feature off (auth/deps.requires_feature), and 911/933 is never gated.

The route test discovers every ``requires_feature`` dependency from the app itself, so a new
gated route is covered without editing this file."""

from __future__ import annotations

import inspect
import re
import uuid

from app.main import create_app
from app.services import entitlements
from app.voice_plane import service as voice_service
from tests.conftest import (
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)
from tests.test_e911 import _room_org, app_with_dial_log  # noqa: F401  (fixture)


def _gated_routes() -> list[tuple[str, str, str]]:
    """(method, path, feature key) for every route carrying a requires_feature dependency."""
    out = []

    def walk(routes, prefix):
        for route in routes:
            inner = getattr(route, "original_router", None)  # FastAPI's lazily included router
            if inner is not None:
                walk(inner.routes, prefix + route.include_context.prefix)
                continue
            for dep in getattr(route, "dependencies", None) or []:
                fn = dep.dependency
                if fn is None or not fn.__qualname__.startswith("requires_feature."):
                    continue
                key = inspect.getclosurevars(fn).nonlocals["key"]
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    out.append((method, prefix + route.path, key))

    walk(create_app(make_settings()).routes, "")
    return out


GATED = _gated_routes()


def test_the_action_routes_are_gated():
    keys = {k for _, _, k in GATED}
    assert len(GATED) >= 46  # POST /calls gates inline, after the 911 branch
    assert {
        "ivr_flows",
        "ring_groups",
        "call_queues",
        "supervisor",
        "power_dialer",
        "fax",
        "ai_agent",
        "ai_kb",
        "porting",
        "enterprise_sso",
        "analytics",
        "contact_export",
        "api_access",
        "tendlc",
        "numbers",
        "voice",
        "sms",
    } <= keys
    assert keys <= set(entitlements.CATALOG)


async def _org_with_everything_off(session, client, email):
    token = await register_and_login(client, email)
    org = await create_org(client, token, "Gated Org")
    org_id = uuid.UUID(org["id"])
    for key in entitlements.CATALOG:
        await entitlements.set_feature(
            session, org_id, key, enabled=False, price_override_micros=None, actor_user_id=None
        )
    await session.commit()
    return token, org_id


async def test_every_gated_route_refuses_when_its_feature_is_off(client, session):
    token, org_id = await _org_with_everything_off(session, client, "gates-off@example.com")
    h = auth_headers(token, org_id)
    wrong = []
    for method, path, key in GATED:
        url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        r = await client.request(method, url, json={}, headers=h)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        code = (body.get("error") or {}).get("code")
        if r.status_code != 403 or code != "feature_disabled":
            wrong.append((method, path, key, r.status_code, code))
    assert wrong == []


async def test_a_gated_route_opens_again_when_the_feature_is_back_on(client, session):
    token, org_id = await _org_with_everything_off(session, client, "gates-on@example.com")
    await entitlements.set_feature(
        session, org_id, "analytics", enabled=True, price_override_micros=None, actor_user_id=None
    )
    await session.commit()
    r = await client.get("/api/v1/analytics/overview", headers=auth_headers(token, org_id))
    assert r.status_code == 200, r.text


async def test_911_connects_with_voice_switched_off(app_with_dial_log, session):  # noqa: F811
    client, dials = app_with_dial_log
    token, org, _ = await _room_org(session, client, "e911-novoice@example.com", "No Voice Org")
    org_id = uuid.UUID(org["id"])
    await entitlements.set_feature(
        session, org_id, "voice", enabled=False, price_override_micros=None, actor_user_id=None
    )
    await session.commit()
    h = auth_headers(token, org["id"])

    ordinary = await client.post(
        "/api/v1/calls", json={"to": "+19725550199", "via": "room"}, headers=h
    )
    assert ordinary.status_code == 403
    assert ordinary.json()["error"]["code"] == "feature_disabled"

    r = await client.post("/api/v1/calls", json={"to": "911", "via": "room"}, headers=h)
    assert r.status_code == 201, r.text
    await voice_service.wait_for_pending_dial_tasks()
    assert [d.get("sip_call_to") for d in dials] == ["911"]


async def test_answering_machine_detection_needs_amd(client, session):
    token = await register_and_login(client, "amd-off@example.com")
    org = await create_org(client, token, "AMD Org")
    org_id = uuid.UUID(org["id"])
    await entitlements.set_feature(
        session, org_id, "amd", enabled=False, price_override_micros=None, actor_user_id=None
    )
    await session.commit()
    r = await client.post(
        "/api/v1/calls",
        json={"to": "+19725550199", "via": "room", "machine_detection": "async"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "feature_disabled"


async def test_picture_messages_need_mms(app_with_carrier, session):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(
        client, "mms-off@example.com", "MMS Org", "+12145550177"
    )
    org_id = uuid.UUID(org["id"])
    await entitlements.set_feature(
        session, org_id, "mms", enabled=False, price_override_micros=None, actor_user_id=None
    )
    await session.commit()
    h = auth_headers(token, org_id)
    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725550199", "body": "hi", "media_ids": [str(uuid.uuid4())]},
        headers=h,
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "feature_disabled"
    assert fake.sent == []

    plain = await client.post(
        "/api/v1/messages", json={"to": "+19725550199", "body": "hi"}, headers=h
    )
    assert plain.status_code == 201, plain.text
