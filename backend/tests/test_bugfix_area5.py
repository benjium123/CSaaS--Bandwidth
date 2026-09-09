"""Regression tests for BUGFIX_LEDGER_2026-09.md Area 5 (inbox / conversations /
contacts / access), items 5.1-5.18 and 5.20 (5.19 is P20, out of scope here).

Pattern for a scoped (non-admin) caller: register a second user, attach them to the
org via a Role holding EXACTLY the permissions the test needs (never "*" or
"inboxes:admin" - either makes resolve_access treat them as admin), then optionally
grant them P15 inbox access with a direct InboxGrant. This mirrors
tests/test_rbac.py::test_agent_denied_members_read and
tests/test_p15_inbox_access.py's grant setup.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    Company,
    Contact,
    ContactPhone,
    CustomFieldDef,
    Department,
    Inbox,
    InboxGrant,
    Message,
    MessageThread,
    OrgMembership,
    OrgNumber,
    Role,
    Tag,
)
from app.repositories import users as users_repo
from app.services import messaging as messaging_svc
from tests.conftest import (
    auth_headers,
    create_contact,
    create_org,
    create_tag,
    make_org_with_number,
    query_counter,  # noqa: F401 - fixture
    register_and_login,
)

OUR = "+12145550100"
OUR_B = "+12145550101"
THEIRS = "+19725550101"
THEIRS_B = "+19725550102"


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
async def _inbox_for(session, e164: str) -> Inbox:
    number = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == e164))
    ).scalar_one()
    return (
        await session.execute(sa.select(Inbox).where(Inbox.number_id == number.id))
    ).scalar_one()


async def _scoped_member(
    client,
    session,
    org_id: uuid.UUID,
    email: str,
    permissions: list[str],
    *,
    grant_e164: str | None = None,
    grant_role: str = "member",
) -> tuple[str, uuid.UUID]:
    """Register a second user, attach to the org via a custom non-admin Role holding
    EXACTLY `permissions`, and (optionally) grant P15 access to one number. Returns
    (token, user_id)."""
    token = await register_and_login(client, email)
    set_org_context(session, org_id)
    user = await users_repo.get_by_email(session, email)
    role = Role(
        id=uuid.uuid4(), org_id=org_id, name=f"scoped-{uuid.uuid4().hex[:8]}",
        permissions=permissions,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()

    if grant_e164 is not None:
        inbox = await _inbox_for(session, grant_e164)
        session.add(
            InboxGrant(
                id=uuid.uuid4(), org_id=org_id, inbox_id=inbox.id,
                grantee_type="user", grantee_id=user.id, role=grant_role,
            )
        )
        await session.commit()

    return token, user.id


async def _api_key(client, token: str, org_id, scopes: list[str]) -> str:
    r = await client.post(
        "/api/v1/api-keys",
        json={"name": "test key", "scopes": scopes},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


# ----------------------------------------------------------------------------------
# 5.1 + 5.20: GET /messages with no thread_id scopes by inbox access, id tiebreak
# ----------------------------------------------------------------------------------
async def test_5_1_list_messages_without_thread_id_scopes_by_inbox_access(
    app_with_carrier, session
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b51-owner@example.com", "Org B51", OUR)
    org_id = uuid.UUID(org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR_B}, headers=auth_headers(token, org_id)
    )
    assert r.status_code == 201, r.text

    set_org_context(session, org_id)
    thread_a = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    thread_b = await messaging_svc.upsert_thread(session, org_id, OUR_B, THEIRS_B)
    session.add_all(
        [
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_a.id, direction="inbound",
                status="received", from_e164=THEIRS, to_e164=OUR, body="visible",
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_b.id, direction="inbound",
                status="received", from_e164=THEIRS_B, to_e164=OUR_B, body="hidden",
            ),
        ]
    )
    await session.commit()

    # Scoped caller only ever granted OUR, not OUR_B.
    token_scoped, _ = await _scoped_member(
        client, session, org_id, "b51-agent@example.com", ["inbox:read"], grant_e164=OUR
    )

    r = await client.get(
        "/api/v1/messages", headers=auth_headers(token_scoped, org_id)
    )
    assert r.status_code == 200, r.text
    bodies = {m["body"] for m in r.json()}
    assert bodies == {"visible"}

    # The org owner (admin) still sees everything.
    r_admin = await client.get("/api/v1/messages", headers=auth_headers(token, org_id))
    assert {m["body"] for m in r_admin.json()} == {"visible", "hidden"}


async def test_5_20_list_messages_orders_with_id_tiebreak(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b520@example.com", "Org B520", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    same_ts = thread.created_at
    ids = sorted(uuid.uuid4() for _ in range(3))
    for mid in ids:
        session.add(
            Message(
                id=mid, org_id=org_id, thread_id=thread.id, direction="inbound",
                status="received", from_e164=THEIRS, to_e164=OUR, body=str(mid),
                created_at=same_ts,
            )
        )
    await session.commit()

    r = await client.get(
        f"/api/v1/messages?thread_id={thread.id}", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    returned_ids = [uuid.UUID(m["id"]) for m in r.json() if uuid.UUID(m["id"]) in ids]
    assert returned_ids == ids


# ----------------------------------------------------------------------------------
# 5.2: POST /softphone/token requires P15 use-access on the call's number
# ----------------------------------------------------------------------------------
async def test_5_2_softphone_token_denied_without_inbox_access(engine):
    """Duplicates the app_with_room_calls fixture shape from test_voice_plane.py -
    LiveKit must be configured for the route to reach the P15 gate at all."""
    import httpx

    from app.main import create_app
    from app.voice_plane.livekit_api import LiveKitApi
    from app.models.voice import Call
    from tests.conftest import make_settings
    from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

    settings = make_settings(
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="lk-key",
        livekit_api_secret="lk-secret-value-padded-to-32-bytes-plus",
        livekit_sip_outbound_trunk_id="trunk-out-1",
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    lk_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key="lk-key",
        api_secret="lk-secret-value-padded-to-32-bytes-plus", client=lk_client,
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        from app.db.session import get_sessionmaker

        token, org, _ = await make_org_with_number(client, "b52-owner@example.com", "Org B52", OUR)
        org_id = uuid.UUID(org["id"])

        async with get_sessionmaker()() as session:
            set_org_context(session, org_id)
            call = Call(
                id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
                our_e164=OUR, carrier="telnyx", status="ringing",
                extra={"via": "livekit", "room": None},
            )
            room = f"call-{call.id}"
            call.extra = {"via": "livekit", "room": room}
            session.add(call)
            await session.commit()

            token_scoped, _ = await _scoped_member(
                client, session, org_id, "b52-viewer@example.com", ["calls:place"],
                grant_e164=OUR, grant_role="viewer",
            )

        r = await client.post(
            "/api/v1/softphone/token", json={"room": room},
            headers=auth_headers(token_scoped, org_id),
        )
        assert r.status_code == 404, r.text

        r_owner = await client.post(
            "/api/v1/softphone/token", json={"room": room}, headers=auth_headers(token, org_id)
        )
        assert r_owner.status_code == 200, r_owner.text

    await lk_client.aclose()


# ----------------------------------------------------------------------------------
# 5.3: GET /search/transcripts scopes by inbox access
# ----------------------------------------------------------------------------------
async def test_5_3_search_transcripts_scoped_by_inbox_access(client, session):
    from app.models.agent import CallTranscriptSegment
    from app.models.voice import Call

    token = await register_and_login(client, "b53-owner@example.com")
    org = await create_org(client, token, "Org B53")
    org_id = uuid.UUID(org["id"])
    for e164 in (OUR, OUR_B):
        r = await client.post(
            "/api/v1/numbers", json={"e164": e164}, headers=auth_headers(token, org_id)
        )
        assert r.status_code == 201, r.text

    set_org_context(session, org_id)
    call_a = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
        our_e164=OUR, carrier="telnyx", status="completed",
    )
    call_b = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS_B,
        our_e164=OUR_B, carrier="telnyx", status="completed",
    )
    session.add_all([call_a, call_b])
    await session.flush()
    session.add_all(
        [
            CallTranscriptSegment(
                id=uuid.uuid4(), org_id=org_id, call_id=call_a.id, role="user",
                text="the secret codeword is banana", at_ms=0,
            ),
            CallTranscriptSegment(
                id=uuid.uuid4(), org_id=org_id, call_id=call_b.id, role="user",
                text="the secret codeword is banana", at_ms=0,
            ),
        ]
    )
    await session.commit()

    token_scoped, _ = await _scoped_member(
        client, session, org_id, "b53-agent@example.com", ["reports:read"], grant_e164=OUR
    )

    r = await client.get(
        "/api/v1/search/transcripts?q=banana", headers=auth_headers(token_scoped, org_id)
    )
    assert r.status_code == 200, r.text
    call_ids = {row["call_id"] for row in r.json()}
    assert call_ids == {str(call_a.id)}

    r_admin = await client.get(
        "/api/v1/search/transcripts?q=banana", headers=auth_headers(token, org_id)
    )
    assert {row["call_id"] for row in r_admin.json()} == {str(call_a.id), str(call_b.id)}


# ----------------------------------------------------------------------------------
# 5.4: POST /threads/{id}/read requires use (member), not just view (viewer)
# ----------------------------------------------------------------------------------
async def test_5_4_mark_read_requires_use_access(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b54-owner@example.com", "Org B54", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    await session.commit()

    token_viewer, _ = await _scoped_member(
        client, session, org_id, "b54-viewer@example.com", ["inbox:read"],
        grant_e164=OUR, grant_role="viewer",
    )

    r = await client.post(
        f"/api/v1/threads/{thread.id}/read", headers=auth_headers(token_viewer, org_id)
    )
    assert r.status_code == 403, r.text

    token_member, _ = await _scoped_member(
        client, session, org_id, "b54-member@example.com", ["inbox:read"],
        grant_e164=OUR, grant_role="member",
    )
    r_ok = await client.post(
        f"/api/v1/threads/{thread.id}/read", headers=auth_headers(token_member, org_id)
    )
    assert r_ok.status_code == 204, r_ok.text


# ----------------------------------------------------------------------------------
# 5.5: PUT /contacts/{id}/tags validates tag ids
# ----------------------------------------------------------------------------------
async def test_5_5_set_contact_tags_validates_tag_ids(client, session):
    token, org, _ = await make_org_with_number(client, "b55@example.com", "Org B55", OUR)
    contact = await create_contact(client, token, org["id"], "Tag Target", [THEIRS])
    h = auth_headers(token, org["id"])

    bogus = str(uuid.uuid4())
    r = await client.put(
        f"/api/v1/contacts/{contact['id']}/tags", json={"tag_ids": [bogus]}, headers=h
    )
    assert r.status_code == 422, r.text

    tag = await create_tag(client, token, org["id"], "VIP")
    r_ok = await client.put(
        f"/api/v1/contacts/{contact['id']}/tags", json={"tag_ids": [tag["id"]]}, headers=h
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["tag_ids"] == [tag["id"]]


# ----------------------------------------------------------------------------------
# 5.6: contact create/patch validates company_id
# ----------------------------------------------------------------------------------
async def test_5_6_contact_company_id_is_validated(client):
    token, org, _ = await make_org_with_number(client, "b56@example.com", "Org B56", OUR)
    h = auth_headers(token, org["id"])

    bogus = str(uuid.uuid4())
    r = await client.post(
        "/api/v1/contacts",
        json={"display_name": "No Company", "company_id": bogus, "phones": []},
        headers=h,
    )
    assert r.status_code == 422, r.text

    company = await client.post(
        "/api/v1/companies", json={"name": "Acme Land"}, headers=h
    )
    assert company.status_code == 201, company.text

    r_ok = await client.post(
        "/api/v1/contacts",
        json={"display_name": "Has Company", "company_id": company.json()["id"], "phones": []},
        headers=h,
    )
    assert r_ok.status_code == 201, r_ok.text

    contact = await create_contact(client, token, org["id"], "Patchee", [THEIRS])
    r_patch = await client.patch(
        f"/api/v1/contacts/{contact['id']}", json={"company_id": bogus}, headers=h
    )
    assert r_patch.status_code == 422, r_patch.text


# ----------------------------------------------------------------------------------
# 5.7: removing a phone unlinks its threads from the contact
# ----------------------------------------------------------------------------------
async def test_5_7_removing_a_phone_unlinks_its_threads(client, session):
    token, org, _ = await make_org_with_number(client, "b57@example.com", "Org B57", OUR)
    org_id = uuid.UUID(org["id"])
    contact = await create_contact(client, token, org["id"], "Old Number Owner", [THEIRS])
    contact_id = uuid.UUID(contact["id"])

    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    thread.contact_id = contact_id
    await session.commit()

    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"phones": []},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text

    await session.refresh(thread)
    assert thread.contact_id is None


# ----------------------------------------------------------------------------------
# 5.8: GET /contacts batch-loads phones (no N+1)
# ----------------------------------------------------------------------------------
async def test_5_8_list_contacts_query_count_independent_of_size(
    app_with_carrier, session, query_counter
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b58@example.com", "Org B58", OUR)
    h = auth_headers(token, org["id"])

    for i in range(3):
        await create_contact(
            client, token, org["id"], f"Small {i}",
            [f"+1972555{i:04d}", f"+1972666{i:04d}"],
        )
    query_counter.reset()
    r_small = await client.get("/api/v1/contacts?limit=3", headers=h)
    count_small = query_counter.count
    assert r_small.status_code == 200 and len(r_small.json()) == 3

    for i in range(3, 10):
        await create_contact(
            client, token, org["id"], f"Big {i}",
            [f"+1972555{i:04d}", f"+1972666{i:04d}"],
        )
    query_counter.reset()
    r_big = await client.get("/api/v1/contacts?limit=10", headers=h)
    count_big = query_counter.count
    assert r_big.status_code == 200 and len(r_big.json()) == 10
    for c in r_big.json():
        assert len(c["phones"]) == 2

    assert count_small == count_big, (
        f"query count grew with result size ({count_small} -> {count_big}): N+1"
    )


# ----------------------------------------------------------------------------------
# 5.9: LIKE wildcards are escaped (4 sites)
# ----------------------------------------------------------------------------------
async def test_5_9_contacts_search_escapes_like_wildcards(client, session):
    token, org, _ = await make_org_with_number(client, "b59a@example.com", "Org B59A", OUR)
    await create_contact(client, token, org["id"], "50% Off Realty", [THEIRS])
    await create_contact(client, token, org["id"], "50X Off Realty", [THEIRS_B])
    h = auth_headers(token, org["id"])

    # A literal "%" in the query must match only the literal contact, not act as a
    # wildcard that also matches "50X Off Realty".
    r = await client.get("/api/v1/contacts?q=50%25", headers=h)
    assert r.status_code == 200, r.text
    names = {c["display_name"] for c in r.json()}
    assert names == {"50% Off Realty"}


async def test_5_9_inbox_threads_q_escapes_like_wildcards(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b59b@example.com", "Org B59B", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    t1 = await messaging_svc.upsert_thread(session, org_id, OUR, "+19725550001")
    t1.last_message_at = t1.created_at
    t2 = await messaging_svc.upsert_thread(session, org_id, OUR, "+19725550002")
    t2.last_message_at = t2.created_at
    await session.commit()

    # "%" has no wildcard meaning here since neither contact_e164 nor any contact name
    # contains a literal percent sign - the escaped query must simply match nothing.
    r = await client.get(
        "/api/v1/inbox/threads?q=%25weird%25", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


async def test_5_9_search_transcripts_escapes_like_wildcards(client, session):
    from app.models.agent import CallTranscriptSegment
    from app.models.voice import Call

    token, org, _ = await make_org_with_number(client, "b59c@example.com", "Org B59C", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
        our_e164=OUR, carrier="telnyx", status="completed",
    )
    session.add(call)
    await session.flush()
    session.add(
        CallTranscriptSegment(
            id=uuid.uuid4(), org_id=org_id, call_id=call.id, role="user",
            text="save 50 percent today", at_ms=0,
        )
    )
    await session.commit()

    r = await client.get(
        "/api/v1/search/transcripts?q=50%25", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_5_9_conversations_q_escapes_like_wildcards(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b59d@example.com", "Org B59D", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    thread.last_message_at = thread.created_at
    await session.commit()

    r = await client.get(
        "/api/v1/conversations?filter=all&q=%25nomatch%25", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# ----------------------------------------------------------------------------------
# 5.10: inbox keyset pagination reaches NULL-last_message_at threads
# ----------------------------------------------------------------------------------
async def test_5_10_keyset_pagination_reaches_null_last_message_at_threads(
    app_with_carrier, session
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b510@example.com", "Org B510", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    set_org_context(session, org_id)

    # Two threads WITH a real last_message_at (newest first)...
    with_msg = []
    for i in range(2):
        t = await messaging_svc.upsert_thread(session, org_id, OUR, f"+1972555{i:04d}")
        t.last_message_at = t.created_at
        with_msg.append(t)
    # ...and one thread that never got a message (last_message_at stays NULL, e.g. a
    # call-only conversation whose thread was upserted by 5.11's read-pair route).
    null_thread = await messaging_svc.upsert_thread(session, org_id, OUR, "+19725559999")
    await session.commit()
    assert null_thread.last_message_at is None

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        url = "/api/v1/inbox/threads?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url, headers=h)).json()
        seen.extend(item["thread"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert str(null_thread.id) in seen, "NULL-last_message_at thread was never reached"
    assert len(seen) == len(set(seen)) == 3


# ----------------------------------------------------------------------------------
# 5.11: a call-only conversation can be marked read via /inbox/read-pair
# ----------------------------------------------------------------------------------
async def test_5_11_read_pair_upserts_and_marks_a_call_only_conversation_read(
    app_with_carrier, session
):
    from app.models.voice import Call

    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b511@example.com", "Org B511", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    existing = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR, MessageThread.contact_e164 == THEIRS
            )
        )
    ).scalar_one_or_none()
    assert existing is None, "no thread should exist yet for a call-only pair"

    # 0(a): read-pair now requires an actual Call (or Message) to exist for this pair
    # before it will upsert a thread - a call-only conversation's Call row IS that
    # precondition.
    session.add(
        Call(
            id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
            our_e164=OUR, carrier="telnyx", status="completed",
        )
    )
    await session.commit()

    r = await client.post(
        "/api/v1/inbox/read-pair", json={"our_e164": OUR, "contact_e164": THEIRS}, headers=h
    )
    assert r.status_code == 204, r.text

    thread = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR, MessageThread.contact_e164 == THEIRS
            )
        )
    ).scalar_one()
    assert thread.last_read_at is not None


async def test_5_11_read_pair_denied_without_inbox_access(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b511b@example.com", "Org B511B", OUR)
    org_id = uuid.UUID(org["id"])

    token_viewer, _ = await _scoped_member(
        client, session, org_id, "b511b-viewer@example.com", ["inbox:read"],
        grant_e164=OUR, grant_role="viewer",
    )
    r = await client.post(
        "/api/v1/inbox/read-pair",
        json={"our_e164": OUR, "contact_e164": THEIRS},
        headers=auth_headers(token_viewer, org_id),
    )
    assert r.status_code == 403, r.text


# ----------------------------------------------------------------------------------
# 5.12: assigned=me with an API-key caller (no human user) is rejected
# ----------------------------------------------------------------------------------
async def test_5_12_assigned_me_rejected_for_api_key_caller(client, session):
    token, org, _ = await make_org_with_number(client, "b512@example.com", "Org B512", OUR)
    key = await _api_key(client, token, org["id"], ["inbox:read"])

    r = await client.get(
        "/api/v1/inbox/threads?assigned=me",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 422, r.text


# ----------------------------------------------------------------------------------
# 5.13: template render requires contacts:read too
# ----------------------------------------------------------------------------------
async def test_5_13_render_template_requires_contacts_read(client):
    token, org, _ = await make_org_with_number(client, "b513@example.com", "Org B513", OUR)
    h = auth_headers(token, org["id"])
    contact = await create_contact(client, token, org["id"], "Render Target", [THEIRS])
    created = await client.post(
        "/api/v1/templates", json={"name": "Hi", "body": "Hi {{contact.first_name}}"}, headers=h
    )
    assert created.status_code == 201, created.text
    template_id = created.json()["id"]

    key = await _api_key(client, token, org["id"], ["templates:read"])
    r = await client.post(
        f"/api/v1/templates/{template_id}/render",
        json={"contact_id": contact["id"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403, r.text

    key_both = await _api_key(client, token, org["id"], ["templates:read", "contacts:read"])
    r_ok = await client.post(
        f"/api/v1/templates/{template_id}/render",
        json={"contact_id": contact["id"]},
        headers={"Authorization": f"Bearer {key_both}"},
    )
    assert r_ok.status_code == 200, r_ok.text


# ----------------------------------------------------------------------------------
# 5.14: claiming an already-claimed thread is a 409, not a silent steal
# ----------------------------------------------------------------------------------
async def test_5_14_claiming_an_already_claimed_thread_is_409(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b514-owner@example.com", "Org B514", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    await session.commit()

    # Owner claims for themself first.
    owner_user = await users_repo.get_by_email(session, "b514-owner@example.com")
    r1 = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(owner_user.id)},
        headers=auth_headers(token, org_id),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["assigned_user_id"] == str(owner_user.id)

    # A second (self-claiming) operator loses the race.
    token2, user2_id = await _scoped_member(
        client, session, org_id, "b514-agent@example.com", ["inbox:manage"],
        grant_e164=OUR, grant_role="member",
    )
    r2 = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(user2_id)},
        headers=auth_headers(token2, org_id),
    )
    assert r2.status_code == 409, r2.text

    # An explicit reassignment (not a self-claim) by the owner is unaffected.
    r3 = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(user2_id)},
        headers=auth_headers(token, org_id),
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["assigned_user_id"] == str(user2_id)


# ----------------------------------------------------------------------------------
# 5.15: a "date" custom field validates via date.fromisoformat
# ----------------------------------------------------------------------------------
async def test_5_15_date_custom_field_is_validated(client):
    token, org, _ = await make_org_with_number(client, "b515@example.com", "Org B515", OUR)
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/custom-fields",
        json={"key": "closing_date", "label": "Closing Date", "kind": "date"},
        headers=h,
    )
    assert created.status_code == 201, created.text

    bad = await client.post(
        "/api/v1/contacts",
        json={
            "display_name": "Bad Date", "phones": [],
            "attributes": {"closing_date": "not-a-date"},
        },
        headers=h,
    )
    assert bad.status_code == 422, bad.text

    good = await client.post(
        "/api/v1/contacts",
        json={
            "display_name": "Good Date", "phones": [],
            "attributes": {"closing_date": "2026-09-01"},
        },
        headers=h,
    )
    assert good.status_code == 201, good.text
    assert good.json()["attributes"]["closing_date"] == "2026-09-01"


# ----------------------------------------------------------------------------------
# 5.16: a custom field key cannot collide with a builtin contact attribute
# ----------------------------------------------------------------------------------
async def test_5_16_custom_field_key_cannot_shadow_a_builtin_attribute(client):
    token, org, _ = await make_org_with_number(client, "b516@example.com", "Org B516", OUR)
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/custom-fields",
        json={"key": "company", "label": "Company", "kind": "text"},
        headers=h,
    )
    assert r.status_code == 422, r.text


# ----------------------------------------------------------------------------------
# 5.17: inbox grants reject a deactivated department, consistent error type
# ----------------------------------------------------------------------------------
async def test_5_17_grant_to_deactivated_department_is_rejected(client, session):
    token, org, _ = await make_org_with_number(client, "b517@example.com", "Org B517", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    dept = await client.post("/api/v1/departments", json={"name": "Sales"}, headers=h)
    assert dept.status_code == 201, dept.text
    dept_id = dept.json()["id"]
    deactivated = await client.patch(
        f"/api/v1/departments/{dept_id}", json={"is_active": False}, headers=h
    )
    assert deactivated.status_code == 200, deactivated.text

    set_org_context(session, org_id)
    inbox = await _inbox_for(session, OUR)

    r = await client.put(
        f"/api/v1/inboxes/{inbox.id}/grants",
        json={"grants": [{"grantee_type": "department", "grantee_id": dept_id, "role": "member"}]},
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"

    # Bogus user grantee also 422 (same error type, not a 404/422 split).
    r_user = await client.put(
        f"/api/v1/inboxes/{inbox.id}/grants",
        json={
            "grants": [
                {"grantee_type": "user", "grantee_id": str(uuid.uuid4()), "role": "member"}
            ]
        },
        headers=h,
    )
    assert r_user.status_code == 422, r_user.text
    assert r_user.json()["error"]["code"] == "validation_failed"


# ----------------------------------------------------------------------------------
# 5.18: conversations backfill query is exact pairs, not a cartesian cross
# ----------------------------------------------------------------------------------
async def test_5_18_conversations_backfill_does_not_cross_unrelated_pairs(
    app_with_carrier, session
):
    """A call-only pair (OUR, THEIRS) and an unrelated MESSAGE thread sharing only
    OUR_B with a DIFFERENT contact must not be conflated by the backfill lookup - the
    cartesian IN/IN bug would have candidate_threads include the wrong thread merely
    because it shared one half of the key."""
    from app.models.voice import Call

    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "b518@example.com", "Org B518", OUR)
    org_id = uuid.UUID(org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR_B}, headers=auth_headers(token, org_id)
    )
    assert r.status_code == 201, r.text

    set_org_context(session, org_id)
    # A decoy thread that shares our_e164=OUR with the call pair below, but a
    # DIFFERENT contact - and one that shares contact_e164 with nothing relevant.
    decoy = await messaging_svc.upsert_thread(session, org_id, OUR, "+19725559000")
    decoy.status = "closed"
    decoy.last_message_at = decoy.created_at

    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
        our_e164=OUR, carrier="telnyx", status="no_answer",
    )
    session.add(call)
    await session.commit()

    r = await client.get(
        "/api/v1/conversations?filter=all&tab=calls", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    call_pair = next(
        i for i in items if i["our_e164"] == OUR and i["contact_e164"] == THEIRS
    )
    # The call-only pair must resolve as "open" (the real default, since no thread of
    # its own exists) rather than inheriting the closed decoy's status.
    assert call_pair["status"] == "open"


# ----------------------------------------------------------------------------------
# Opus-verifier follow-ups on Area 5
# ----------------------------------------------------------------------------------
async def test_0a_read_pair_404s_when_our_e164_is_not_an_org_number(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "f0a-a@example.com", "Org F0A-A", OUR)
    h = auth_headers(token, org["id"])

    # OUR_B was never purchased/added for this org.
    r = await client.post(
        "/api/v1/inbox/read-pair", json={"our_e164": OUR_B, "contact_e164": THEIRS}, headers=h
    )
    assert r.status_code == 404, r.text


async def test_0a_read_pair_404s_without_an_existing_call_or_message(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "f0a-b@example.com", "Org F0A-B", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    # OUR is a real org number, but nothing (no Call, no Message) has ever happened
    # between OUR and THEIRS - the route must refuse to fabricate a thread for it.
    r = await client.post(
        "/api/v1/inbox/read-pair", json={"our_e164": OUR, "contact_e164": THEIRS}, headers=h
    )
    assert r.status_code == 404, r.text

    set_org_context(session, org_id)
    thread = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR, MessageThread.contact_e164 == THEIRS
            )
        )
    ).scalar_one_or_none()
    assert thread is None, "no thread should have been fabricated"


async def test_0a_read_pair_succeeds_with_a_message_backed_pair(app_with_carrier, session):
    """A message-only conversation (thread already exists via a real Message) still
    works through read-pair - the new precondition is satisfied by a Message, not only
    by a Call."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "f0a-c@example.com", "Org F0A-C", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
            status="received", from_e164=THEIRS, to_e164=OUR, body="hi",
        )
    )
    await session.commit()

    r = await client.post(
        "/api/v1/inbox/read-pair", json={"our_e164": OUR, "contact_e164": THEIRS}, headers=h
    )
    assert r.status_code == 204, r.text


