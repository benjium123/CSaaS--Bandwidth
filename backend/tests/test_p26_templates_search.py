from __future__ import annotations

import uuid

from app.db.base import set_org_context
from app.models import MessageTemplate
from tests.conftest import auth_headers, create_org, register_and_login


async def _make_template(session, org_id: uuid.UUID, name: str, body: str) -> MessageTemplate:
    set_org_context(session, org_id)
    row = MessageTemplate(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name,
        body=body,
        media_asset_ids=[],
    )
    session.add(row)
    await session.commit()
    return row


async def test_templates_q_matches_name_and_body(client, session):
    token = await register_and_login(client, "templates-search-1@example.com")
    org = await create_org(client, token, "Templates Search One")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, str(org_id))

    await _make_template(
        session, org_id, "Welcome", "Hello {{contact.first_name}}, welcome!"
    )
    await _make_template(
        session, org_id, "Pricing", "Here is the pricing information."
    )
    await _make_template(session, org_id, "Farewell", "Goodbye and see you soon.")

    # A new org is seeded with two default templates ("Help reply", "Opt-out
    # confirmation"), so every assertion here names the rows this test created rather
    # than asserting on the whole list.
    r = await client.get("/api/v1/templates", params={"q": "pricing"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()] == ["Pricing"]

    r = await client.get("/api/v1/templates", params={"q": "WELCOME"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()] == ["Welcome"]

    r = await client.get("/api/v1/templates", params={"q": "goodbye"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()] == ["Farewell"]


async def test_templates_q_wildcards_are_literal(client, session):
    token = await register_and_login(client, "templates-search-2@example.com")
    org = await create_org(client, token, "Templates Search Two")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, str(org_id))

    await _make_template(session, org_id, "50% off", "Half price sale")
    await _make_template(session, org_id, "Welcome", "Hello there")

    r = await client.get("/api/v1/templates", params={"q": "50% off"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()] == ["50% off"]

    r = await client.get("/api/v1/templates", params={"q": "%"}, headers=headers)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()] == ["50% off"]


async def test_templates_without_q_are_unchanged(client, session):
    token = await register_and_login(client, "templates-search-3@example.com")
    org = await create_org(client, token, "Templates Search Three")
    org_id = uuid.UUID(org["id"])
    headers = auth_headers(token, str(org_id))

    await _make_template(session, org_id, "Zeta", "Z body")
    await _make_template(session, org_id, "Alpha", "A body")
    await _make_template(session, org_id, "Mia", "M body")

    r = await client.get("/api/v1/templates", headers=headers)
    assert r.status_code == 200, r.text
    names = [item["name"] for item in r.json()]
    # The two seeded defaults are still there; ordering is still by name.
    assert names == sorted(names)
    assert {"Alpha", "Mia", "Zeta"}.issubset(set(names))
