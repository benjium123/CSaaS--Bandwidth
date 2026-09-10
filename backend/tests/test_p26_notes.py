from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Inbox,
    InboxGrant,
    Message,
    MessageThread,
    Notification,
    OrgMembership,
    OrgNumber,
    Role,
    ThreadNote,
    User,
)
from app.repositories import users as users_repo
from app.services import notifications as notifications_svc
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


async def test_note_never_leaves_as_a_message(client, session):
    owner_token = await register_and_login(client, "p26n1@example.com")
    org = await create_org(client, owner_token, "P26 Notes Org")
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

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "private note", "mention_user_ids": []},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["body"] == "private note"

    set_org_context(session, org_id)
    note_count = (
        await session.execute(
            sa.select(sa.func.count(ThreadNote.id)).where(ThreadNote.thread_id == thread.id)
        )
    ).scalar_one()
    assert note_count == 1

    message_count = (
        await session.execute(
            sa.select(sa.func.count(Message.id)).where(Message.thread_id == thread.id)
        )
    ).scalar_one()
    assert message_count == 0


async def test_mention_creates_one_notification_per_user_and_dedupes(client, session):
    owner_token = await register_and_login(client, "p26n2@example.com")
    org = await create_org(client, owner_token, "P26 Mention Org")
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

    _, alice = await _register_user_with_role(
        client, session, org_id, "alice@example.com", ["inbox:read"]
    )
    _, bob = await _register_user_with_role(
        client, session, org_id, "bob@example.com", ["inbox:read"]
    )
    set_org_context(session, org_id)
    alice.full_name = "Alice Smith"
    bob.full_name = "Bob Jones"
    await session.commit()

    body = "Loop in @Alice Smith and @Bob Jones please"
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": body, "mention_user_ids": []},
        headers=h,
    )
    assert r.status_code == 201, r.text
    note1 = r.json()
    note1_id = note1["id"]
    assert {m["user_id"] for m in note1["mentions"]} == {str(alice.id), str(bob.id)}

    set_org_context(session, org_id)

    async def note_notification_count(note_id: str) -> int:
        return (
            await session.execute(
                sa.select(sa.func.count(Notification.id)).where(
                    Notification.dedupe_key.in_(
                        [
                            f"mention:{note_id}:{alice.id}",
                            f"mention:{note_id}:{bob.id}",
                        ]
                    )
                )
            )
        ).scalar_one()

    assert await note_notification_count(note1_id) == 2

    r2 = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": body, "mention_user_ids": []},
        headers=h,
    )
    assert r2.status_code == 201, r2.text
    note2_id = r2.json()["id"]
    assert note2_id != note1_id
    assert await note_notification_count(note2_id) == 2

    # Calling the notification service twice for the SAME note id must not create a
    # second row for either teammate.
    bus = notifications_svc.bus_from_session(session)
    await notifications_svc.notify_mention(
        session,
        org_id,
        thread_id=thread.id,
        note_id=uuid.UUID(note1_id),
        author_name="Owner",
        user_ids=[alice.id, bob.id],
        bus=bus,
    )
    await notifications_svc.notify_mention(
        session,
        org_id,
        thread_id=thread.id,
        note_id=uuid.UUID(note1_id),
        author_name="Owner",
        user_ids=[alice.id, bob.id],
        bus=bus,
    )
    await session.commit()

    assert await note_notification_count(note1_id) == 2


async def test_note_requires_thread_visibility(client, session):
    owner_token = await register_and_login(client, "p26n3-owner@example.com")
    org = await create_org(client, owner_token, "P26 Visibility Org")
    org_id = uuid.UUID(org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    user_token, _ = await _register_user_with_role(
        client, session, org_id, "p26n3-user@example.com", ["inbox:read", "inbox:send"]
    )
    user_h = auth_headers(user_token, org["id"])

    r = await client.get(f"/api/v1/conversations/{thread.id}/notes", headers=user_h)
    assert r.status_code == 404

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "should not be visible", "mention_user_ids": []},
        headers=user_h,
    )
    assert r.status_code == 404