async def test_0b_claim_is_idempotent_for_the_same_caller_but_409_for_someone_else(
    app_with_carrier, session
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "f0b@example.com", "Org F0B", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    await session.commit()

    owner_user = await users_repo.get_by_email(session, "f0b@example.com")
    h = auth_headers(token, org_id)

    r1 = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(owner_user.id)},
        headers=h,
    )
    assert r1.status_code == 200, r1.text

    # Same caller re-claiming their own already-claimed thread is idempotent (200), not
    # a conflict - e.g. a retried request after a dropped response.
    r2 = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(owner_user.id)},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["assigned_user_id"] == str(owner_user.id)

    # A different caller trying to self-claim the same thread still loses the race.
    token2, user2_id = await _scoped_member(
        client, session, org_id, "f0b-agent@example.com", ["inbox:manage"],
        grant_e164=OUR, grant_role="member",
    )
    r3 = await client.patch(
        f"/api/v1/threads/{thread.id}",
        json={"assigned_user_id": str(user2_id)},
        headers=auth_headers(token2, org_id),
    )
    assert r3.status_code == 409, r3.text


async def test_0c_builtin_attribute_is_checked_before_a_colliding_custom_def(
    client, session
):
    """A CustomFieldDef colliding with a builtin key can, in principle, pre-date the
    5.16 create-time guard (or be inserted directly, e.g. by a migration/backfill).
    validate_attributes must check BUILTIN_CONTACT_ATTRIBUTES BEFORE any such
    definition, so it can never shadow the builtin's plain-text handling."""
    from app.models.contacts import CustomFieldDef

    token, org, _ = await make_org_with_number(client, "f0c@example.com", "Org F0C", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    set_org_context(session, org_id)
    # Bypass the route-level 5.16 guard entirely - simulate a pre-existing colliding def.
    session.add(
        CustomFieldDef(
            id=uuid.uuid4(), org_id=org_id, key="role", label="Role", kind="select",
            options=["buyer", "seller"],
        )
    )
    await session.commit()

    # If the colliding def still shadowed the builtin, this plain-text value ("Buyer",
    # capitalized) would be rejected as not in the def's ["buyer", "seller"] options.
    r = await client.post(
        "/api/v1/contacts",
        json={"display_name": "Shadow Test", "phones": [], "attributes": {"role": "Buyer"}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["attributes"] == {"role": "Buyer"}


# ----------------------------------------------------------------------------------
# Inbox "important" feature (2026-09-09): GET /conversations?filter=important +
# POST /inbox/important-pair
# ----------------------------------------------------------------------------------
async def test_important_filter_returns_only_starred_pairs(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "imp1@example.com", "Org Imp1", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)
    set_org_context(session, org_id)

    starred = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    starred.last_message_at = starred.created_at
    starred.is_important = True

    not_starred = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS_B)
    not_starred.last_message_at = not_starred.created_at
    await session.commit()

    r = await client.get("/api/v1/conversations?filter=important", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [item["contact_e164"] for item in items] == [THEIRS]
    assert items[0]["important"] is True


async def test_important_pair_toggles_on_and_off(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "imp2@example.com", "Org Imp2", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, THEIRS)
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
            status="received", from_e164=THEIRS, to_e164=OUR, body="hi",
        )
    )
    thread.last_message_at = thread.created_at
    await session.commit()

    r = await client.post(
        "/api/v1/inbox/important-pair",
        json={"our_e164": OUR, "contact_e164": THEIRS, "important": True},
        headers=h,
    )
    assert r.status_code == 204, r.text

    r_on = await client.get("/api/v1/conversations?filter=important", headers=h)
    assert [i["contact_e164"] for i in r_on.json()["items"]] == [THEIRS]

    r = await client.post(
        "/api/v1/inbox/important-pair",
        json={"our_e164": OUR, "contact_e164": THEIRS, "important": False},
        headers=h,
    )
    assert r.status_code == 204, r.text

    r_off = await client.get("/api/v1/conversations?filter=important", headers=h)
    assert r_off.json()["items"] == []


async def test_important_pair_denied_for_viewer_without_use_access(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "imp3@example.com", "Org Imp3", OUR)
    org_id = uuid.UUID(org["id"])

    token_viewer, _ = await _scoped_member(
        client, session, org_id, "imp3-viewer@example.com", ["inbox:read"],
        grant_e164=OUR, grant_role="viewer",
    )
    r = await client.post(
        "/api/v1/inbox/important-pair",
        json={"our_e164": OUR, "contact_e164": THEIRS, "important": True},
        headers=auth_headers(token_viewer, org_id),
    )
    assert r.status_code == 403, r.text


async def test_important_pair_404s_for_a_foreign_number(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "imp4@example.com", "Org Imp4", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    r = await client.post(
        "/api/v1/inbox/important-pair",
        json={"our_e164": OUR_B, "contact_e164": THEIRS, "important": True},
        headers=h,
    )
    assert r.status_code == 404, r.text


async def test_important_pair_works_for_a_call_only_pair(app_with_carrier, session):
    from app.models.voice import Call

    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "imp5@example.com", "Org Imp5", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)
    set_org_context(session, org_id)

    existing = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR, MessageThread.contact_e164 == THEIRS
            )
        )
    ).scalar_one_or_none()
    assert existing is None, "no thread should exist yet for a call-only pair"

    session.add(
        Call(
            id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
            our_e164=OUR, carrier="telnyx", status="completed",
        )
    )
    await session.commit()

    r = await client.post(
        "/api/v1/inbox/important-pair",
        json={"our_e164": OUR, "contact_e164": THEIRS, "important": True},
        headers=h,
    )
    assert r.status_code == 204, r.text

    thread = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR, MessageThread.contact_e164 == THEIRS
            )
        )
    ).scalar_one()
    assert thread.is_important is True

    r2 = await client.get("/api/v1/conversations?filter=important", headers=h)
    assert r2.status_code == 200, r2.text
    items = r2.json()["items"]
    assert [i["contact_e164"] for i in items] == [THEIRS]
    assert items[0]["important"] is True


async def test_important_pair_404s_without_an_existing_call_or_message(
    app_with_carrier, session
):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "imp6@example.com", "Org Imp6", OUR)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    r = await client.post(
        "/api/v1/inbox/important-pair",
        json={"our_e164": OUR, "contact_e164": THEIRS, "important": True},
        headers=h,
    )
    assert r.status_code == 404, r.text
