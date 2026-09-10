from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Inbox, MessageThread, OrgMembership, OrgNumber, Role, User
from app.models.inbox_pro import ThreadNote
from app.services import notifications as notifications_svc
from tests.conftest import auth_headers, create_org, register_and_login


async def _get_user_by_email(session, email: str) -> User | None:
    return (
        await session.execute(sa.select(User).where(User.email == email))
    ).scalar_one_or_none()


async def _make_number(
    session, org_id: uuid.UUID, e164: str
) -> tuple[OrgNumber, Inbox]:
    set_org_context(session, org_id)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164=e164,
        carrier="bandwidth",
        capabilities={},
        status="active",
    )
    session.add(number)
    await session.flush()
    inbox = Inbox(id=uuid.uuid4(), org_id=org_id, name=e164, number_id=number.id)
    session.add(inbox)
    await session.commit()
    return number, inbox


async def _make_thread(
    session,
    org_id: uuid.UUID,
    our_e164: str,
    contact_e164: str,
    *,
    last_message_at: datetime | None = None,
    status: str = "open",
    last_read_at: datetime | None = None,
) -> MessageThread:
    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        last_message_at=last_message_at,
        status=status,
        last_read_at=last_read_at,
        ai_state="off",
    )
    session.add(thread)
    await session.commit()
    return thread


async def _setup_two_orgs(client, session, *, a_email: str, b_email: str) -> dict:
    token_a = await register_and_login(client, a_email)
    org_a = await create_org(client, token_a, "Tenancy Org A")
    token_b = await register_and_login(client, b_email)
    org_b = await create_org(client, token_b, "Tenancy Org B")

    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    number_a, inbox_a = await _make_number(session, org_a_id, "+12145550101")
    number_b, inbox_b = await _make_number(session, org_b_id, "+12145550102")
    thread_a = await _make_thread(
        session, org_a_id, number_a.e164, "+19725550001"
    )
    thread_b = await _make_thread(
        session, org_b_id, number_b.e164, "+19725550001"
    )

    return {
        "token_a": token_a,
        "token_b": token_b,
        "org_a_id": org_a_id,
        "org_b_id": org_b_id,
        "inbox_a": inbox_a,
        "inbox_b": inbox_b,
        "thread_a": thread_a,
        "thread_b": thread_b,
        "number_a": number_a,
        "number_b": number_b,
    }


async def test_cross_org_ids_are_404(client, session):
    ctx = await _setup_two_orgs(
        client, session, a_email="tenancy-404-a@example.com", b_email="tenancy-404-b@example.com"
    )
    headers_b = auth_headers(ctx["token_b"], str(ctx["org_b_id"]))
    thread_a_id = str(ctx["thread_a"].id)
    inbox_a_id = str(ctx["inbox_a"].id)

    r = await client.get(f"/api/v1/conversations/{thread_a_id}/notes", headers=headers_b)
    assert r.status_code == 404, r.text

    r = await client.post(
        f"/api/v1/conversations/{thread_a_id}/notes",
        json={"body": "notes cross-org", "mentions": []},
        headers=headers_b,
    )
    assert r.status_code == 404, r.text

    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = await client.post(
        f"/api/v1/conversations/{thread_a_id}/snooze",
        json={"until": until, "duration_minutes": 60},
        headers=headers_b,
    )
    assert r.status_code == 404, r.text

    r = await client.delete(
        f"/api/v1/conversations/{thread_a_id}/snooze", headers=headers_b
    )
    assert r.status_code == 404, r.text

    r = await client.patch(
        f"/api/v1/inboxes/{inbox_a_id}",
        json={"sla_first_response_minutes": 15, "sla_resolution_minutes": 120},
        headers=headers_b,
    )
    assert r.status_code == 404, r.text


async def test_notes_are_not_visible_across_orgs(client, session):
    a_email = "tenancy-notes-a@example.com"
    b_email = "tenancy-notes-b@example.com"
    ctx = await _setup_two_orgs(client, session, a_email=a_email, b_email=b_email)
    org_a_id = ctx["org_a_id"]
    org_b_id = ctx["org_b_id"]
    thread_a = ctx["thread_a"]
    thread_b = ctx["thread_b"]

    author_a = await _get_user_by_email(session, a_email)
    assert author_a is not None

    set_org_context(session, org_a_id)
    session.add(
        ThreadNote(
            id=uuid.uuid4(),
            org_id=org_a_id,
            thread_id=thread_a.id,
            author_user_id=author_a.id,
            body="secret org A note",
            mentions=[],
        )
    )
    await session.commit()

    headers_b = auth_headers(ctx["token_b"], str(org_b_id))
    r = await client.get(
        f"/api/v1/conversations/{thread_b.id}/notes", headers=headers_b
    )
    assert r.status_code == 200, r.text
    assert not any(note.get("body") == "secret org A note" for note in r.json())

    set_org_context(session, org_b_id)
    b_notes = (
        await session.execute(
            sa.select(ThreadNote).where(ThreadNote.thread_id == thread_b.id)
        )
    ).scalars().all()
    assert b_notes == []