async def test_notes_require_member_access_not_view_only(client, session):
    # Fable decision: notes are a MEMBER-level (can_use) action, not view-only. A grantee
    # who can see the thread but only holds a "viewer" grant is refused with 403 (the
    # thread's existence is not in question - PermissionDeniedError, not NotFoundError).
    owner_token = await register_and_login(client, "p26n5-owner@example.com")
    org = await create_org(client, owner_token, "P26 Member Access Org")
    org_id = uuid.UUID(org["id"])

    A = "+12145550100"
    C = "+19725550111"
    _, inbox = await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    viewer_token, viewer_user = await _register_user_with_role(
        client, session, org_id, "p26n5-viewer@example.com", ["inbox:read", "inbox:send"]
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

    r = await client.get(f"/api/v1/conversations/{thread.id}/notes", headers=viewer_h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "should be refused", "mention_user_ids": []},
        headers=viewer_h,
    )
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

    r = await client.get(f"/api/v1/conversations/{thread.id}/notes", headers=viewer_h)
    assert r.status_code == 200

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "now allowed", "mention_user_ids": []},
        headers=viewer_h,
    )
    assert r.status_code == 201


async def test_mention_rejects_non_member_or_no_inbox_send(client, session):
    owner_token = await register_and_login(client, "p26n4-owner@example.com")
    org = await create_org(client, owner_token, "P26 Reject Mention Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    await register_and_login(client, "p26n4-foreign@example.com")
    foreign_user = await users_repo.get_by_email(session, "p26n4-foreign@example.com")
    assert foreign_user is not None

    _, no_inbox_user = await _register_user_with_role(
        client, session, org_id, "p26n4-noinbox@example.com", ["inbox:send"]
    )

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "Hello", "mention_user_ids": [str(foreign_user.id)]},
        headers=h,
    )
    assert r.status_code == 422

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "Hello", "mention_user_ids": [str(no_inbox_user.id)]},
        headers=h,
    )
    assert r.status_code == 422


async def test_note_appears_in_timeline_as_a_note_event(client, session):
    owner_token = await register_and_login(client, "p26n5@example.com")
    org = await create_org(client, owner_token, "P26 Note Timeline Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    _, alice = await _register_user_with_role(
        client, session, org_id, "alice.timeline@example.com", ["inbox:read"]
    )
    set_org_context(session, org_id)
    alice.full_name = "Alice Smith"
    await session.commit()

    body = "Please review @Alice Smith"
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": body, "mention_user_ids": []},
        headers=h,
    )
    assert r.status_code == 201, r.text

    timeline = await client.get(
        f"/api/v1/conversations/{quote(C, safe='')}/timeline",
        params={"our_e164": A},
        headers=h,
    )
    assert timeline.status_code == 200, timeline.text

    note_items = [item for item in timeline.json()["items"] if item["kind"] == "note"]
    assert len(note_items) == 1
    note_item = note_items[0]
    assert note_item["body"] == body
    assert [m["name"] for m in note_item["mentions"]] == ["Alice Smith"]


async def test_ambiguous_first_name_mention_matches_nobody(client, session):
    owner_token = await register_and_login(client, "p26n6@example.com")
    org = await create_org(client, owner_token, "P26 Ambiguous Mention Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    _, sam1 = await _register_user_with_role(
        client, session, org_id, "sam.alpha@example.com", ["inbox:read"]
    )
    _, sam2 = await _register_user_with_role(
        client, session, org_id, "sam.beta@example.com", ["inbox:read"]
    )
    set_org_context(session, org_id)
    sam1.full_name = "Sam Alpha"
    sam2.full_name = "Sam Beta"
    await session.commit()

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "@Sam please help", "mention_user_ids": []},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["mentions"] == []

    set_org_context(session, org_id)
    notification_count = (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(
                Notification.user_id.in_([sam1.id, sam2.id])
            )
        )
    ).scalar_one()
    assert notification_count == 0


async def test_first_name_mention_does_not_match_a_longer_name(client, session):
    """A note naming "@Sammy Jones" must not notify a teammate called "Sam" - the
    mention match is a whole-token match, not a substring one."""
    owner_token = await register_and_login(client, "p26n7@example.com")
    org = await create_org(client, owner_token, "P26 Boundary Mention Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    _, sam = await _register_user_with_role(
        client, session, org_id, "sam.only@example.com", ["inbox:read"]
    )
    _, sammy = await _register_user_with_role(
        client, session, org_id, "sammy.jones@example.com", ["inbox:read"]
    )
    set_org_context(session, org_id)
    sam.full_name = "Sam"
    sammy.full_name = "Sammy Jones"
    await session.commit()

    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "Handing this to @Sammy Jones", "mention_user_ids": []},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert {m["user_id"] for m in r.json()["mentions"]} == {str(sammy.id)}

    set_org_context(session, org_id)
    sam_rows = (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(Notification.user_id == sam.id)
        )
    ).scalar_one()
    assert sam_rows == 0
