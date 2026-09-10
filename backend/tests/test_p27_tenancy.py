from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import set_org_context
from app.models import (
    Contact,
    Message,
    MessageThread,
    SavedView,
)
from app.services import contact_lifecycle as lifecycle_svc
from app.services import retention as retention_svc
from app.storage.base import InMemoryObjectStore
from tests.conftest import auth_headers, create_contact, create_org, register_and_login


async def _make_org(client, _session, email, name):
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, uuid.UUID(org["id"])


async def _thread_with_message(
    session, org_id, contact_id, e164, *, body="hello", created_at=None
):
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        contact_id=contact_id,
        contact_e164=e164,
        our_e164="+12145559999",
        status="open",
    )
    session.add(thread)
    await session.flush()
    msg = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction="inbound",
        status="received",
        from_e164=e164,
        to_e164="+12145559999",
        body=body,
    )
    if created_at is not None:
        msg.created_at = created_at
    session.add(msg)
    await session.flush()
    return thread, msg


async def test_saved_view_of_another_workspace_is_not_found(client, session):
    token_a, org_a_id = await _make_org(
        client, session, "p27-tenant-sv-a@example.com", "Tenant A"
    )
    token_b, org_b_id = await _make_org(
        client, session, "p27-tenant-sv-b@example.com", "Tenant B"
    )

    set_org_context(session, org_a_id)
    view_id = uuid.uuid4()
    session.add(
        SavedView(
            id=view_id,
            org_id=org_a_id,
            name="A private view",
            filters={"tag": "lead"},
        )
    )
    await session.commit()

    headers_b = auth_headers(token_b, org_b_id)

    r = await client.patch(
        f"/api/v1/contacts/views/{view_id}",
        json={"name": "hijacked"},
        headers=headers_b,
    )
    assert r.status_code == 404

    r = await client.delete(f"/api/v1/contacts/views/{view_id}", headers=headers_b)
    assert r.status_code == 404


async def test_export_job_of_another_workspace_is_not_found(client, session):
    token_a, org_a_id = await _make_org(
        client, session, "p27-tenant-exp-a@example.com", "Tenant A"
    )
    token_b, org_b_id = await _make_org(
        client, session, "p27-tenant-exp-b@example.com", "Tenant B"
    )

    # Workspace A really runs an export; the CSV and its status file live under a
    # workspace-prefixed key in the object store, which is what makes B's probe a miss.
    r = await client.post(
        "/api/v1/contacts/export", json={}, headers=auth_headers(token_a, org_a_id)
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    await lifecycle_svc.wait_for_pending_export_tasks()

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}", headers=auth_headers(token_a, org_a_id)
    )
    assert r.status_code == 200 and r.json()["status"] == "done", r.text

    headers_b = auth_headers(token_b, org_b_id)

    r = await client.get(f"/api/v1/contacts/export/{job_id}", headers=headers_b)
    assert r.status_code == 404

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download", headers=headers_b
    )
    assert r.status_code == 404


async def test_duplicates_and_merge_do_not_cross_workspaces(client, session):
    token_a, org_a_id = await _make_org(
        client, session, "p27-tenant-dup-a@example.com", "Tenant A"
    )
    token_b, org_b_id = await _make_org(
        client, session, "p27-tenant-dup-b@example.com", "Tenant B"
    )

    contact_a = await create_contact(
        client, token_a, org_a_id, "A Person", ["+12145551500"]
    )
    contact_b = await create_contact(
        client, token_b, org_b_id, "B Person", ["+12145551501"]
    )
    contact_a_id = uuid.UUID(contact_a["id"])
    headers_b = auth_headers(token_b, org_b_id)

    r = await client.get(
        f"/api/v1/contacts/{contact_a_id}/duplicates", headers=headers_b
    )
    assert r.status_code == 404

    # B tries to swallow A's contact into its own: the loser is not visible, so 404.
    r = await client.post(
        f"/api/v1/contacts/{contact_b['id']}/merge",
        json={"loser_ids": [str(contact_a_id)]},
        headers=headers_b,
    )
    assert r.status_code == 404

    r = await client.post(
        f"/api/v1/contacts/{contact_a_id}/merge",
        json={"loser_ids": [contact_b["id"]]},
        headers=headers_b,
    )
    assert r.status_code == 404

    set_org_context(session, org_a_id)
    row_a = await session.get(Contact, contact_a_id)
    await session.refresh(row_a)
    assert row_a.merged_into_contact_id is None


async def test_erase_of_another_workspace_is_not_found(client, session):
    token_a, org_a_id = await _make_org(
        client, session, "p27-tenant-erase-a@example.com", "Tenant A"
    )
    token_b, org_b_id = await _make_org(
        client, session, "p27-tenant-erase-b@example.com", "Tenant B"
    )

    contact_a = await create_contact(
        client, token_a, org_a_id, "A Person", ["+12145551600"]
    )
    contact_a_id = uuid.UUID(contact_a["id"])
    headers_b = auth_headers(token_b, org_b_id)

    r = await client.post(
        f"/api/v1/contacts/{contact_a_id}/erase", headers=headers_b
    )
    assert r.status_code == 404

    set_org_context(session, org_a_id)
    row_a = await session.get(Contact, contact_a_id)
    await session.refresh(row_a)
    assert row_a.display_name == "A Person"


async def test_retention_policy_is_per_workspace(client, session):
    token_a, org_a_id = await _make_org(
        client, session, "p27-tenant-ret-a@example.com", "Tenant A"
    )
    token_b, org_b_id = await _make_org(
        client, session, "p27-tenant-ret-b@example.com", "Tenant B"
    )

    contact_a = await create_contact(
        client, token_a, org_a_id, "A Person", ["+12145551700"]
    )
    contact_b = await create_contact(
        client, token_b, org_b_id, "B Person", ["+12145551701"]
    )

    r = await client.patch(
        "/api/v1/orgs/current/retention",
        json={"messages_days": 10},
        headers=auth_headers(token_a, org_a_id),
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        "/api/v1/orgs/current/retention",
        headers=auth_headers(token_b, org_b_id),
    )
    assert r.status_code == 200, r.text
    org_b_policy = r.json()
    assert org_b_policy["messages_days"] is None
    assert org_b_policy["recordings_days"] == 90
    assert org_b_policy["transcripts_days"] == 365
    assert org_b_policy["imports_days"] == 30

    old = datetime.now(timezone.utc) - timedelta(days=365)

    set_org_context(session, org_a_id)
    _thread_a, msg_a = await _thread_with_message(
        session,
        org_a_id,
        uuid.UUID(contact_a["id"]),
        "+12145551700",
        body="old A",
        created_at=old,
    )
    await session.commit()

    set_org_context(session, org_b_id)
    _thread_b, msg_b = await _thread_with_message(
        session,
        org_b_id,
        uuid.UUID(contact_b["id"]),
        "+12145551701",
        body="old B",
        created_at=old,
    )
    await session.commit()

    store = InMemoryObjectStore()
    future = datetime.now(timezone.utc) + timedelta(days=400)
    counts = await retention_svc.retention_tick(session, store, now=future)

    assert counts["messages"] == 1, counts

    set_org_context(session, org_a_id)
    row_a = await session.get(Message, msg_a.id)
    await session.refresh(row_a)
    assert row_a.body is None

    set_org_context(session, org_b_id)
    row_b = await session.get(Message, msg_b.id)
    await session.refresh(row_b)
    assert row_b.body == "old B"