async def test_notifications_never_cross_orgs(client, session):
    ctx = await _setup_two_orgs(
        client,
        session,
        a_email="tenancy-notif-a@example.com",
        b_email="tenancy-notif-b@example.com",
    )
    org_a_id = ctx["org_a_id"]
    org_b_id = ctx["org_b_id"]

    dual_token = await register_and_login(client, "tenancy-dual@example.com")
    dual_user = await _get_user_by_email(session, "tenancy-dual@example.com")
    assert dual_user is not None

    for org_id, name in ((org_a_id, "Dual Role A"), (org_b_id, "Dual Role B")):
        set_org_context(session, org_id)
        role = Role(
            id=uuid.uuid4(),
            org_id=org_id,
            name=name,
            permissions=["*"],
        )
        session.add(role)
        await session.flush()
        session.add(
            OrgMembership(
                id=uuid.uuid4(),
                org_id=org_id,
                user_id=dual_user.id,
                role_id=role.id,
            )
        )
        await session.commit()

    set_org_context(session, org_a_id)
    row = await notifications_svc.create(
        session,
        org_a_id,
        user_id=dual_user.id,
        kind="mention",
        body="A-only mention",
        dedupe_key="cross-org-mention",
    )
    assert row is not None
    await session.commit()

    headers_a = auth_headers(dual_token, str(org_a_id))
    r = await client.get("/api/v1/me/notifications", headers=headers_a)
    assert r.status_code == 200, r.text
    assert any(item["body"] == "A-only mention" for item in r.json()["items"])

    headers_b = auth_headers(dual_token, str(org_b_id))
    r = await client.get("/api/v1/me/notifications", headers=headers_b)
    assert r.status_code == 200, r.text
    assert not any(item["body"] == "A-only mention" for item in r.json()["items"])


async def test_overdue_and_snoozed_filters_are_scoped(client, session):
    ctx = await _setup_two_orgs(
        client,
        session,
        a_email="tenancy-filters-a@example.com",
        b_email="tenancy-filters-b@example.com",
    )
    org_a_id = ctx["org_a_id"]
    org_b_id = ctx["org_b_id"]
    headers_a = auth_headers(ctx["token_a"], str(org_a_id))
    headers_b = auth_headers(ctx["token_b"], str(org_b_id))

    set_org_context(session, org_a_id)
    snoozed = await _make_thread(
        session, org_a_id, ctx["number_a"].e164, "+19725550002"
    )
    overdue = await _make_thread(
        session, org_a_id, ctx["number_a"].e164, "+19725550003"
    )
    now = datetime.now(timezone.utc)
    snoozed.snoozed_until = now + timedelta(hours=1)
    overdue.sla_breached_at = now
    await session.commit()

    r = await client.get(
        "/api/v1/conversations", params={"filter": "snoozed"}, headers=headers_a
    )
    assert r.status_code == 200, r.text
    assert str(snoozed.id) in [item["thread_id"] for item in r.json()["items"]]

    r = await client.get(
        "/api/v1/conversations", params={"filter": "overdue"}, headers=headers_a
    )
    assert r.status_code == 200, r.text
    assert str(overdue.id) in [item["thread_id"] for item in r.json()["items"]]

    r = await client.get(
        "/api/v1/conversations", params={"filter": "snoozed"}, headers=headers_b
    )
    assert r.status_code == 200, r.text
    assert str(snoozed.id) not in [item["thread_id"] for item in r.json()["items"]]

    r = await client.get(
        "/api/v1/conversations", params={"filter": "overdue"}, headers=headers_b
    )
    assert r.status_code == 200, r.text
    assert str(overdue.id) not in [item["thread_id"] for item in r.json()["items"]]


async def test_sla_targets_are_per_org(client, session):
    ctx = await _setup_two_orgs(
        client,
        session,
        a_email="tenancy-sla-a@example.com",
        b_email="tenancy-sla-b@example.com",
    )
    org_a_id = ctx["org_a_id"]
    org_b_id = ctx["org_b_id"]
    headers_a = auth_headers(ctx["token_a"], str(org_a_id))

    # Capture the ids BEFORE expiring: touching an expired instance attribute would
    # trigger a lazy refresh outside the async context and blow up with MissingGreenlet.
    inbox_a_id = ctx["inbox_a"].id
    inbox_b_id = ctx["inbox_b"].id

    r = await client.patch(
        f"/api/v1/inboxes/{inbox_a_id}",
        json={"sla_first_response_minutes": 15, "sla_resolution_minutes": 120},
        headers=headers_a,
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    set_org_context(session, org_a_id)
    inbox_a_after = await session.get(Inbox, inbox_a_id)
    assert inbox_a_after.sla_first_response_minutes == 15
    assert inbox_a_after.sla_resolution_minutes == 120

    set_org_context(session, org_b_id)
    inbox_b_after = await session.get(Inbox, inbox_b_id)
    assert inbox_b_after.sla_first_response_minutes is None
    assert inbox_b_after.sla_resolution_minutes is None
