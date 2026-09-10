from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    AuditLogEntry,
    Contact,
    ContactNote,
    ContactPhone,
    ContactTag,
    MessageThread,
    Org,
    OrgMembership,
    Role,
)
from app.repositories import users as users_repo
from app.services import contact_lifecycle as lifecycle_svc
from app.services import contacts as contacts_svc
from tests.conftest import auth_headers, create_contact, create_org, create_tag, register_and_login


def _with_org_id(model, org_id, **kwargs):
    if "org_id" in model.__table__.columns.keys():
        kwargs["org_id"] = org_id
    return kwargs


def _note_kwargs(model, org_id, contact_id):
    kwargs = {"id": uuid.uuid4(), "contact_id": contact_id}
    cols = model.__table__.columns.keys()
    if "body" in cols:
        kwargs["body"] = "loser note"
    elif "note" in cols:
        kwargs["note"] = "loser note"
    return _with_org_id(model, org_id, **kwargs)


async def _register_member(client, session, org_id, email, role_name="agent", role_id=None):
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    if role_id is None:
        role_id = (
            await session.execute(
                sa.select(Role).where(Role.name == role_name, Role.org_id == org_id)
            )
        ).scalar_one().id
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role_id
        )
    )
    await session.commit()
    set_org_context(session, org_id)
    return token, user


async def test_duplicates_detected_by_name_and_phone(client, session):
    lifecycle_svc.clear_dupes_cache()

    token = await register_and_login(client, "dupes-name-phone@example.com")
    org = await create_org(client, token, "Dupes Name Phone")
    org_id = uuid.UUID(org["id"])

    c1 = await create_contact(client, token, org_id, "Dup Person A", ["+12145560400"])
    c2 = await create_contact(client, token, org_id, "Dup Person B", ["+12145560401"])
    c3 = await create_contact(client, token, org_id, "Phone Person A", ["+12145559001"])
    c4 = await create_contact(client, token, org_id, "Phone Person B", [])
    c5 = await create_contact(client, token, org_id, "Unique Person", ["+12145560555"])

    set_org_context(session, org_id)
    for cid in (c1["id"], c2["id"]):
        row = await session.get(Contact, uuid.UUID(cid))
        row.first_name = "Dup"
        row.last_name = "Person"
        row.attributes = {"email": "dup.person@example.com"}
    await session.commit()

    set_org_context(session, org_id)
    session.add(
        ContactPhone(
            **_with_org_id(
                ContactPhone,
                org_id,
                id=uuid.uuid4(),
                contact_id=uuid.UUID(c4["id"]),
                e164="+3312145559001",
                label="mobile",
                is_primary=True,
            )
        )
    )
    await session.commit()
    set_org_context(session, org_id)

    groups = await lifecycle_svc.find_duplicates(session, org_id)

    name_group_ids = set()
    phone_group_ids = set()
    for group in groups:
        ids = {uuid.UUID(x) for x in group["contact_ids"]}
        if group["reason"] == "name_and_email":
            name_group_ids = ids
        elif group["reason"] == "phone":
            phone_group_ids.update(ids)

    assert name_group_ids == {uuid.UUID(c1["id"]), uuid.UUID(c2["id"])}
    assert {uuid.UUID(c3["id"]), uuid.UUID(c4["id"])} <= phone_group_ids
    assert uuid.UUID(c5["id"]) not in {
        uuid.UUID(x) for g in groups for x in g["contact_ids"]
    }

    r = await client.get(
        f"/api/v1/contacts/{c1['id']}/duplicates",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    listed = r.json()
    assert any(
        d["contact_id"] == c2["id"]
        and d["display_name"] == "Dup Person B"
        and "+12145560401" in d["phones"]
        for d in listed
    )


async def test_merge_reparents_everything_and_hides_loser(client, session):
    lifecycle_svc.clear_dupes_cache()

    token = await register_and_login(client, "merge-reparent@example.com")
    org = await create_org(client, token, "Merge Reparent")
    org_id = uuid.UUID(org["id"])

    survivor = await create_contact(client, token, org_id, "Survivor", ["+12145560600"])
    loser = await create_contact(client, token, org_id, "Loser", ["+12145560601"])
    tag = await create_tag(client, token, org_id, "loser-tag")

    set_org_context(session, org_id)
    survivor_row = await session.get(Contact, uuid.UUID(survivor["id"]))
    loser_row = await session.get(Contact, uuid.UUID(loser["id"]))
    survivor_row.attributes = {"shared": "survivor_value", "survivor_extra": "keep"}
    loser_row.attributes = {"shared": "loser_value", "loser_extra": "move"}

    session.add(
        ContactTag(
            **_with_org_id(
                ContactTag,
                org_id,
                contact_id=loser_row.id,
                tag_id=uuid.UUID(tag["id"]),
            )
        )
    )
    session.add(ContactNote(**_note_kwargs(ContactNote, org_id, loser_row.id)))
    session.add(
        MessageThread(
            **_with_org_id(
                MessageThread,
                org_id,
                id=uuid.uuid4(),
                contact_id=loser_row.id,
                our_e164="+12145559999",
                contact_e164="+12145559998",
                status="open",
            )
        )
    )
    await session.commit()
    set_org_context(session, org_id)

    r = await client.post(
        f"/api/v1/contacts/{survivor['id']}/merge",
        json={"loser_ids": [loser["id"]]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    set_org_context(session, org_id)
    session.expire_all()
    survivor_row = await session.get(Contact, uuid.UUID(survivor["id"]))
    loser_row = await session.get(Contact, uuid.UUID(loser["id"]))

    assert loser_row is not None
    assert loser_row.merged_into_contact_id == survivor_row.id

    phones = (
        await session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == survivor_row.id)
        )
    ).scalars().all()
    assert {p.e164 for p in phones} == {"+12145560600", "+12145560601"}

    note = (
        await session.execute(
            sa.select(ContactNote).where(ContactNote.contact_id == survivor_row.id)
        )
    ).scalars().one_or_none()
    assert note is not None

    thread = (
        await session.execute(
            sa.select(MessageThread).where(MessageThread.contact_id == survivor_row.id)
        )
    ).scalars().one_or_none()
    assert thread is not None

    tag_rows = (
        await session.execute(
            sa.select(ContactTag).where(ContactTag.contact_id == survivor_row.id)
        )
    ).scalars().all()
    assert len(tag_rows) == 1
    assert tag_rows[0].tag_id == uuid.UUID(tag["id"])

    assert survivor_row.attributes["shared"] == "survivor_value"
    assert survivor_row.attributes["survivor_extra"] == "keep"
    assert survivor_row.attributes["loser_extra"] == "move"

    audit = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "contact.merge")
        )
    ).scalars().all()
    assert len(audit) == 1


