from __future__ import annotations

import inspect
import json
import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.models import Contact, Message, MessageThread, Org, OrgMembership, Role
from app.repositories import users as users_repo
from app.services import contact_lifecycle as lifecycle_svc
from app.storage.base import InMemoryObjectStore
from tests.conftest import auth_headers, create_contact, create_org, register_and_login


def _with_org_id(model, org_id, **kwargs):
    if "org_id" in model.__table__.columns.keys():
        kwargs["org_id"] = org_id
    return kwargs


def _message_kwargs(model, org_id, thread_id, body):
    kwargs = {"id": uuid.uuid4(), "thread_id": thread_id}
    cols = model.__table__.columns.keys()
    if "body" in cols:
        kwargs["body"] = body
    if "direction" in cols:
        kwargs["direction"] = "inbound"
    if "status" in cols:
        kwargs["status"] = "received"
    if "from_e164" in cols:
        kwargs["from_e164"] = "+12145550701"
    if "to_e164" in cols:
        kwargs["to_e164"] = "+12145559999"
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


async def _set_org_visibility(session, org_id, visibility):
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.contact_visibility = visibility
    await session.commit()
    set_org_context(session, org_id)


async def test_export_respects_requester_visibility(client, session):
    owner_token = await register_and_login(client, "export-owner@example.com")
    org = await create_org(client, owner_token, "Export Owner Vis")
    org_id = uuid.UUID(org["id"])
    await _set_org_visibility(session, org_id, "owner")

    token_a, _ = await _register_member(
        client, session, org_id, "export-agent-a@example.com", "agent"
    )
    token_b, _ = await _register_member(
        client, session, org_id, "export-agent-b@example.com", "agent"
    )

    await create_contact(client, token_a, org_id, "Alpha Agent Contact", ["+12145550100"])
    await create_contact(client, token_b, org_id, "Beta Agent Contact", ["+12145550111"])

    r = await client.post(
        "/api/v1/contacts/export", json={}, headers=auth_headers(token_a, org_id)
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    await lifecycle_svc.wait_for_pending_export_tasks()

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}", headers=auth_headers(token_a, org_id)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download",
        headers=auth_headers(token_a, org_id),
    )
    assert r.status_code == 200, r.text
    text = r.text
    assert "Alpha Agent Contact" in text
    assert "Beta Agent Contact" not in text


async def test_export_streams_without_loading_all_rows(client, session):
    source = inspect.getsource(lifecycle_svc.run_export)
    assert "stream_scalars" in source
    assert "partitions(" in source
    assert "stmt).all()" not in source
    # The contact scan is fed through stream_scalars/partitions. Side lookups
    # (custom field defs, phone rows) use small .scalars().all() calls, so an
    # unconditional ".scalars().all() not in source" would fail against this
    # implementation. What must not happen is a `.all()` over the contact stmt
    # itself; that is guarded by the explicit `stmt).all()` check above.
    assert "execution_options(yield_per=EXPORT_BATCH_SIZE)" in source
    assert isinstance(lifecycle_svc.EXPORT_BATCH_SIZE, int)
    assert lifecycle_svc.EXPORT_BATCH_SIZE > 0

    token = await register_and_login(client, "export-bulk@example.com")
    org = await create_org(client, token, "Export Bulk")
    org_id = uuid.UUID(org["id"])

    for i in range(25):
        await create_contact(
            client,
            token,
            org_id,
            f"Bulk Contact {i:02d}",
            [f"+121455601{i:02d}"],
        )

    set_org_context(session, org_id)
    membership = (await session.execute(sa.select(OrgMembership))).scalars().first()
    role = await session.get(Role, membership.role_id)

    store = InMemoryObjectStore()
    job_id = uuid.uuid4()
    status = await lifecycle_svc.run_export(
        get_sessionmaker(),
        store,
        org_id=org_id,
        job_id=job_id,
        requester_user_id=membership.user_id,
        permissions=list(role.permissions or []),
    )
    assert status["status"] == "done", status
    assert status["rows"] == 25

    csv_bytes = await lifecycle_svc.read_export_csv(store, org_id, job_id)
    lines = csv_bytes.decode().splitlines()
    assert len(lines) == 26  # header + 25 contacts
    assert lines[0].startswith("id,display_name")


async def test_export_download_is_private_to_the_person_who_asked(client, session):
    owner_token = await register_and_login(client, "export-private-owner@example.com")
    org = await create_org(client, owner_token, "Export Private")
    org_id = uuid.UUID(org["id"])

    token_a, _ = await _register_member(
        client, session, org_id, "export-private-a@example.com", "agent"
    )
    token_b, _ = await _register_member(
        client, session, org_id, "export-private-b@example.com", "agent"
    )

    await create_contact(client, token_a, org_id, "Visible Export Contact", ["+12145560200"])

    r = await client.post(
        "/api/v1/contacts/export", json={}, headers=auth_headers(token_a, org_id)
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    await lifecycle_svc.wait_for_pending_export_tasks()

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}", headers=auth_headers(token_b, org_id)
    )
    assert r.status_code == 404, r.text

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download",
        headers=auth_headers(token_b, org_id),
    )
    assert r.status_code == 404, r.text

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}", headers=auth_headers(owner_token, org_id)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"

    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download",
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200, r.text
    assert "Visible Export Contact" in r.text


