from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.api.routes.softphone import _event_visible
from app.db.base import set_org_context
from app.models import (
    Call,
    Inbox,
    InboxGrant,
    MessageThread,
    Notification,
    OrgMembership,
    OrgNumber,
    Role,
    User,
)
from app.services import notifications as notifications_svc
from app.services.inbox_access import InboxAccess
from tests.conftest import auth_headers, create_org, register_and_login


async def _get_user_by_email(session, email: str) -> User | None:
    return (
        await session.execute(sa.select(User).where(User.email == email))
    ).scalar_one_or_none()


async def _register_user_with_role(
    client,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
) -> tuple[str, User]:
    token = await register_and_login(client, email)
    user = await _get_user_by_email(session, email)
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


async def _make_thread(
    session, org_id: uuid.UUID, our_e164: str, contact_e164: str
) -> MessageThread:
    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        status="open",
        ai_state="off",
    )
    session.add(thread)
    await session.commit()
    return thread


async def test_notifications_read_all(client, session):
    token = await register_and_login(client, "notif-read-all@example.com")
    org = await create_org(client, token, "Notifications Read All")
    org_id = uuid.UUID(org["id"])
    user = await _get_user_by_email(session, "notif-read-all@example.com")
    assert user is not None

    set_org_context(session, org_id)
    for i in range(3):
        row = await notifications_svc.create(
            session,
            org_id,
            user_id=user.id,
            kind="mention",
            body=f"Note {i}",
            dedupe_key=f"read-all:{i}",
        )
        assert row is not None
    await session.commit()

    headers = auth_headers(token, str(org_id))
    r = await client.post(
        "/api/v1/me/notifications/read",
        json={"all": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": 3}

    r = await client.get("/api/v1/me/notifications", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["unread_count"] == 0
    assert len(r.json()["items"]) == 3


async def test_notifications_read_one_leaves_others_unread(client, session):
    token = await register_and_login(client, "notif-read-one@example.com")
    org = await create_org(client, token, "Notifications Read One")
    org_id = uuid.UUID(org["id"])
    user = await _get_user_by_email(session, "notif-read-one@example.com")
    assert user is not None

    set_org_context(session, org_id)
    rows = []
    for i in range(3):
        row = await notifications_svc.create(
            session,
            org_id,
            user_id=user.id,
            kind="mention",
            body=f"Note {i}",
            dedupe_key=f"read-one:{i}",
        )
        assert row is not None
        rows.append(row)
    await session.commit()

    headers = auth_headers(token, str(org_id))
    r = await client.post(
        "/api/v1/me/notifications/read",
        json={"ids": [str(rows[0].id)]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": 1}

    r = await client.get(
        "/api/v1/me/notifications",
        params={"unread": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["unread_count"] == 2
    assert len(r.json()["items"]) == 2


async def test_notifications_read_requires_a_selection(client):
    token = await register_and_login(client, "notif-read-selection@example.com")
    org = await create_org(client, token, "Notifications Read Selection")
    headers = auth_headers(token, str(org["id"]))

    r = await client.post("/api/v1/me/notifications/read", json={}, headers=headers)
    assert r.status_code == 422, r.text
    assert "read" in r.json()["error"]["message"]


async def test_notification_list_is_personal(client, session):
    owner_token = await register_and_login(client, "notif-personal-owner@example.com")
    org = await create_org(client, owner_token, "Notifications Personal")
    org_id = uuid.UUID(org["id"])

    token_a, user_a = await _register_user_with_role(
        client, session, org_id, "notif-personal-a@example.com", ["*"]
    )
    token_b, user_b = await _register_user_with_role(
        client, session, org_id, "notif-personal-b@example.com", ["*"]
    )

    set_org_context(session, org_id)
    await notifications_svc.create(
        session,
        org_id,
        user_id=user_a.id,
        kind="mention",
        body="Alice private",
        dedupe_key="personal:a",
    )
    await notifications_svc.create(
        session,
        org_id,
        user_id=user_b.id,
        kind="mention",
        body="Bob private",
        dedupe_key="personal:b",
    )
    await session.commit()

    headers_a = auth_headers(token_a, str(org_id))
    r = await client.get("/api/v1/me/notifications", headers=headers_a)
    assert r.status_code == 200, r.text
    bodies_a = [item["body"] for item in r.json()["items"]]
    assert bodies_a == ["Alice private"]

    headers_b = auth_headers(token_b, str(org_id))
    r = await client.get("/api/v1/me/notifications", headers=headers_b)
    assert r.status_code == 200, r.text
    bodies_b = [item["body"] for item in r.json()["items"]]
    assert bodies_b == ["Bob private"]


async def test_dedupe_key_blocks_a_second_row(client, session):
    token = await register_and_login(client, "notif-dedupe-owner@example.com")
    org = await create_org(client, token, "Notifications Dedupe")
    org_id = uuid.UUID(org["id"])
    owner = await _get_user_by_email(session, "notif-dedupe-owner@example.com")
    assert owner is not None

    _, user_b = await _register_user_with_role(
        client, session, org_id, "notif-dedupe-b@example.com", ["*"]
    )

    set_org_context(session, org_id)
    row1 = await notifications_svc.create(
        session,
        org_id,
        user_id=owner.id,
        kind="mention",
        body="first",
        dedupe_key="dup",
    )
    assert row1 is not None
    await session.commit()

    set_org_context(session, org_id)
    row2 = await notifications_svc.create(
        session,
        org_id,
        user_id=owner.id,
        kind="mention",
        body="second",
        dedupe_key="dup",
    )
    assert row2 is None

    count = (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(
                Notification.user_id == owner.id,
                Notification.dedupe_key == "dup",
            )
        )
    ).scalar_one()
    assert count == 1

    row3 = await notifications_svc.create(
        session,
        org_id,
        user_id=user_b.id,
        kind="mention",
        body="other user same key",
        dedupe_key="dup",
    )
    assert row3 is not None
    await session.commit()


async def test_missed_call_notification_only_for_visible_inboxes(client, session):
    owner_token = await register_and_login(client, "notif-missed-owner@example.com")
    org = await create_org(client, owner_token, "Notifications Missed Scope")
    org_id = uuid.UUID(org["id"])
    owner = await _get_user_by_email(session, "notif-missed-owner@example.com")
    assert owner is not None

    member_token, member = await _register_user_with_role(
        client, session, org_id, "notif-missed-member@example.com", ["inbox:read"]
    )

    number_a, inbox_a = await _make_number(session, org_id, "+12145550101")
    number_b, inbox_b = await _make_number(session, org_id, "+12145550102")

    set_org_context(session, org_id)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            grantee_type="user",
            grantee_id=member.id,
            inbox_id=inbox_a.id,
            role="member",
        )
    )
    await session.commit()

    now = datetime.now(timezone.utc)
    await _make_call(
        session,
        org_id,
        our_e164=number_a.e164,
        contact_e164="+19725550101",
        direction="inbound",
        status="no_answer",
        created_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )
    await _make_call(
        session,
        org_id,
        our_e164=number_b.e164,
        contact_e164="+19725550102",
        direction="inbound",
        status="no_answer",
        created_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )

    result = await notifications_svc.missed_call_tick(session)
    assert result["notifications"] == 3

    set_org_context(session, org_id)
    member_notes = await notifications_svc.list_for_user(session, org_id, member.id)
    assert len(member_notes) == 1
    assert member_notes[0].body == "Missed call from +19725550101"

    owner_notes = await notifications_svc.list_for_user(session, org_id, owner.id)
    assert len(owner_notes) == 2
    owner_bodies = {n.body for n in owner_notes}
    assert "Missed call from +19725550101" in owner_bodies
    assert "Missed call from +19725550102" in owner_bodies

    result_again = await notifications_svc.missed_call_tick(session)
    assert result_again["notifications"] == 0


async def test_missed_call_tick_ignores_answered_and_outbound_calls(client, session):
    token = await register_and_login(client, "notif-missed-ignore@example.com")
    org = await create_org(client, token, "Notifications Missed Ignore")
    org_id = uuid.UUID(org["id"])
    owner = await _get_user_by_email(session, "notif-missed-ignore@example.com")
    assert owner is not None

    number, _inbox = await _make_number(session, org_id, "+12145550101")

    now = datetime.now(timezone.utc)
    await _make_call(
        session,
        org_id,
        our_e164=number.e164,
        contact_e164="+19725550101",
        direction="inbound",
        status="no_answer",
        created_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )
    await _make_call(
        session,
        org_id,
        our_e164=number.e164,
        contact_e164="+19725550102",
        direction="inbound",
        status="completed",
        created_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )
    await _make_call(
        session,
        org_id,
        our_e164=number.e164,
        contact_e164="+19725550103",
        direction="outbound",
        status="no_answer",
        created_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )

    result = await notifications_svc.missed_call_tick(session)
    assert result["notifications"] == 1

    set_org_context(session, org_id)
    owner_notes = await notifications_svc.list_for_user(session, org_id, owner.id)
    assert len(owner_notes) == 1
    assert owner_notes[0].body == "Missed call from +19725550101"


async def test_notification_created_event_is_pushed(client, session):
    token = await register_and_login(client, "notif-event-push@example.com")
    org = await create_org(client, token, "Notifications Event Push")
    org_id = uuid.UUID(org["id"])
    user = await _get_user_by_email(session, "notif-event-push@example.com")
    assert user is not None

    class StubBus:
        def __init__(self):
            self.published: list = []

        def publish(self, org_id, event):
            self.published.append((org_id, event))

    bus = StubBus()
    set_org_context(session, org_id)
    row = await notifications_svc.create(
        session,
        org_id,
        user_id=user.id,
        kind="mention",
        body="Hello push",
        dedupe_key="event-push",
        bus=bus,
    )
    assert row is not None
    await session.commit()

    assert len(bus.published) == 1
    _, event = bus.published[0]
    assert event["type"] == "notification.created"
    assert event["user_id"] == str(user.id)
    assert event["body"] == "Hello push"
    # No thread_id on this notification - the pair fields must be present and null,
    # never omitted, so the bell's client code can rely on the keys existing.
    assert event["our_e164"] is None
    assert event["contact_e164"] is None


async def test_notification_created_event_carries_thread_pair(client, session):
    # Fable decision: when thread_id is set, the WS payload also carries the thread's
    # (our_e164, contact_e164) so the bell can navigate straight to the conversation -
    # it is addressed by that number pair, not by thread id (P20c).
    token = await register_and_login(client, "notif-event-pair@example.com")
    org = await create_org(client, token, "Notifications Event Pair")
    org_id = uuid.UUID(org["id"])
    user = await _get_user_by_email(session, "notif-event-pair@example.com")
    assert user is not None

    class StubBus:
        def __init__(self):
            self.published: list = []

        def publish(self, org_id, event):
            self.published.append((org_id, event))

    A = "+12145550100"
    C = "+19725550111"
    thread = await _make_thread(session, org_id, A, C)

    bus = StubBus()
    set_org_context(session, org_id)
    row = await notifications_svc.create(
        session,
        org_id,
        user_id=user.id,
        kind="mention",
        body="Hello with a thread",
        thread_id=thread.id,
        dedupe_key="event-pair",
        bus=bus,
    )
    assert row is not None
    await session.commit()

    assert len(bus.published) == 1
    _, event = bus.published[0]
    assert event["thread_id"] == str(thread.id)
    assert event["our_e164"] == A
    assert event["contact_e164"] == C

    headers = auth_headers(token, str(org_id))
    r = await client.get("/api/v1/me/notifications", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["thread_id"] == str(thread.id)
    assert items[0]["our_e164"] == A
    assert items[0]["contact_e164"] == C


async def test_ws_gate_hides_another_users_notification():
    org_id = uuid.uuid4()
    user_x = uuid.uuid4()
    user_y = uuid.uuid4()

    admin_access = InboxAccess(
        is_admin=True,
        member_e164s=frozenset(),
        viewer_e164s=frozenset(),
    )

    event = {"type": "notification.created", "user_id": str(user_x)}
    assert await _event_visible(event, admin_access, org_id, user_x) is True
    assert await _event_visible(event, admin_access, org_id, user_y) is False

    no_user_event = {"type": "notification.created"}
    assert await _event_visible(no_user_event, admin_access, org_id, user_x) is False