async def test_merge_keeps_loser_row_and_does_not_orphan_phones(client, session):
    lifecycle_svc.clear_dupes_cache()

    token = await register_and_login(client, "merge-orphans@example.com")
    org = await create_org(client, token, "Merge Orphans")
    org_id = uuid.UUID(org["id"])

    survivor = await create_contact(client, token, org_id, "Keep", ["+12145560700"])
    loser = await create_contact(client, token, org_id, "Hide", ["+12145560701"])

    set_org_context(session, org_id)
    loser_row = await session.get(Contact, uuid.UUID(loser["id"]))
    session.add(
        MessageThread(
            **_with_org_id(
                MessageThread,
                org_id,
                id=uuid.uuid4(),
                contact_id=loser_row.id,
                our_e164="+12145559999",
                contact_e164="+12145559998",
                status="open",
            )
        )
    )
    await session.commit()
    set_org_context(session, org_id)

    r = await client.post(
        f"/api/v1/contacts/{survivor['id']}/merge",
        json={"loser_ids": [loser["id"]]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    set_org_context(session, org_id)
    session.expire_all()
    contact_ids = set((await session.execute(sa.select(Contact.id))).scalars().all())
    phone_contact_ids = set(
        (await session.execute(sa.select(ContactPhone.contact_id))).scalars().all()
    )
    thread_contact_ids = set(
        (await session.execute(sa.select(MessageThread.contact_id))).scalars().all()
    )

    assert phone_contact_ids <= contact_ids
    assert thread_contact_ids <= contact_ids

    loser_row = await session.get(Contact, uuid.UUID(loser["id"]))
    assert loser_row is not None
    assert loser_row.merged_into_contact_id == uuid.UUID(survivor["id"])


async def test_merged_rows_excluded_everywhere(client, session):
    lifecycle_svc.clear_dupes_cache()

    token = await register_and_login(client, "merged-hidden@example.com")
    org = await create_org(client, token, "Merged Hidden")
    org_id = uuid.UUID(org["id"])

    survivor = await create_contact(client, token, org_id, "Keep Contact", ["+12145560800"])
    loser = await create_contact(client, token, org_id, "Hide Contact", ["+12145560801"])

    r = await client.post(
        f"/api/v1/contacts/{survivor['id']}/merge",
        json={"loser_ids": [loser["id"]]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/contacts", headers=auth_headers(token, org_id))
    assert r.status_code == 200, r.text
    listed_ids = [c["id"] for c in r.json()]
    assert loser["id"] not in listed_ids
    assert survivor["id"] in listed_ids

    r = await client.get(
        "/api/v1/contacts?q=Hide", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    assert all(c["id"] != loser["id"] for c in r.json())

    r = await client.get(
        f"/api/v1/contacts/{loser['id']}", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 404, r.text

    set_org_context(session, org_id)
    that_e164 = "+12145560899"
    session.add(
        ContactPhone(
            **_with_org_id(
                ContactPhone,
                org_id,
                id=uuid.uuid4(),
                contact_id=uuid.UUID(loser["id"]),
                e164=that_e164,
                label="mobile",
                is_primary=False,
            )
        )
    )
    await session.commit()
    set_org_context(session, org_id)

    found = await contacts_svc.find_contact_by_phone(session, that_e164)
    assert found is None

    # services/inbox.py applies active_contacts_filter() for conversation cards.
    # The same filter is asserted here as the repository-level visibility rule.
    row = (
        await session.execute(
            sa.select(Contact)
            .where(Contact.id == uuid.UUID(loser["id"]))
            .where(contacts_svc.active_contacts_filter())
        )
    ).scalar_one_or_none()
    assert row is None


async def test_merge_refuses_a_contact_you_cannot_see(client, session):
    lifecycle_svc.clear_dupes_cache()

    owner_token = await register_and_login(client, "merge-invisible@example.com")
    org = await create_org(client, owner_token, "Merge Invisible")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    org_row = await session.get(Org, org_id)
    org_row.contact_visibility = "owner"
    await session.commit()
    set_org_context(session, org_id)

    token_a, _ = await _register_member(
        client, session, org_id, "merge-invisible-a@example.com", "agent"
    )
    token_b, _ = await _register_member(
        client, session, org_id, "merge-invisible-b@example.com", "agent"
    )

    c_a = await create_contact(client, token_a, org_id, "Agent A Contact", ["+12145560900"])
    c_b = await create_contact(client, token_b, org_id, "Agent B Contact", ["+12145560901"])

    r = await client.post(
        f"/api/v1/contacts/{c_a['id']}/merge",
        json={"loser_ids": [c_b["id"]]},
        headers=auth_headers(token_a, org_id),
    )
    assert r.status_code == 404, r.text

    set_org_context(session, org_id)
    session.expire_all()
    survivor = await session.get(Contact, uuid.UUID(c_a["id"]))
    loser = await session.get(Contact, uuid.UUID(c_b["id"]))

    assert survivor is not None and survivor.merged_into_contact_id is None
    assert loser is not None and loser.merged_into_contact_id is None

    phones_a = (
        await session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == survivor.id)
        )
    ).scalars().all()
    phones_b = (
        await session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == loser.id)
        )
    ).scalars().all()
    assert len(phones_a) == 1 and len(phones_b) == 1


async def test_duplicate_detection_to_merge_flow(client, session):
    lifecycle_svc.clear_dupes_cache()

    token = await register_and_login(client, "dupe-flow@example.com")
    org = await create_org(client, token, "Dupe Flow")
    org_id = uuid.UUID(org["id"])

    survivor = await create_contact(client, token, org_id, "Flow Dup", ["+12145561000"])
    loser = await create_contact(client, token, org_id, "Flow Dup", ["+12145561001"])

    set_org_context(session, org_id)
    for cid in (survivor["id"], loser["id"]):
        row = await session.get(Contact, uuid.UUID(cid))
        row.first_name = "Flow"
        row.last_name = "Dup"
        row.attributes = {"email": "flow.dup@example.com"}
    await session.commit()
    set_org_context(session, org_id)

    r = await client.get(
        f"/api/v1/contacts/{survivor['id']}/duplicates",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    dupes = r.json()
    assert any(
        d["contact_id"] == loser["id"] and d["reason"] == "name_and_email"
        for d in dupes
    )

    r = await client.post(
        f"/api/v1/contacts/{survivor['id']}/merge",
        json={"loser_ids": [loser["id"]]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    set_org_context(session, org_id)
    phones = (
        await session.execute(
            sa.select(ContactPhone).where(
                ContactPhone.contact_id == uuid.UUID(survivor["id"])
            )
        )
    ).scalars().all()
    assert {p.e164 for p in phones} == {"+12145561000", "+12145561001"}

    r = await client.get(
        f"/api/v1/contacts/{survivor['id']}/duplicates",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json() == []
