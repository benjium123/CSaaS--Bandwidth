from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Department,
    DepartmentMember,
    InboxGrant,
    Message,
    MessageThread,
    Notification,
    OrgMembership,
    OrgNumber,
    Role,
    User,
)
from app.models import Inbox as InboxModel
from app.services import inbox_sla
from tests.conftest import auth_headers, create_org, register_and_login


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _make_number(
    session, org_id: uuid.UUID, e164: str
) -> tuple[OrgNumber, InboxModel]:
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
    inbox = InboxModel(id=uuid.uuid4(), org_id=org_id, name=e164, number_id=number.id)
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
    assigned_user_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    snoozed_until: datetime | None = None,
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
        assigned_user_id=assigned_user_id,
        snoozed_until=snoozed_until,
        ai_state="off",
        created_at=created_at,
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
    client,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
) -> tuple[str, User]:
    token = await register_and_login(client, email)
    user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()

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


async def test_first_response_stamped_once(client, session):
    token = await register_and_login(client, "p26-first@example.com")
    org = await create_org(client, token, "P26 First")
    org_id = uuid.UUID(org["id"])

    number, inbox = await _make_number(session, org_id, "+12145550100")
    thread = await _make_thread(
        session, org_id, number.e164, "+19725550111", created_at=datetime.now(timezone.utc)
    )
    now = datetime.now(timezone.utc)
    await _make_message(
        session,
        org_id,
        thread,
        direction="inbound",
        body="hello",
        created_at=now - timedelta(minutes=1),
    )
    await _make_message(
        session,
        org_id,
        thread,
        direction="outbound",
        body="first reply",
        created_at=now,
    )

    first = await inbox_sla.stamp_first_response(session, thread)
    assert first is True
    first_at = thread.first_response_at

    second = await inbox_sla.stamp_first_response(session, thread)
    assert second is False
    assert thread.first_response_at == first_at


async def test_first_response_not_stamped_without_an_inbound(client, session):
    token = await register_and_login(client, "p26-first-no-inbound@example.com")
    org = await create_org(client, token, "P26 First No Inbound")
    org_id = uuid.UUID(org["id"])

    number, inbox = await _make_number(session, org_id, "+12145550101")
    thread = await _make_thread(
        session, org_id, number.e164, "+19725550112", created_at=datetime.now(timezone.utc)
    )

    stamped = await inbox_sla.stamp_first_response(session, thread)
    assert stamped is False
    assert thread.first_response_at is None


async def test_breach_marks_and_notifies_leads_once(client, session):
    token = await register_and_login(client, "p26-breach-lead@example.com")
    org = await create_org(client, token, "P26 Breach Lead")
    org_id = uuid.UUID(org["id"])

    number, inbox = await _make_number(session, org_id, "+12145550102")
    inbox.sla_first_response_minutes = 1
    await session.commit()

    lead_token, lead = await _register_user_with_role(
        client, session, org_id, "lead@example.com", ["inbox:read"]
    )
    assignee_token, assignee = await _register_user_with_role(
        client, session, org_id, "assignee@example.com", ["inbox:read"]
    )

    set_org_context(session, org_id)
    department = Department(
        id=uuid.uuid4(), org_id=org_id, name="Support", is_active=True
    )
    session.add(department)
    await session.flush()
    session.add(
        DepartmentMember(
            id=uuid.uuid4(),
            org_id=org_id,
            department_id=department.id,
            user_id=lead.id,
            is_lead=True,
        )
    )
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox.id,
            grantee_type="department",
            grantee_id=department.id,
            role="member",
        )
    )
    await session.commit()

    now = datetime.now(timezone.utc)
    thread = await _make_thread(
        session,
        org_id,
        number.e164,
        "+19725551113",
        assigned_user_id=assignee.id,
        created_at=now - timedelta(minutes=10),
    )
    await _make_message(
        session,
        org_id,
        thread,
        direction="inbound",
        body="question",
        created_at=now - timedelta(minutes=5),
    )

    result = await inbox_sla.sla_tick(session)
    assert result["breached"] >= 1
    assert result["notifications"] >= 2

    set_org_context(session, org_id)
    await session.refresh(thread)
    assert thread.sla_breached_at is not None
    first_sla = _aware(thread.sla_breached_at)

    notifications = list(
        (
            await session.execute(
                sa.select(Notification).where(Notification.kind == "overdue")
            )
        )
        .scalars()
        .all()
    )
    assert len(notifications) == 2
    by_user = {n.user_id for n in notifications}
    assert lead.id in by_user
    assert assignee.id in by_user
    assert sum(1 for n in notifications if n.user_id == lead.id) == 1
    assert sum(1 for n in notifications if n.user_id == assignee.id) == 1

    result_again = await inbox_sla.sla_tick(session)
    assert result_again["breached"] == 0
    assert result_again["notifications"] == 0

    notifications_after = list(
        (
            await session.execute(
                sa.select(Notification).where(Notification.kind == "overdue")
            )
        )
        .scalars()
        .all()
    )
    assert len(notifications_after) == 2

    set_org_context(session, org_id)
    await session.refresh(thread)
    assert _aware(thread.sla_breached_at) == first_sla


