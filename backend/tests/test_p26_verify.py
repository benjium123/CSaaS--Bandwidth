"""P26 independent verification probes (Opus verifier).

Written against the integrated working tree, not derived from the drafter's tests.
Each test here corresponds to one numbered probe in the P26 verification brief.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Department,
    DepartmentMember,
    Inbox,
    InboxGrant,
    Message,
    MessageThread,
    Notification,
    OrgMembership,
    OrgNumber,
    Role,
    ThreadNote,
)
from app.repositories import users as users_repo
from tests.conftest import auth_headers, create_org, register_and_login

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
async def _make_number(session, org_id, e164) -> tuple[OrgNumber, Inbox]:
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


async def _make_thread(session, org_id, our, contact, **kw) -> MessageThread:
    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our,
        contact_e164=contact,
        last_message_at=kw.get("last_message_at", datetime.now(timezone.utc)),
        status=kw.get("status", "open"),
        ai_state="off",
        created_at=kw.get("created_at", datetime.now(timezone.utc)),
    )
    session.add(thread)
    await session.commit()
    return thread


async def _make_message(session, org_id, thread, *, direction, body, created_at):
    set_org_context(session, org_id)
    msg = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread.id,
        direction=direction,
        status="delivered" if direction == "outbound" else "received",
        from_e164=thread.our_e164 if direction == "outbound" else thread.contact_e164,
        to_e164=thread.contact_e164 if direction == "outbound" else thread.our_e164,
        body=body,
        media=[],
        carrier="bandwidth",
        created_at=created_at,
    )
    session.add(msg)
    await session.commit()
    return msg


async def _user_with_role(client, session, org_id, email, permissions, full_name=None):
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    assert user is not None
    set_org_context(session, org_id)
    if full_name:
        user.full_name = full_name
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"r-{uuid.uuid4().hex[:8]}",
        permissions=permissions,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token, user


async def _grant(session, org_id, inbox_id, user_id, role="member"):
    set_org_context(session, org_id)
    session.add(
        InboxGrant(
            id=uuid.uuid4(),
            org_id=org_id,
            inbox_id=inbox_id,
            grantee_type="user",
            grantee_id=user_id,
            role=role,
        )
    )
    await session.commit()


# ==================================================================================
# Probe 1 - a private note NEVER reaches the carrier
# ==================================================================================
async def test_probe1_note_never_reaches_the_carrier(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    token = await register_and_login(client, "v1@example.com")
    org = await create_org(client, token, "V1")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    A, C = "+12145551000", "+19725551000"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)
    await _make_message(
        session, org_id, thread, direction="inbound", body="hi",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    before_sends = len(fake.sent)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "internal only: do not tell the customer", "mention_user_ids": []},
        headers=h,
    )
    assert r.status_code == 201, r.text

    # zero outbound sends
    assert len(fake.sent) == before_sends == 0

    set_org_context(session, org_id)
    # no outbound Message row created by the note
    outbound = (
        await session.execute(
            sa.select(sa.func.count(Message.id)).where(
                Message.thread_id == thread.id, Message.direction == "outbound"
            )
        )
    ).scalar_one()
    assert outbound == 0
    # the note body never appears in ANY message row
    bodies = (
        await session.execute(sa.select(Message.body).where(Message.thread_id == thread.id))
    ).scalars().all()
    assert all("internal only" not in (b or "") for b in bodies)
    notes = (
        await session.execute(
            sa.select(sa.func.count(ThreadNote.id)).where(ThreadNote.thread_id == thread.id)
        )
    ).scalar_one()
    assert notes == 1

    # and the note does NOT stamp the SLA first-response clock
    set_org_context(session, org_id)
    fresh = await session.get(MessageThread, thread.id)
    await session.refresh(fresh)
    assert fresh.first_response_at is None


# ==================================================================================
# Probe 2 - grant boundary: no grant -> 404, viewer -> 403, member -> 200, cross-org -> 404
# ==================================================================================
async def test_probe2_grant_boundary_notes_and_snooze(client, session):
    owner = await register_and_login(client, "v2owner@example.com")
    org = await create_org(client, owner, "V2")
    org_id = uuid.UUID(org["id"])

    A, C = "+12145552000", "+19725552000"
    _num, inbox = await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    perms = ["inbox:read", "inbox:send", "inbox:manage"]
    nogrant_token, nogrant = await _user_with_role(
        client, session, org_id, "v2none@example.com", perms
    )
    viewer_token, viewer = await _user_with_role(
        client, session, org_id, "v2viewer@example.com", perms
    )
    member_token, member = await _user_with_role(
        client, session, org_id, "v2member@example.com", perms
    )
    await _grant(session, org_id, inbox.id, viewer.id, role="viewer")
    await _grant(session, org_id, inbox.id, member.id, role="member")

    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    notes_url = f"/api/v1/conversations/{thread.id}/notes"
    snooze_url = f"/api/v1/conversations/{thread.id}/snooze"

    # no grant -> 404 on all three (existence must not leak)
    hn = auth_headers(nogrant_token, org["id"])
    assert (await client.get(notes_url, headers=hn)).status_code == 404
    assert (
        await client.post(notes_url, json={"body": "x"}, headers=hn)
    ).status_code == 404
    assert (
        await client.post(snooze_url, json={"until": until}, headers=hn)
    ).status_code == 404

    # viewer -> 403 (Fable: notes/snooze need member-level access)
    hv = auth_headers(viewer_token, org["id"])
    assert (await client.get(notes_url, headers=hv)).status_code == 403
    assert (
        await client.post(notes_url, json={"body": "x"}, headers=hv)
    ).status_code == 403
    assert (
        await client.post(snooze_url, json={"until": until}, headers=hv)
    ).status_code == 403
    assert (await client.delete(snooze_url, headers=hv)).status_code == 403

    # member -> 200/201
    hm = auth_headers(member_token, org["id"])
    assert (await client.get(notes_url, headers=hm)).status_code == 200
    assert (
        await client.post(notes_url, json={"body": "member note"}, headers=hm)
    ).status_code == 201
    assert (
        await client.post(snooze_url, json={"until": until}, headers=hm)
    ).status_code == 200
    assert (await client.delete(snooze_url, headers=hm)).status_code == 200


async def test_probe2_cross_org_is_404(client, session):
    owner_a = await register_and_login(client, "v2a@example.com")
    org_a = await create_org(client, owner_a, "V2 A")
    org_a_id = uuid.UUID(org_a["id"])
    await _make_number(session, org_a_id, "+12145552100")
    thread_a = await _make_thread(session, org_a_id, "+12145552100", "+19725552100")

    owner_b = await register_and_login(client, "v2b@example.com")
    org_b = await create_org(client, owner_b, "V2 B")
    hb = auth_headers(owner_b, org_b["id"])

    # org B's owner is an admin IN ITS OWN ORG; org A's thread must still be a 404.
    assert (
        await client.get(f"/api/v1/conversations/{thread_a.id}/notes", headers=hb)
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/conversations/{thread_a.id}/notes", json={"body": "leak"}, headers=hb
        )
    ).status_code == 404
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert (
        await client.post(
            f"/api/v1/conversations/{thread_a.id}/snooze", json={"until": until}, headers=hb
        )
    ).status_code == 404

    set_org_context(session, org_a_id)
    assert (
        await session.execute(
            sa.select(sa.func.count(ThreadNote.id)).where(ThreadNote.thread_id == thread_a.id)
        )
    ).scalar_one() == 0


# ==================================================================================
# Probe 3 - mentions
# ==================================================================================
async def test_probe3_mentions(client, session):
    owner = await register_and_login(client, "v3owner@example.com")
    org = await create_org(client, owner, "V3")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner, org["id"])

    A, C = "+12145553000", "+19725553000"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    _t, sammy = await _user_with_role(
        client, session, org_id, "sammy@example.com", ["inbox:read"], full_name="Sammy Jones"
    )
    _t, dana = await _user_with_role(
        client, session, org_id, "dana@example.com", ["inbox:read"], full_name="Dana Reed"
    )
    # a non-member of THIS org - mentioning their name must be ignored
    outsider_token = await register_and_login(client, "outsider@example.com")
    other_org = await create_org(client, outsider_token, "V3 Other")
    set_org_context(session, uuid.UUID(other_org["id"]))
    outsider = await users_repo.get_by_email(session, "outsider@example.com")
    outsider.full_name = "Zed Outsider"
    await session.commit()

    # 3a: "@Sam" must NOT fire for "@Sammy Jones" - whole-token match only
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "ping @Sam about this"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["mentions"] == []

    set_org_context(session, org_id)
    assert (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(Notification.user_id == sammy.id)
        )
    ).scalar_one() == 0

    # 3b: the real token fires, exactly one notification per mentioned user
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "@Sammy Jones and @Dana Reed please look"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    note_id = r.json()["id"]
    mentioned = {m["user_id"] for m in r.json()["mentions"]}
    assert mentioned == {str(sammy.id), str(dana.id)}

    set_org_context(session, org_id)
    for user in (sammy, dana):
        rows = (
            await session.execute(
                sa.select(Notification).where(
                    Notification.user_id == user.id, Notification.kind == "mention"
                )
            )
        ).scalars().all()
        assert len(rows) == 1, f"{user.email}: {len(rows)} mention rows"
        assert rows[0].dedupe_key == f"mention:{note_id}:{user.id}"
        assert rows[0].thread_id == thread.id

    # 3c: dedupe - replaying the same note id through the producer creates nothing new
    from app.services import notifications as notifications_svc

    set_org_context(session, org_id)
    created = await notifications_svc.notify_mention(
        session,
        org_id,
        thread_id=thread.id,
        note_id=uuid.UUID(note_id),
        author_name="Owner",
        user_ids={sammy.id, dana.id},
    )
    await session.commit()
    assert created == 0
    for user in (sammy, dana):
        assert (
            await session.execute(
                sa.select(sa.func.count(Notification.id)).where(
                    Notification.user_id == user.id, Notification.kind == "mention"
                )
            )
        ).scalar_one() == 1

    # 3d: a mention of a non-member of this org is ignored entirely
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "cc @Zed Outsider"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["mentions"] == []
    set_org_context(session, org_id)
    assert (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(Notification.user_id == outsider.id)
        )
    ).scalar_one() == 0

    # 3e: an explicit mention_user_ids naming a non-member is rejected, not silently kept
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/notes",
        json={"body": "hello", "mention_user_ids": [str(outsider.id)]},
        headers=h,
    )
    assert r.status_code == 422, r.text


# ==================================================================================
# Probe 4 - WS gating: notification.created reaches only its own recipient
# ==================================================================================
async def test_probe4_ws_gate_admin_never_sees_another_users_notification():
    from app.api.routes.softphone import _event_visible
    from app.services.inbox_access import InboxAccess

    org_id = uuid.uuid4()
    user_a = uuid.uuid4()  # admin
    user_b = uuid.uuid4()

    admin_access = InboxAccess(
        is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset()
    )
    member_access = InboxAccess(
        is_admin=False, member_e164s=frozenset({"+12145554000"}), viewer_e164s=frozenset()
    )

    event_for_b = {
        "type": "notification.created",
        "user_id": str(user_b),
        "kind": "mention",
        "thread_id": str(uuid.uuid4()),
        "our_e164": "+12145554000",
        "contact_e164": "+19725554000",
        "body": "x mentioned you",
    }

    # the whole point: admin A must NOT receive B's bell row
    assert await _event_visible(event_for_b, admin_access, org_id, user_a) is False
    # B does
    assert await _event_visible(event_for_b, member_access, org_id, user_b) is True
    # an admin still receives their OWN
    own = dict(event_for_b, user_id=str(user_a))
    assert await _event_visible(own, admin_access, org_id, user_a) is True
    # fail-closed: no recipient reaches nobody, admin included
    headless = dict(event_for_b)
    headless.pop("user_id")
    assert await _event_visible(headless, admin_access, org_id, user_a) is False
    assert await _event_visible(headless, member_access, org_id, user_b) is False


async def test_probe4_notification_event_carries_the_pair(client, session):
    """The events path: the published payload carries our_e164/contact_e164 (Fable)."""
    from app.services import notifications as notifications_svc

    owner = await register_and_login(client, "v4@example.com")
    org = await create_org(client, owner, "V4")
    org_id = uuid.UUID(org["id"])
    A, C = "+12145554100", "+19725554100"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)
    _t, target = await _user_with_role(
        client, session, org_id, "v4target@example.com", ["inbox:read"], full_name="Tara Target"
    )

    published: list[tuple] = []

    class _Bus:
        def publish(self, org, event):
            published.append((org, event))

    set_org_context(session, org_id)
    await notifications_svc.notify_mention(
        session,
        org_id,
        thread_id=thread.id,
        note_id=uuid.uuid4(),
        author_name="Owner",
        user_ids={target.id},
        bus=_Bus(),
    )
    await session.commit()

    assert len(published) == 1
    _org, event = published[0]
    assert event["type"] == "notification.created"
    assert event["user_id"] == str(target.id)
    assert event["our_e164"] == A
    assert event["contact_e164"] == C
    assert event["thread_id"] == str(thread.id)

    # and that published event is invisible to a DIFFERENT user's socket
    from app.api.routes.softphone import _event_visible
    from app.services.inbox_access import InboxAccess

    assert (
        await _event_visible(
            event,
            InboxAccess(is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset()),
            org_id,
            uuid.uuid4(),
        )
        is False
    )


# ==================================================================================
# Probe 5 - SLA
# ==================================================================================
async def test_probe5_first_response_stamped_once_and_not_by_bulk_or_ai(
    app_with_carrier, session
):
    client, _fake, _app = app_with_carrier
    from app.services import messaging as messaging_svc
    from app.services.messaging import AI_SEND_KEY, BULK_SEND_KEY

    owner = await register_and_login(client, "v5a@example.com")
    org = await create_org(client, owner, "V5A")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner, org["id"])

    A, C = "+12145555000", "+19725555000"
    r = await client.post("/api/v1/numbers", json={"e164": A}, headers=h)
    assert r.status_code == 201, r.text

    # inbound first, so there is something to respond to
    set_org_context(session, org_id)
    thread = await _make_thread(session, org_id, A, C)
    await _make_message(
        session, org_id, thread, direction="inbound", body="hello?",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )

    # An AI send and a bulk/campaign send must NOT stamp the clock. Driven through the
    # real service with the same session flags the AI daemon and campaign scheduler set.
    from app.providers.base import SendResult
    from tests.conftest import FakeCarrier

    def _carrier(tag: str) -> FakeCarrier:
        # A distinct provider id per send: every FakeCarrier reuses one default id and
        # they would collide on uq_messages_provider_id.
        return FakeCarrier(
            default_result=SendResult("accepted", f"p26-verify-{tag}-{uuid.uuid4().hex}", None)
        )

    for flag in (AI_SEND_KEY, BULK_SEND_KEY):
        set_org_context(session, org_id)
        session.info[flag] = True
        try:
            await messaging_svc.send_message(
                session,
                org_id,
                _carrier(flag),
                to_e164=C,
                from_e164=A,
                body=f"automated send via {flag}",
            )
        finally:
            session.info.pop(flag, None)
        set_org_context(session, org_id)
        auto = await session.get(MessageThread, thread.id)
        await session.refresh(auto)
        assert auto.first_response_at is None, f"{flag} must not stamp the SLA clock"

    # a compliance auto-reply (exemption) must not stamp either
    set_org_context(session, org_id)
    await messaging_svc.send_message(
        session, org_id, _carrier("exempt"), to_e164=C, from_e164=A,
        body="You are unsubscribed.", exemption="stop_confirmation",
    )
    set_org_context(session, org_id)
    auto = await session.get(MessageThread, thread.id)
    await session.refresh(auto)
    assert auto.first_response_at is None

    # human reply through the API stamps once
    r = await client.post(
        "/api/v1/messages", json={"from": A, "to": C, "body": "a human reply"}, headers=h
    )
    assert r.status_code in (201, 202), r.text

    set_org_context(session, org_id)
    fresh = await session.get(MessageThread, thread.id)
    await session.refresh(fresh)
    first = fresh.first_response_at
    assert first is not None

    # a second human reply must not move it
    r = await client.post(
        "/api/v1/messages", json={"from": A, "to": C, "body": "second reply"}, headers=h
    )
    assert r.status_code in (201, 202), r.text
    set_org_context(session, org_id)
    fresh = await session.get(MessageThread, thread.id)
    await session.refresh(fresh)
    assert fresh.first_response_at == first

    # the guard flags exist and are exactly the three the takeover check uses
    assert AI_SEND_KEY and BULK_SEND_KEY


async def test_probe5_ai_and_bulk_sends_do_not_stamp(client, session):
    """The stamp helper is skipped for AI / bulk / exempt sends (direct unit probe)."""
    import inspect

    from app.services import messaging as messaging_svc

    src = inspect.getsource(messaging_svc.send_message)
    hook = src[src.index("P26: the first HUMAN reply") :]
    hook = hook[: hook.index("stamp_first_response(session, thread)")]
    assert "exemption is None" in hook
    assert "AI_SEND_KEY" in hook
    assert "BULK_SEND_KEY" in hook

    # and stamp_first_response is a no-op on a thread with no inbound (cold outreach)
    from app.services import inbox_sla as inbox_sla_svc

    owner = await register_and_login(client, "v5b@example.com")
    org = await create_org(client, owner, "V5B")
    org_id = uuid.UUID(org["id"])
    A, C = "+12145555100", "+19725555100"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)
    set_org_context(session, org_id)
    assert await inbox_sla_svc.stamp_first_response(session, thread) is False
    assert thread.first_response_at is None


async def test_probe5_breach_marks_once_notifies_leads_and_assignee_once(client, session):
    from app.services import inbox_sla as inbox_sla_svc

    owner = await register_and_login(client, "v5c@example.com")
    org = await create_org(client, owner, "V5C")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner, org["id"])

    A, C = "+12145555200", "+19725555200"
    _num, inbox = await _make_number(session, org_id, A)

    _t, lead = await _user_with_role(
        client, session, org_id, "v5lead@example.com", ["inbox:read"], full_name="Lee Lead"
    )
    _t, assignee = await _user_with_role(
        client, session, org_id, "v5assignee@example.com", ["inbox:read", "inbox:send"],
        full_name="Ann Assignee",
    )
    set_org_context(session, org_id)
    dept = Department(id=uuid.uuid4(), org_id=org_id, name="Support", is_active=True)
    session.add(dept)
    await session.flush()
    session.add(
        DepartmentMember(
            id=uuid.uuid4(), org_id=org_id, department_id=dept.id,
            user_id=lead.id, is_lead=True,
        )
    )
    session.add(
        InboxGrant(
            id=uuid.uuid4(), org_id=org_id, inbox_id=inbox.id,
            grantee_type="department", grantee_id=dept.id, role="member",
        )
    )
    inbox.sla_first_response_minutes = 15
    await session.commit()

    # a thread with an inbound 2h ago and no reply -> breached
    thread = await _make_thread(session, org_id, A, C)
    await _make_message(
        session, org_id, thread, direction="inbound", body="urgent",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    set_org_context(session, org_id)
    thread.assigned_user_id = assignee.id
    await session.commit()

    # a CLOSED thread on the same inbox must never breach
    closed = await _make_thread(session, org_id, A, "+19725555299", status="closed")
    await _make_message(
        session, org_id, closed, direction="inbound", body="old",
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )

    counts = await inbox_sla_svc.sla_tick(session)
    assert counts["breached"] == 1, counts

    set_org_context(session, org_id)
    fresh = await session.get(MessageThread, thread.id)
    await session.refresh(fresh)
    assert fresh.sla_breached_at is not None
    first_mark = fresh.sla_breached_at

    fresh_closed = await session.get(MessageThread, closed.id)
    await session.refresh(fresh_closed)
    assert fresh_closed.sla_breached_at is None

    for user in (lead, assignee):
        rows = (
            await session.execute(
                sa.select(Notification).where(
                    Notification.user_id == user.id, Notification.kind == "overdue"
                )
            )
        ).scalars().all()
        assert len(rows) == 1, f"{user.email}: {len(rows)}"
        assert rows[0].dedupe_key == f"overdue:{thread.id}:first"

    # re-running the sweeper marks and notifies nothing further
    counts2 = await inbox_sla_svc.sla_tick(session)
    assert counts2["breached"] == 0
    assert counts2["notifications"] == 0
    set_org_context(session, org_id)
    fresh = await session.get(MessageThread, thread.id)
    await session.refresh(fresh)
    assert fresh.sla_breached_at == first_mark
    for user in (lead, assignee):
        assert (
            await session.execute(
                sa.select(sa.func.count(Notification.id)).where(
                    Notification.user_id == user.id, Notification.kind == "overdue"
                )
            )
        ).scalar_one() == 1

    # filter=overdue lists it
    r = await client.get("/api/v1/conversations?filter=overdue", headers=h)
    assert r.status_code == 200, r.text
    ids = {row["contact_e164"] for row in r.json()["items"]}
    assert C in ids
    assert "+19725555299" not in ids


async def test_probe5_sweeper_commits_per_row_not_once_at_the_end():
    """Read the code: a multi-org loop must not batch its commit at the end."""
    import inspect

    from app.services import inbox_sla as inbox_sla_svc
    from app.services import notifications as notifications_svc

    for fn in (
        inbox_sla_svc.sla_tick,
        inbox_sla_svc.reopen_due_snoozes,
        notifications_svc.missed_call_tick,
    ):
        src = inspect.getsource(fn)
        body = src[src.index("for org_id in org_ids") :]
        assert "await session.commit()" in body, fn.__name__
        # the commit sits INSIDE the per-row loop, i.e. indented deeper than the org loop
        commit_lines = [ln for ln in body.splitlines() if ln.strip() == "await session.commit()"]
        assert commit_lines, fn.__name__
        for line in commit_lines:
            indent = len(line) - len(line.lstrip())
            assert indent >= 16, f"{fn.__name__}: commit at indent {indent} is not per-row"
        # and each org is isolated by its own rollback on failure
        assert "await session.rollback()" in body, fn.__name__


# ==================================================================================
# Probe 6 - snooze
# ==================================================================================
async def test_probe6_snooze_leaves_open_appears_in_snoozed_and_reopens(client, session):
    from app.services import inbox_sla as inbox_sla_svc

    owner = await register_and_login(client, "v6@example.com")
    org = await create_org(client, owner, "V6")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner, org["id"])

    A, C = "+12145556000", "+19725556000"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)
    await _make_message(
        session, org_id, thread, direction="inbound", body="hi",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    def _contacts(payload):
        return {row["contact_e164"] for row in payload["items"]}

    r = await client.get("/api/v1/conversations?filter=open", headers=h)
    assert C in _contacts(r.json())

    until = datetime.now(timezone.utc) + timedelta(hours=3)
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": until.isoformat()},
        headers=h,
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/conversations?filter=open", headers=h)
    assert C not in _contacts(r.json()), "a snoozed thread must leave the open list"
    r = await client.get("/api/v1/conversations?filter=snoozed", headers=h)
    assert C in _contacts(r.json())

    # sweeper with a clock past the due time reopens it
    reopened = await inbox_sla_svc.reopen_due_snoozes(
        session, now=until + timedelta(minutes=1)
    )
    assert reopened >= 1
    set_org_context(session, org_id)
    fresh = await session.get(MessageThread, thread.id)
    await session.refresh(fresh)
    assert fresh.snoozed_until is None
    assert fresh.status == "open"

    r = await client.get("/api/v1/conversations?filter=open", headers=h)
    assert C in _contacts(r.json())
    r = await client.get("/api/v1/conversations?filter=snoozed", headers=h)
    assert C not in _contacts(r.json())

    # a past time is rejected
    r = await client.post(
        f"/api/v1/conversations/{thread.id}/snooze",
        json={"until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
        headers=h,
    )
    assert r.status_code == 422, r.text


# ==================================================================================
# Probe 7 - D25 regression
# ==================================================================================
async def test_probe7_d25_matched_flag_regression_exists():
    """The D25 regression must exist and its Postgres-only case must be marked pg_only."""
    from pathlib import Path

    import app

    root = Path(app.__file__).resolve().parents[1]
    path = root / "tests" / "test_p26_search_d25.py"
    assert path.exists(), "backend/tests/test_p26_search_d25.py is missing"
    text = path.read_text(encoding="utf-8")
    assert "matched" in text
    assert "pg_only" in text, "the stemmed-tsvector case must be explicitly pg_only"

    # the SQLite path still computes a matched flag rather than dropping the key
    import inspect

    from app.services import search as search_svc

    src = inspect.getsource(search_svc)
    assert '"matched": matched' in src
    assert "matched_ids is not None" in src


# ==================================================================================
# Probe 8 - templates ?q=
# ==================================================================================
async def test_probe8_templates_q_is_org_scoped_and_matches_name_or_body(client, session):
    owner_a = await register_and_login(client, "v8a@example.com")
    org_a = await create_org(client, owner_a, "V8 A")
    ha = auth_headers(owner_a, org_a["id"])
    owner_b = await register_and_login(client, "v8b@example.com")
    org_b = await create_org(client, owner_b, "V8 B")
    hb = auth_headers(owner_b, org_b["id"])

    def _items(resp):
        payload = resp.json()
        return payload["items"] if isinstance(payload, dict) else payload

    async def _mk(headers, name, body):
        r = await client.post(
            "/api/v1/templates", json={"name": name, "body": body}, headers=headers
        )
        assert r.status_code == 201, r.text
        return r.json()

    await _mk(ha, "Refund policy", "We refund within 30 days.")
    await _mk(ha, "Greeting", "Hello and welcome aboard.")
    await _mk(hb, "Refund policy B", "Org B refund text.")

    # match by NAME
    r = await client.get("/api/v1/templates?q=refund", headers=ha)
    assert r.status_code == 200, r.text
    names = [t["name"] for t in _items(r)]
    assert names == ["Refund policy"], names

    # match by BODY
    r = await client.get("/api/v1/templates?q=welcome", headers=ha)
    names = [t["name"] for t in _items(r)]
    assert names == ["Greeting"], names

    # org B never sees org A's, and vice versa
    r = await client.get("/api/v1/templates?q=refund", headers=hb)
    names = [t["name"] for t in _items(r)]
    assert names == ["Refund policy B"], names

    # a LIKE metacharacter is escaped, not smuggled
    r = await client.get("/api/v1/templates?q=%25", headers=ha)
    items = _items(r)
    assert items == [], items


# ==================================================================================
# Extra probe - cross-tenant safety of the missed-call recipient resolver
# ==================================================================================
async def test_probe_extra_recipients_for_number_is_org_scoped(client, session):
    """recipients_for_number's role sweep has no explicit org filter of its own.

    It leans entirely on the session tenant guard. Prove the guard actually covers a
    column-only select, or an admin in ANOTHER org would be told about this org's
    missed calls.
    """
    from app.services import notifications as notifications_svc

    owner_a = await register_and_login(client, "vx-a@example.com")
    org_a = await create_org(client, owner_a, "VX A")
    org_a_id = uuid.UUID(org_a["id"])
    A = "+12145557000"
    _num, inbox_a = await _make_number(session, org_a_id, A)

    _t, a_member = await _user_with_role(
        client, session, org_a_id, "vx-amember@example.com", ["inbox:read"]
    )
    await _grant(session, org_a_id, inbox_a.id, a_member.id, role="member")

    # a full admin in a DIFFERENT org
    owner_b = await register_and_login(client, "vx-b@example.com")
    org_b = await create_org(client, owner_b, "VX B")
    org_b_id = uuid.UUID(org_b["id"])
    set_org_context(session, org_b_id)
    b_owner = await users_repo.get_by_email(session, "vx-b@example.com")

    set_org_context(session, org_a_id)
    recipients = await notifications_svc.recipients_for_number(session, A)

    assert a_member.id in recipients
    assert b_owner.id not in recipients, "an admin in another org must never be a recipient"

    # and the number itself does not resolve from the wrong org's context
    set_org_context(session, org_b_id)
    assert await notifications_svc.recipients_for_number(session, A) == set()
