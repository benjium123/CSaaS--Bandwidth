"""Deleting a contact hides it (0079 soft delete) and restore brings it back."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Contact, ContactPhone, MessageThread
from app.services import privacy as privacy_svc
from app.storage.base import InMemoryObjectStore
from tests.conftest import (
    auth_headers,
    create_contact,
    create_org,
    fixture_bytes,
    make_org_with_number,
    register_and_login,
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


async def test_delete_hides_the_contact_and_keeps_the_row(client):
    token = await register_and_login(client, "sd1@example.com")
    org = await create_org(client, token, "SD Org")
    h = auth_headers(token, org["id"])
    contact = await create_contact(client, token, org["id"], "Ada Lovelace", [THEIRS])
    note = await client.post(
        f"/api/v1/contacts/{contact['id']}/notes", json={"body": "met at expo"}, headers=h
    )
    assert note.status_code == 201, note.text

    r = await client.delete(f"/api/v1/contacts/{contact['id']}", headers=h)
    assert r.status_code == 204

    assert (await client.get("/api/v1/contacts", headers=h)).json() == []
    assert (await client.get(f"/api/v1/contacts/{contact['id']}", headers=h)).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/contacts/{contact['id']}", json={"display_name": "X"}, headers=h
        )
    ).status_code == 404

    deleted = (await client.get("/api/v1/contacts/deleted", headers=h)).json()
    assert [d["id"] for d in deleted] == [contact["id"]]
    assert deleted[0]["display_name"] == "Ada Lovelace"
    assert [p["e164"] for p in deleted[0]["phones"]] == [THEIRS]
    assert deleted[0]["deleted_at"] is not None

    # The number is free again: a new contact may take it.
    again = await create_contact(client, token, org["id"], "Someone New", [THEIRS])
    assert [p["e164"] for p in again["phones"]] == [THEIRS]


async def test_restore_brings_back_phones_notes_and_threads(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "sd2@example.com", "SD Org 2", OUR)
    h = auth_headers(token, org["id"])
    await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    contact = (await client.get("/api/v1/contacts", headers=h)).json()[0]
    await client.post(
        f"/api/v1/contacts/{contact['id']}/notes", json={"body": "keep me"}, headers=h
    )

    assert (await client.delete(f"/api/v1/contacts/{contact['id']}", headers=h)).status_code == 204
    threads = await _unscoped(session, MessageThread)
    assert [t.contact_id for t in threads] == [None]

    r = await client.post(f"/api/v1/contacts/{contact['id']}/restore", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phones_skipped"] == []
    assert [p["e164"] for p in body["contact"]["phones"]] == [THEIRS]

    listed = (await client.get("/api/v1/contacts", headers=h)).json()
    assert [c["id"] for c in listed] == [contact["id"]]
    assert (await client.get("/api/v1/contacts/deleted", headers=h)).json() == []
    notes = (await client.get(f"/api/v1/contacts/{contact['id']}/notes", headers=h)).json()
    assert [n["body"] for n in notes] == ["keep me"]

    session.expire_all()
    threads = await _unscoped(session, MessageThread)
    assert [str(t.contact_id) for t in threads] == [contact["id"]]


async def test_inbound_text_after_delete_starts_a_fresh_contact(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "sd3@example.com", "SD Org 3", OUR)
    h = auth_headers(token, org["id"])
    old = await create_contact(client, token, org["id"], "Old Name", [THEIRS])
    assert (await client.delete(f"/api/v1/contacts/{old['id']}", headers=h)).status_code == 204

    await client.post(
        HOOK, content=fixture_bytes("message-received.json"), headers=webhook_auth_headers()
    )
    listed = (await client.get("/api/v1/contacts", headers=h)).json()
    assert len(listed) == 1
    assert listed[0]["id"] != old["id"]

    # The number is now taken by the fresh contact, so restore brings the old one back
    # without it and says so.
    r = await client.post(f"/api/v1/contacts/{old['id']}/restore", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["phones_skipped"] == [THEIRS]
    assert r.json()["contact"]["phones"] == []
    phones = await _unscoped(session, ContactPhone)
    assert [str(p.contact_id) for p in phones] == [listed[0]["id"]]


async def test_restore_needs_a_deleted_contact(client):
    token = await register_and_login(client, "sd4@example.com")
    org = await create_org(client, token, "SD Org 4")
    h = auth_headers(token, org["id"])
    contact = await create_contact(client, token, org["id"], "Live One", [THEIRS])
    r = await client.post(f"/api/v1/contacts/{contact['id']}/restore", headers=h)
    assert r.status_code == 409


async def test_a_deleted_contact_can_still_be_exported_and_erased(client, session):
    token = await register_and_login(client, "sd5@example.com")
    org = await create_org(client, token, "SD Org 5")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)
    contact = await create_contact(client, token, org["id"], "Gone Person", [THEIRS])
    assert (await client.delete(f"/api/v1/contacts/{contact['id']}", headers=h)).status_code == 204

    mine = await client.get(f"/api/v1/contacts/{contact['id']}/export-my-data", headers=h)
    assert mine.status_code == 200, mine.text

    r = await client.post(f"/api/v1/contacts/{contact['id']}/erase", headers=h)
    assert r.status_code == 202, r.text
    result = await privacy_svc.erasure_tick(session, InMemoryObjectStore())
    assert result["completed"] == 1, result

    set_org_context(session, org_id)
    session.expire_all()
    row = await session.get(Contact, uuid.UUID(contact["id"]))
    assert row.deleted_phones is None
    assert row.display_name == privacy_svc.ERASED_NAME