async def test_no_targets_means_no_breach(client, session):
    token = await register_and_login(client, "p26-no-targets@example.com")
    org = await create_org(client, token, "P26 No Targets")
    org_id = uuid.UUID(org["id"])

    number, inbox = await _make_number(session, org_id, "+12145550103")
    thread = await _make_thread(
        session,
        org_id,
        number.e164,
        "+19725550114",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org_id,
        thread,
        direction="inbound",
        body="hello",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    result = await inbox_sla.sla_tick(session)
    assert result["breached"] == 0
    assert result["notifications"] == 0

    set_org_context(session, org_id)
    await session.refresh(thread)
    assert thread.sla_breached_at is None

    overdue_notifications = list(
        (
            await session.execute(
                sa.select(Notification).where(Notification.kind == "overdue")
            )
        )
        .scalars()
        .all()
    )
    assert overdue_notifications == []


async def test_resolution_rule_breaches_only_while_open(client, session):
    token = await register_and_login(client, "p26-resolution@example.com")
    org = await create_org(client, token, "P26 Resolution")
    org_id = uuid.UUID(org["id"])

    number, inbox = await _make_number(session, org_id, "+12145550104")
    inbox.sla_resolution_minutes = 1
    await session.commit()

    now = datetime.now(timezone.utc)
    thread_open = await _make_thread(
        session,
        org_id,
        number.e164,
        "+19725550115",
        status="open",
        created_at=now - timedelta(minutes=5),
    )
    thread_closed = await _make_thread(
        session,
        org_id,
        number.e164,
        "+19725550116",
        status="open",
        created_at=now - timedelta(minutes=5),
    )

    for thread in (thread_open, thread_closed):
        set_org_context(session, org_id)
        thread.first_response_at = now - timedelta(minutes=6)
        await session.commit()

    set_org_context(session, org_id)
    thread_closed.status = "closed"
    await session.commit()

    await _make_message(
        session,
        org_id,
        thread_open,
        direction="inbound",
        body="hi",
        created_at=now - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org_id,
        thread_closed,
        direction="inbound",
        body="hi",
        created_at=now - timedelta(minutes=5),
    )

    result = await inbox_sla.sla_tick(session)
    assert result["breached"] >= 1

    set_org_context(session, org_id)
    await session.refresh(thread_open)
    await session.refresh(thread_closed)
    assert thread_open.sla_breached_at is not None
    assert thread_closed.sla_breached_at is None


async def test_overdue_filter_lists_breached_open_threads_only(client, session):
    token = await register_and_login(client, "p26-overdue@example.com")
    org = await create_org(client, token, "P26 Overdue")
    org_id = uuid.UUID(org["id"])

    user_token, user = await _register_user_with_role(
        client, session, org_id, "viewer@example.com", ["inbox:read", "inboxes:admin"]
    )

    number, inbox = await _make_number(session, org_id, "+12145550105")
    inbox.sla_first_response_minutes = 1
    await session.commit()

    now = datetime.now(timezone.utc)
    breached_open = await _make_thread(
        session, org_id, number.e164, "+19725550117", created_at=now - timedelta(minutes=10)
    )
    closed = await _make_thread(
        session, org_id, number.e164, "+19725550118", created_at=now - timedelta(minutes=10)
    )
    healthy = await _make_thread(
        session, org_id, number.e164, "+19725550119", created_at=now - timedelta(minutes=10)
    )

    await _make_message(
        session,
        org_id,
        breached_open,
        direction="inbound",
        body="old",
        created_at=now - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org_id,
        closed,
        direction="inbound",
        body="old",
        created_at=now - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org_id,
        healthy,
        direction="inbound",
        body="recent",
        created_at=now - timedelta(seconds=5),
    )

    await inbox_sla.sla_tick(session)

    set_org_context(session, org_id)
    closed.status = "closed"
    await session.commit()

    r = await client.get(
        "/api/v1/conversations",
        params={"filter": "overdue"},
        headers=auth_headers(user_token, org["id"]),
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    ids = {item["thread_id"] for item in items}
    assert str(breached_open.id) in ids
    assert str(closed.id) not in ids
    assert str(healthy.id) not in ids


async def test_sla_field_on_list_rows(client, session):
    token = await register_and_login(client, "p26-slafield@example.com")
    org = await create_org(client, token, "P26 SLA Field")
    org_id = uuid.UUID(org["id"])

    user_token, user = await _register_user_with_role(
        client, session, org_id, "viewer2@example.com", ["inbox:read", "inboxes:admin"]
    )

    target_number, target_inbox = await _make_number(session, org_id, "+12145550106")
    target_inbox.sla_first_response_minutes = 60
    await session.commit()

    no_target_number, no_target_inbox = await _make_number(session, org_id, "+12145550107")

    now = datetime.now(timezone.utc)
    target_thread = await _make_thread(
        session, org_id, target_number.e164, "+19725550121", created_at=now - timedelta(minutes=5)
    )
    no_target_thread = await _make_thread(
        session,
        org_id,
        no_target_number.e164,
        "+19725550122",
        created_at=now - timedelta(minutes=5),
    )

    await _make_message(
        session,
        org_id,
        target_thread,
        direction="inbound",
        body="hi",
        created_at=now - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org_id,
        no_target_thread,
        direction="inbound",
        body="hi",
        created_at=now - timedelta(minutes=5),
    )

    r = await client.get(
        "/api/v1/conversations", headers=auth_headers(user_token, org["id"])
    )
    assert r.status_code == 200, r.text
    items = {item["thread_id"]: item for item in r.json()["items"]}

    assert str(target_thread.id) in items
    assert str(no_target_thread.id) in items
    assert items[str(target_thread.id)]["sla"] is not None
    assert items[str(target_thread.id)]["sla"]["due_at"] is not None
    assert items[str(no_target_thread.id)]["sla"] is None


async def test_sweeper_reopens_due_snooze(client, session):
    token = await register_and_login(client, "p26-snooze@example.com")
    org = await create_org(client, token, "P26 Snooze")
    org_id = uuid.UUID(org["id"])

    number, inbox = await _make_number(session, org_id, "+12145550108")
    now = datetime.now(timezone.utc)
    due_thread = await _make_thread(
        session,
        org_id,
        number.e164,
        "+19725550123",
        snoozed_until=now - timedelta(minutes=1),
    )
    future_thread = await _make_thread(
        session,
        org_id,
        number.e164,
        "+19725550124",
        snoozed_until=now + timedelta(hours=1),
    )

    reopened = await inbox_sla.reopen_due_snoozes(session, now=now)
    assert reopened >= 1

    set_org_context(session, org_id)
    await session.refresh(due_thread)
    await session.refresh(future_thread)
    assert due_thread.snoozed_until is None
    assert due_thread.status == "open"
    assert future_thread.snoozed_until is not None


async def test_patch_inbox_sla_targets(client, session):
    token = await register_and_login(client, "p26-patch-owner@example.com")
    org = await create_org(client, token, "P26 Patch")
    org_id = uuid.UUID(org["id"])

    admin_token, admin = await _register_user_with_role(
        client, session, org_id, "admin@example.com", ["inboxes:admin", "inbox:read"]
    )
    member_token, member = await _register_user_with_role(
        client, session, org_id, "member@example.com", ["inbox:read"]
    )

    number, inbox = await _make_number(session, org_id, "+12145550109")
    admin_headers = auth_headers(admin_token, org["id"])

    r = await client.patch(
        f"/api/v1/inboxes/{inbox.id}",
        json={
            "sla_first_response_minutes": 60,
            "sla_resolution_minutes": 120,
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sla_first_response_minutes"] == 60
    assert body["sla_resolution_minutes"] == 120

    r = await client.patch(
        f"/api/v1/inboxes/{inbox.id}",
        json={"sla_first_response_minutes": 0},
        headers=admin_headers,
    )
    assert r.status_code == 422

    r = await client.patch(
        f"/api/v1/inboxes/{inbox.id}",
        json={"sla_first_response_minutes": 44641},
        headers=admin_headers,
    )
    assert r.status_code == 422

    r = await client.patch(
        f"/api/v1/inboxes/{inbox.id}",
        json={"sla_first_response_minutes": 10},
        headers=auth_headers(member_token, org["id"]),
    )
    assert r.status_code == 403

    r = await client.patch(
        f"/api/v1/inboxes/{inbox.id}",
        json={
            "clear_sla_first_response": True,
            "clear_sla_resolution": True,
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sla_first_response_minutes"] is None
    assert body["sla_resolution_minutes"] is None


async def test_sla_tick_is_tenant_isolated(client, session):
    token1 = await register_and_login(client, "p26-tenant1@example.com")
    org1 = await create_org(client, token1, "P26 Tenant One")
    org1_id = uuid.UUID(org1["id"])

    token2 = await register_and_login(client, "p26-tenant2@example.com")
    org2 = await create_org(client, token2, "P26 Tenant Two")
    org2_id = uuid.UUID(org2["id"])

    _token1, user1 = await _register_user_with_role(
        client, session, org1_id, "user-one@example.com", ["inbox:read"]
    )
    _token2, user2 = await _register_user_with_role(
        client, session, org2_id, "user-two@example.com", ["inbox:read"]
    )

    number1, inbox1 = await _make_number(session, org1_id, "+12145550110")
    inbox1.sla_first_response_minutes = 1
    await session.commit()

    number2, inbox2 = await _make_number(session, org2_id, "+12145550111")
    inbox2.sla_first_response_minutes = 1
    await session.commit()

    now = datetime.now(timezone.utc)
    thread1 = await _make_thread(
        session,
        org1_id,
        number1.e164,
        "+19725550125",
        assigned_user_id=user1.id,
        created_at=now - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org1_id,
        thread1,
        direction="inbound",
        body="hello",
        created_at=now - timedelta(minutes=5),
    )

    thread2 = await _make_thread(
        session,
        org2_id,
        number2.e164,
        "+19725550126",
        assigned_user_id=user2.id,
        created_at=now - timedelta(minutes=5),
    )
    await _make_message(
        session,
        org2_id,
        thread2,
        direction="inbound",
        body="hello",
        created_at=now - timedelta(minutes=5),
    )

    result = await inbox_sla.sla_tick(session)
    assert result["orgs"] == 2
    assert result["breached"] == 2

    set_org_context(session, org1_id)
    await session.refresh(thread1)
    assert thread1.sla_breached_at is not None

    set_org_context(session, org2_id)
    await session.refresh(thread2)
    assert thread2.sla_breached_at is not None

    set_org_context(session, org1_id)
    notifs1 = list(
        (
            await session.execute(
                sa.select(Notification).where(Notification.kind == "overdue")
            )
        )
        .scalars()
        .all()
    )
    assert notifs1
    assert all(n.user_id == user1.id for n in notifs1)

    set_org_context(session, org2_id)
    notifs2 = list(
        (
            await session.execute(
                sa.select(Notification).where(Notification.kind == "overdue")
            )
        )
        .scalars()
        .all()
    )
    assert notifs2
    assert all(n.user_id == user2.id for n in notifs2)
