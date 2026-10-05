"""AI agents v2: template catalog, deterministic render, from-template, curated voices."""

from __future__ import annotations

import copy
import uuid

from app.services import agent_templates as tpl
from app.services import entitlements
from tests.conftest import auth_headers, create_org, register_and_login

IDS = {
    "after_hours",
    "support_triage",
    "missed_call_textback",
    "re_seller_qualifier",
    "appointment_booker",
    "re_buyer_qualifier",
    "home_services_dispatch",
    "restaurant_reservations",
    "medical_front_desk",
}


def test_catalog_has_nine_ids_and_no_receptionist():
    ids = {t["id"] for t in tpl.TEMPLATES}
    assert ids == IDS and len(tpl.TEMPLATES) == 9
    assert not any(
        "receptionist" in t["id"] or "receptionist" in t["name"].lower() for t in tpl.TEMPLATES
    )
    by = {t["id"]: t for t in tpl.TEMPLATES}
    assert by["medical_front_desk"]["available"] is False
    assert by["medical_front_desk"]["unavailable_reason"] == "Needs a signed BAA"
    assert by["missed_call_textback"]["channel"] == "sms"
    assert by["appointment_booker"]["interview"]["booking"]["enabled"] is True


def test_render_drops_empty_lines_and_never_emits_braces():
    iv = copy.deepcopy(tpl.get_template("after_hours")["interview"])
    iv["business"]["name"] = "Acme {{evil}}"
    out = tpl.render("after_hours", iv)
    assert "{{" not in out["prompt"] and "{{" not in out["greeting"]
    assert "Hours:" not in out["prompt"] and "Address" not in out["prompt"]
    assert "Questions you can answer" not in out["prompt"]
    assert "Booking" not in out["prompt"]
    assert "transfer to is" not in out["prompt"]
    assert out["greeting"] == "Hi, thanks for calling Acme evil. How can I help?"
    assert all("{{" not in s for s in out["locked"])
    assert any("Voice style rules" in s for s in out["locked"])


def test_missing_and_greeting_defaults():
    out = tpl.render(None, {})
    assert out["missing"] == [
        "business.name",
        "agent.name",
        "goal",
        "handoff.transfer_number",
    ]
    assert out["greeting"] == "Hi, thanks for calling. How can I help?"
    iv = copy.deepcopy(tpl.get_template("appointment_booker")["interview"])
    iv["business"]["name"] = "Acme"
    iv["agent"]["name"] = "Sam"
    iv["handoff"]["transfer_number"] = "+15551234567"
    out = tpl.render("appointment_booker", iv)
    assert out["missing"] == []
    assert out["greeting"] == "Hi, thanks for calling Acme, this is Sam. How can I help?"
    iv["agent"]["greeting"] = "Custom hello"
    assert tpl.render("appointment_booker", iv)["greeting"] == "Custom hello"


def test_booking_text_says_pending_confirmation():
    iv = copy.deepcopy(tpl.get_template("appointment_booker")["interview"])
    prompt = tpl.render("appointment_booker", iv)["prompt"]
    assert "requested, pending confirmation" in prompt
    assert "Never say it is booked or confirmed" in prompt
    iv["booking"]["enabled"] = False
    assert "pending confirmation" not in tpl.render("appointment_booker", iv)["prompt"]


def _full_interview():
    iv = copy.deepcopy(tpl.get_template("appointment_booker")["interview"])
    iv["business"]["name"] = "Acme"
    iv["agent"].update(name="Sam", voice_id="voice-x", language="en")
    iv["faqs"] = [{"q": "Where?", "a": "Main St."}, {"q": "", "a": "orphan"}]
    iv["handoff"]["transfer_number"] = "+15551234567"
    return iv


async def test_catalog_render_and_voices_routes(client):
    token = await register_and_login(client, "tpl-routes@example.com")
    org = await create_org(client, token, "Tpl Routes")
    h = auth_headers(token, org["id"])

    r = await client.get("/api/v1/agent/templates", headers=h)
    assert r.status_code == 200, r.text
    assert {t["id"] for t in r.json()} == IDS

    r = await client.post(
        "/api/v1/agent/templates/render",
        json={"template_id": "appointment_booker", "interview": _full_interview()},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Q: Where?\nA: Main St." in body["prompt"] and "orphan" not in body["prompt"]
    assert body["missing"] == []

    r = await client.get("/api/v1/agent/voices", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 4
    assert {v["provider"] for v in r.json()} == {"elevenlabs"}


async def test_from_template_creates_profile_with_extra(client):
    token = await register_and_login(client, "tpl-create@example.com")
    org = await create_org(client, token, "Tpl Create")
    h = auth_headers(token, org["id"])
    iv = _full_interview()
    payload = {"template_id": "appointment_booker", "name": "Front desk", "interview": iv}

    r = await client.post("/api/v1/agent/profiles/from-template", json=payload, headers=h)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["voice_id"] == "voice-x" and p["language"] == "en"
    assert p["greeting"] == "Hi, thanks for calling Acme, this is Sam. How can I help?"
    assert "requested, pending confirmation" in p["system_prompt"]
    assert p["extra"]["template_id"] == "appointment_booker"
    assert p["extra"]["template_version"] == 1
    assert p["extra"]["prompt_mode"] == "interview"
    assert p["extra"]["interview"]["business"]["name"] == "Acme"

    dup = await client.post("/api/v1/agent/profiles/from-template", json=payload, headers=h)
    assert dup.status_code == 409, dup.text

    bad = await client.post(
        "/api/v1/agent/profiles/from-template",
        json={"template_id": "nope", "name": "X", "interview": iv},
        headers=h,
    )
    assert bad.status_code == 422, bad.text


async def test_from_template_refused_when_ai_agent_off(client, session):
    token = await register_and_login(client, "tpl-gate@example.com")
    org = await create_org(client, token, "Tpl Gate")
    await entitlements.set_feature(
        session,
        uuid.UUID(org["id"]),
        "ai_agent",
        enabled=False,
        price_override_micros=None,
        actor_user_id=None,
    )
    await session.commit()
    r = await client.post(
        "/api/v1/agent/profiles/from-template",
        json={"template_id": "after_hours", "name": "X", "interview": {}},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "feature_disabled"
