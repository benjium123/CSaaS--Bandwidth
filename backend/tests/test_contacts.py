from __future__ import annotations

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import Contact, MessageThread
from tests.conftest import (
    auth_headers,
    create_contact,
    fixture_bytes,
    make_org_with_number,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
THEIRS = "+19725550199"


async def _unscoped(session, model):
    return list(
        (
            await session.execute(
                sa.select(model).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )


async def test_crud_and_phone_uniqueness(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c1@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    contact = await create_contact(client, token, org["id"], "Ada Lovelace", [THEIRS])
    assert contact["display_name"] == "Ada Lovelace"
    assert [p["e164"] for p in contact["phones"]] == [THEIRS]

    # A second contact cannot claim the same number - the unique constraint is the
    # structural guard against duplicates.
    dupe = await client.post(
        "/api/v1/contacts",
        json={"display_name": "Impostor", "phones": [{"e164": THEIRS}]},
        headers=h,
    )
    assert dupe.status_code == 409

    got = await client.get(f"/api/v1/contacts/{contact['id']}", headers=h)
    assert got.status_code == 200

    patched = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"display_name": "Ada King"},
        headers=h,
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Ada King"

    found = await client.get("/api/v1/contacts?q=ada", headers=h)
    assert len(found.json()) == 1
    by_phone = await client.get("/api/v1/contacts?q=9725550199", headers=h)
    assert len(by_phone.json()) == 1


async def test_inbound_autocreates_and_links_contact(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c2@example.com", "Org A", OUR)

    r = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert r.status_code == 200

    contacts = await _unscoped(session, Contact)
    assert len(contacts) == 1
    assert contacts[0].display_name == THEIRS

    threads = await _unscoped(session, MessageThread)
    assert threads[0].contact_id == contacts[0].id

    # A replayed webhook must NOT create a second contact - the dedupe transaction
    # covers the side effects, not just the message row.
    again = await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    assert again.status_code == 200
    assert len(await _unscoped(session, Contact)) == 1


async def test_late_contact_adoption(app_with_carrier, session):
    """The common real-world order: messages first, contact created afterwards."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c3@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    # Auto-created placeholder contact from the inbound.
    await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    placeholder = (await _unscoped(session, Contact))[0]

    # A human now creates a *named* contact and claims that number. The phone moves,
    # and the thread re-links to the real contact.
    placeholder_id = placeholder["id"] if isinstance(placeholder, dict) else placeholder.id
    moved = await client.patch(
        f"/api/v1/contacts/{placeholder_id}",
        json={"display_name": "Grace Hopper", "phones": [{"e164": THEIRS}]},
        headers=h,
    )
    assert moved.status_code == 200
    assert moved.json()["display_name"] == "Grace Hopper"

    threads = await _unscoped(session, MessageThread)
    assert threads[0].contact_id is not None


async def test_custom_field_validation(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c4@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await client.post(
        "/api/v1/custom-fields",
        json={"key": "deal_size", "label": "Deal size", "kind": "number"},
        headers=h,
    )
    await client.post(
        "/api/v1/custom-fields",
        json={"key": "stage", "label": "Stage", "kind": "select", "options": ["new", "won"]},
        headers=h,
    )

    bad_type = await client.post(
        "/api/v1/contacts",
        json={"display_name": "X", "attributes": {"deal_size": "abc"}},
        headers=h,
    )
    assert bad_type.status_code == 422

    unknown = await client.post(
        "/api/v1/contacts",
        json={"display_name": "X", "attributes": {"nope": "1"}},
        headers=h,
    )
    assert unknown.status_code == 422

    bad_option = await client.post(
        "/api/v1/contacts",
        json={"display_name": "X", "attributes": {"stage": "lost"}},
        headers=h,
    )
    assert bad_option.status_code == 422

    ok = await client.post(
        "/api/v1/contacts",
        json={"display_name": "X", "attributes": {"deal_size": 42, "stage": "won"}},
        headers=h,
    )
    assert ok.status_code == 201
    assert ok.json()["attributes"] == {"deal_size": 42, "stage": "won"}


async def test_select_field_requires_options(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c5@example.com", "Org A", OUR)
    r = await client.post(
        "/api/v1/custom-fields",
        json={"key": "stage", "label": "Stage", "kind": "select", "options": []},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422


async def test_bad_custom_field_key_rejected(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c6@example.com", "Org A", OUR)
    r = await client.post(
        "/api/v1/custom-fields",
        json={"key": "Deal Size", "label": "x", "kind": "text"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422


async def test_tenancy(app_with_carrier):
    client, _, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "t1@example.com", "Org A", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "t2@example.com", "Org B", "+12145550111"
    )

    contact = await create_contact(client, token_a, org_a["id"], "Secret Person", [THEIRS])
    await client.post(
        "/api/v1/tags", json={"name": "vip"}, headers=auth_headers(token_a, org_a["id"])
    )
    await client.post(
        "/api/v1/companies", json={"name": "ACME"}, headers=auth_headers(token_a, org_a["id"])
    )
    await client.post(
        "/api/v1/custom-fields",
        json={"key": "k", "label": "K", "kind": "text"},
        headers=auth_headers(token_a, org_a["id"]),
    )

    hb = auth_headers(token_b, org_b["id"])
    assert (await client.get("/api/v1/contacts", headers=hb)).json() == []
    assert (await client.get("/api/v1/tags", headers=hb)).json() == []
    assert (await client.get("/api/v1/companies", headers=hb)).json() == []
    assert (await client.get("/api/v1/custom-fields", headers=hb)).json() == []
    assert (await client.get(f"/api/v1/contacts/{contact['id']}", headers=hb)).status_code == 404


async def test_delete_contact_keeps_thread_history(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "c7@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    contact = (await client.get("/api/v1/contacts", headers=h)).json()[0]

    deleted = await client.delete(f"/api/v1/contacts/{contact['id']}", headers=h)
    assert deleted.status_code == 204

    # SET NULL, not CASCADE: what was said is an immutable record.
    threads = await _unscoped(session, MessageThread)
    assert len(threads) == 1
    assert threads[0].contact_id is None
    msgs = await client.get("/api/v1/messages", headers=h)
    assert len(msgs.json()) == 1