async def test_saved_view_private_vs_shared_permissions(client, session):
    owner_token = await register_and_login(client, "views-owner@example.com")
    org = await create_org(client, owner_token, "Views")
    org_id = uuid.UUID(org["id"])

    token_a, _ = await _register_member(
        client, session, org_id, "views-agent-a@example.com", "agent"
    )
    token_b, _ = await _register_member(
        client, session, org_id, "views-agent-b@example.com", "agent"
    )

    r = await client.post(
        "/api/v1/contacts/views",
        json={"name": "Mine", "filters": {"scope": "mine"}},
        headers=auth_headers(token_a, org_id),
    )
    assert r.status_code == 201, r.text
    private_view = r.json()
    assert private_view["shared"] is False

    r = await client.get("/api/v1/contacts/views", headers=auth_headers(token_b, org_id))
    assert r.status_code == 200, r.text
    assert all(v["id"] != private_view["id"] for v in r.json())

    r = await client.post(
        "/api/v1/contacts/views",
        json={"name": "Shared", "filters": {}, "shared": True},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 201, r.text
    shared_view = r.json()
    assert shared_view["shared"] is True

    for token in (token_a, token_b):
        r = await client.get("/api/v1/contacts/views", headers=auth_headers(token, org_id))
        assert r.status_code == 200, r.text
        assert any(v["id"] == shared_view["id"] for v in r.json())

    r = await client.patch(
        f"/api/v1/contacts/views/{private_view['id']}",
        json={"name": "Mine Renamed"},
        headers=auth_headers(token_b, org_id),
    )
    assert r.status_code == 404, r.text

    r = await client.delete(
        f"/api/v1/contacts/views/{private_view['id']}",
        headers=auth_headers(token_b, org_id),
    )
    assert r.status_code == 404, r.text

    viewer_role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name="viewer",
        permissions=["contacts:read"],
        is_system=False,
    )
    session.add(viewer_role)
    await session.commit()
    set_org_context(session, org_id)

    viewer_token, _ = await _register_member(
        client, session, org_id, "views-viewer@example.com", role_id=viewer_role.id
    )
    r = await client.post(
        "/api/v1/contacts/views",
        json={"name": "Bad Shared", "filters": {}, "shared": True},
        headers=auth_headers(viewer_token, org_id),
    )
    assert r.status_code == 403, r.text


async def test_export_my_data_scoped_to_one_contact(client, session):
    token = await register_and_login(client, "my-data@example.com")
    org = await create_org(client, token, "My Data")
    org_id = uuid.UUID(org["id"])

    c1 = await create_contact(client, token, org_id, "First Person", ["+12145560300"])
    c2 = await create_contact(client, token, org_id, "Second Person", ["+12145560311"])

    set_org_context(session, org_id)
    contact1 = await session.get(Contact, uuid.UUID(c1["id"]))
    contact2 = await session.get(Contact, uuid.UUID(c2["id"]))

    thread1 = MessageThread(
        **_with_org_id(MessageThread,
            org_id,
            id=uuid.uuid4(),
            contact_id=contact1.id,
            our_e164="+12145559999",
            contact_e164="+12145550701",
            status="open",)
    )
    thread2 = MessageThread(
        **_with_org_id(MessageThread,
            org_id,
            id=uuid.uuid4(),
            contact_id=contact2.id,
            our_e164="+12145559999",
            contact_e164="+12145550702",
            status="open",)
    )
    session.add_all([thread1, thread2])
    await session.flush()

    session.add(
        Message(
            **_message_kwargs(
                Message, org_id, thread1.id, "first unique message body"
            )
        )
    )
    session.add(
        Message(
            **_message_kwargs(
                Message, org_id, thread2.id, "second unique message body"
            )
        )
    )
    await session.commit()
    set_org_context(session, org_id)

    r = await client.get(
        f"/api/v1/contacts/{c1['id']}/export-my-data",
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    serialized = json.dumps(data, default=str)
    assert "first unique message body" in serialized
    assert "second unique message body" not in serialized
    assert "First Person" in data["csv"]
    assert "Second Person" not in data["csv"]


async def test_api_key_export_is_not_downloadable_by_a_limited_agent(client, session):
    """An API-key export is built with workspace-wide visibility (P22 rule: integrations
    act for the whole workspace), so its CSV contains every contact. It must not become a
    back door around the `owner` contact-visibility policy for an ordinary agent."""
    owner_token = await register_and_login(client, "export-ak-owner@example.com")
    org = await create_org(client, owner_token, "Export API Key")
    org_id = uuid.UUID(org["id"])
    owner_headers = auth_headers(owner_token, org_id)

    agent_token, _ = await _register_member(
        client, session, org_id, "export-ak-agent@example.com", "agent"
    )

    # A contact the agent must never see under the `owner` policy: it belongs to nobody
    # and the agent cannot write, so no ownership clause can pull it in.
    await create_contact(client, owner_token, org_id, "Owner Only Contact", ["+12145560300"])
    await _set_org_visibility(session, org_id, "owner")

    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "exporter", "scopes": ["contacts:read"]},
        headers=owner_headers,
    )
    assert created.status_code == 201, created.text
    key_headers = {"Authorization": f"Bearer {created.json()['key']}"}

    r = await client.post("/api/v1/contacts/export", json={}, headers=key_headers)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    await lifecycle_svc.wait_for_pending_export_tasks()

    # The key that asked for it can still read and download its own export.
    r = await client.get(f"/api/v1/contacts/export/{job_id}", headers=key_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download", headers=key_headers
    )
    assert r.status_code == 200, r.text
    assert "Owner Only Contact" in r.text

    # The limited agent must not, in either direction.
    agent_headers = auth_headers(agent_token, org_id)
    r = await client.get(f"/api/v1/contacts/export/{job_id}", headers=agent_headers)
    assert r.status_code == 404, r.text
    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download", headers=agent_headers
    )
    assert r.status_code == 404, r.text

    # The owner, who holds contacts:read_all, still can.
    r = await client.get(
        f"/api/v1/contacts/export/{job_id}/download", headers=owner_headers
    )
    assert r.status_code == 200, r.text
