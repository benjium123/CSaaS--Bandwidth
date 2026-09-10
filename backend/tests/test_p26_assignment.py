"""P26: assigning a conversation puts one entry on the new owner's bell.

The producer lives in routes/inbox.py::patch_thread; the dedupe key is what keeps a
thread that is handed back and forth from ringing the same person twice.
"""

from __future__ import annotations

import uuid

import httpx
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Inbox,
    MessageThread,
    Notification,
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
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token, user


async def _assignment_rows(session, org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.kind == "assignment",
            )
        )
    ).scalar_one()


async def test_assigning_a_thread_notifies_the_new_owner_once(client, session):
    owner_token = await register_and_login(client, "p26as1@example.com")
    org = await create_org(client, owner_token, "P26 Assignment Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    _, mate = await _register_user_with_role(
        client, session, org_id, "p26as1-mate@example.com", ["inbox:read", "inbox:send"]
    )

    r = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(mate.id)},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert await _assignment_rows(session, org_id, mate.id) == 1

    # Handed away and handed back: the dedupe key keeps it at one bell entry.
    r = await client.patch(
        f"/api/v1/threads/{thread.id}", json={"clear_assignee": True}, headers=h
    )
    assert r.status_code == 200, r.text
    r = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(mate.id)},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert await _assignment_rows(session, org_id, mate.id) == 1


async def test_claiming_a_thread_never_notifies_yourself(client, session):
    owner_token = await register_and_login(client, "p26as2@example.com")
    org = await create_org(client, owner_token, "P26 Self Claim Org")
    org_id = uuid.UUID(org["id"])
    h = auth_headers(owner_token, org["id"])

    A = "+12145550100"
    C = "+19725550111"
    await _make_number(session, org_id, A)
    thread = await _make_thread(session, org_id, A, C)

    set_org_context(session, org_id)
    owner = await users_repo.get_by_email(session, "p26as2@example.com")
    assert owner is not None

    r = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(owner.id)},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert await _assignment_rows(session, org_id, owner.id) == 0
