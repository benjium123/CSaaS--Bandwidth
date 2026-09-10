from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Call,
    Inbox,
    InboxGrant,
    Message,
    MessageThread,
    OrgMembership,
    OrgNumber,
    Role,
    User,
)
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login


async def _make_number(session, org_id: uuid.UUID, e164: str) -> tuple[OrgNumber, Inbox]:
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


async def _make_message(
    session,
    org_id: uuid.UUID,
    thread: MessageThread,
    *,
    direction: str,
    body: str,
    created_at: datetime,
    status: str | None = None,
) -> Message:
    set_org_context(session, org_id)
    if status is None:
        status = "delivered" if direction == "outbound" else "received"
    from_e164 = thread.our_e164 if direction == "outbound" else thread.contact_e164
    to_e164 = thread.contact_e164 if direction == "outbound" else thread.our_e164
    msg = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction=direction,
        status=status,
        from_e164=from_e164,
        to_e164=to_e164,
        body=body,
        media=[],
        carrier="bandwidth",
        created_at=created_at,
    )
    session.add(msg)
    await session.commit()
    return msg


async def _make_call(
    session,
    org_id: uuid.UUID,
    *,
    our_e164: str,
    contact_e164: str,
    direction: str,
    status: str,
    created_at: datetime,
    ended_at: datetime | None = None,
    answered_at: datetime | None = None,
    duration_seconds: int | None = None,
) -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        direction=direction,
        carrier="bandwidth",
        status=status,
        ended_at=ended_at,
        answered_at=answered_at,
        duration_seconds=duration_seconds,
        extra={},
        created_at=created_at,
    )
    session.add(call)
    await session.commit()
    return call


async def _register_user_with_role(
    client: httpx.AsyncClient,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
) -> tuple[str, User]:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    assert user is not None

    set_org_context(session, org_id)
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"role-{email.split('@')[0]}",
        permissions=permissions,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=user.id,
            role_id=role.id,
        )
    )
    await session.commit()
    return token, user


async def test_snooze_hides_thread_from_open_list(client, session):
    owner_token = await register_and_login(client, "p26s1@example.com")
    org = await create_org(client, owner_token, "P26 Snooze Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(
        session,
        org_id,
        A,
        C,
        last_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await _make_message(
        session,
        org_id,
        thread,
        direction="inbound",
        body="hello",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": future.isoformat()},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["snoozed_until"] is not None

    open_r = await client.get("/api/v1/conversations", params={"filter": "open"}, headers=h)
    assert open_r.status_code == 200, open_r.text
    open_thread_ids = {
        item["thread_id"]
        for item in open_r.json()["items"]
        if item["thread_id"] is not None
    }
    assert str(thread.id) not in open_thread_ids

    snoozed_r = await client.get(
        "/api/v1/conversations", params={"filter": "snoozed"}, headers=h
    )
    assert snoozed_r.status_code == 200, snoozed_r.text
    snoozed_thread_ids = {
        item["thread_id"]
        for item in snoozed_r.json()["items"]
        if item["thread_id"] is not None
    }
    assert str(thread.id) in snoozed_thread_ids


async def test_unsnooze_returns_thread(client, session):
    owner_token = await register_and_login(client, "p26s2@example.com")
    org = await create_org(client, owner_token, "P26 Unsnooze Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(
        session,
        org_id,
        A,
        C,
        last_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await _make_message(
        session,
        org_id,
        thread,
        direction="inbound",
        body="hello",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": future.isoformat()},
        headers=h,
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"/api/v1/conversations/{thread.id}/snooze", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["snoozed_until"] is None

    open_r = await client.get("/api/v1/conversations", params={"filter": "open"}, headers=h)
    open_thread_ids = {
        item["thread_id"]
        for item in open_r.json()["items"]
        if item["thread_id"] is not None
    }
    assert str(thread.id) in open_thread_ids


async def test_snooze_custom_rejects_past_time(client, session):
    owner_token = await register_and_login(client, "p26s3@example.com")
    org = await create_org(client, owner_token, "P26 Past Snooze Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": past.isoformat()},
        headers=h,
    )
    assert r.status_code == 422
    assert "future" in r.json()["error"]["message"].lower()


async def test_snooze_requires_thread_visibility(client, session):
    owner_token = await register_and_login(client, "p26s4-owner@example.com")
    org = await create_org(client, owner_token, "P26 Snooze Visibility Org")
    org_id = uuid.UUID(org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    user_token, _ = await _register_user_with_role(
        client, session, org_id, "p26s4-user@example.com", ["inbox:read", "inbox:send"]
    )
    user_h = auth_headers(user_token, org["id"])

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": future.isoformat()},
        headers=user_h,
    )
    assert r.status_code == 404

    r = await client.delete(f"/api/v1/conversations/{thread.id}/snooze", headers=user_h)
    assert r.status_code == 404


async def test_snooze_requires_member_access_not_view_only(client, session):
    # Fable decision: snooze/unsnooze are MEMBER-level (can_use) actions, not view-only.
    # A "viewer" grantee can see the thread but is refused with 403 (existence not in
    # question - PermissionDeniedError, not NotFoundError); a "member" grant succeeds.
    owner_token = await register_and_login(client, "p26s5-owner@example.com")
    org = await create_org(client, owner_token, "P26 Snooze Member Access Org")
    org_id = uuid.UUID(org["id"])

    A = "+12145550100"
    C = "+19725550111"
    _, inbox = await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    viewer_token, viewer_user = await _register_user_with_role(
        client, session, org_id, "p26s5-viewer@example.com", ["inbox:read", "inbox:send"]
    )
    set_org_context(session, org_id)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="user",
            grantee_id=viewer_user.id,
            role="viewer",
        )
    )
    await session.commit()
    viewer_h = auth_headers(viewer_token, org["id"])

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": future.isoformat()},
        headers=viewer_h,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"

    r = await client.delete(f"/api/v1/conversations/{thread.id}/snooze", headers=viewer_h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"

    # Upgrade the grant to "member" (can_use) and the same calls succeed.
    grant = (
        await session.execute(
            sa.select(InboxGrant).where(InboxGrant.grantee_id == viewer_user.id)
        )
    ).scalar_one()
    grant.role = "member"
    await session.commit()

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": future.isoformat()},
        headers=viewer_h,
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"/api/v1/conversations/{thread.id}/snooze", headers=viewer_h)
    assert r.status_code == 200, r.text


async def test_snoozed_filter_excludes_call_only_pairs(client, session):
    owner_token = await register_and_login(client, "p26s5@example.com")
    org = await create_org(client, owner_token, "P26 Call Only Snooze Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    now = datetime.now(timezone.utc)
    await _make_call(
        session,
        org_id,
        our_e164=A,
        contact_e164=C,
        direction="inbound",
        status="no_answer",
        created_at=now - timedelta(hours=1),
        ended_at=now - timedelta(hours=1),
    )

    r = await client.get(
        "/api/v1/conversations", params={"filter": "snoozed"}, headers=h
    )
    assert r.status_code == 200, r.text
    contacts = [item["contact_e164"] for item in r.json()["items"]]
    assert C not in contacts
