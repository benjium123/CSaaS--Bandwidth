from __future__ import annotations

import uuid
from urllib.parse import quote

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    AgentProfile,
    Call,
    CallScore,
    Contact,
    ContactPhone,
    Message,
    MessageThread,
    User,
)
from app.services import messaging as messaging_svc
from tests.conftest import auth_headers, register_and_login
from tests.test_agent_seams import (
    OUR,
    THEIRS,
    _place_call,
    worker_headers,
    worker_token,
)
from tests.test_agent_seams import app_with_agent  # noqa: F401 - pytest fixture by name

THEIRS_OWNED = "+19725550200"


async def _post_outcomes(client, outcomes):
    return await client.post(
        "/api/v1/agent/outcome",
        json={"outcomes": outcomes},
        headers=worker_headers(worker_token()),
    )


async def _get_user_id_by_email(session, email: str) -> uuid.UUID:
    return (await session.execute(sa.select(User.id).where(User.email == email))).scalar_one()


async def _place_livekit_call(
    session, org_id: uuid.UUID, *, contact_e164: str = THEIRS, our_e164: str = OUR
) -> str:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        direction="outbound",
        # calls.carrier is NOT NULL: a LiveKit room call still names the TRUNK carrier
        # (voice_plane/service.py's own start_room_call sets "telnyx" for the same reason).
        carrier="telnyx",
        status="in_progress",
        extra={"via": "livekit", "room": f"room-{uuid.uuid4()}"},
    )
    session.add(call)
    await session.commit()
    return str(call.id)


async def test_outcome_post_is_idempotent_on_call_id(app_with_agent, session):
    """Posting the same outcome batch twice leaves exactly one CallScore row."""
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "idem@example.com", "Org Idem")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    payload = {"call_id": call_id, "summary": "Same summary", "disposition": "booked"}
    first = await _post_outcomes(client, [payload])
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1

    second = await _post_outcomes(client, [payload])
    assert second.status_code == 200, second.text
    assert second.json()["accepted"] == 1

    rows = (
        await session.execute(
            sa.select(CallScore).where(CallScore.call_id == uuid.UUID(call_id))
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].summary == "Same summary"
    assert rows[0].disposition == "booked"
    assert rows[0].status == "done"


async def test_extracted_fields_write_contact_attributes_only_for_mapped_keys(
    app_with_agent, session
):
    """One extracted key is mapped to a contact attribute and one is not: only the
    mapped one is written to the contact."""
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "mapped@example.com", "Org Mapped")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="Caller", attributes={})
    contact_id = contact.id
    session.add(contact)
    session.add(
        ContactPhone(id=uuid.uuid4(), org_id=org_id, contact_id=contact.id, e164=THEIRS)
    )
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Maps",
        system_prompt="",
        is_default=True,
        post_call_fields=[
            {"name": "company", "type": "text", "write_to_attribute": "company"},
            {"name": "internal_note", "type": "text"},
        ],
    )
    session.add(profile)
    await session.commit()

    extracted = {"company": "ACME", "internal_note": "not applied"}
    r = await _post_outcomes(
        client, [{"call_id": call_id, "summary": "Mapped", "extracted": extracted}]
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["applied_attributes"] == ["company"]

    row = (
        await session.execute(
            sa.select(CallScore).where(CallScore.call_id == uuid.UUID(call_id))
        )
    ).scalar_one()
    assert row.extracted == extracted

    # Column-only select on purpose: the app wrote on ITS OWN session and this one still
    # holds the contact in its identity map from the setup commit, so a whole-entity
    # select would hand back the stale instance.
    attributes_after = (
        await session.execute(
            sa.select(Contact.attributes).where(Contact.id == contact_id)
        )
    ).scalar_one()
    assert attributes_after == {"company": "ACME"}


async def test_outcome_never_blanks_a_field_it_did_not_send(app_with_agent, session):
    """A second outcome carrying only disposition leaves the prior summary intact."""
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "blank@example.com", "Org Blank")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    first = await _post_outcomes(
        client, [{"call_id": call_id, "summary": "Original summary", "sentiment": "positive"}]
    )
    assert first.status_code == 200, first.text

    second = await _post_outcomes(client, [{"call_id": call_id, "disposition": "no_answer"}])
    assert second.status_code == 200, second.text

    row = (
        await session.execute(
            sa.select(CallScore).where(CallScore.call_id == uuid.UUID(call_id))
        )
    ).scalar_one()
    assert row.summary == "Original summary"
    assert row.sentiment == "positive"
    assert row.disposition == "no_answer"


async def test_outcome_requires_the_worker_token(app_with_agent):
    """A user bearer token is not valid on the machine seam."""
    client, _app = app_with_agent
    user_token, org, call_id = await _place_call(client, "user401@example.com", "Org 401")

    r = await client.post(
        "/api/v1/agent/outcome",
        json={"outcomes": [{"call_id": call_id, "summary": "nope"}]},
        headers=auth_headers(user_token, org["id"]),
    )
    assert r.status_code == 401


async def test_outcome_is_tenant_scoped(app_with_agent, session):
    """The seam resolves org from the Call row, and a follow_up_sms_message_id from
    another org is ignored rather than stored."""
    client, _app = app_with_agent
    _token_a, org_a, call_a = await _place_call(client, "scopeA@example.com", "Org Scope A")
    _token_b, org_b, call_b = await _place_call(
        client, "scopeB@example.com", "Org Scope B", e164="+12145550101"
    )
    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    set_org_context(session, org_a_id)
    thread = await messaging_svc.upsert_thread(session, org_a_id, OUR, THEIRS)
    msg = Message(
        id=uuid.uuid4(),
        org_id=org_a_id,
        thread_id=thread.id,
        direction="outbound",
        from_e164=OUR,
        to_e164=THEIRS,
        body="hello",
        status="sent",
    )
    session.add(msg)
    await session.commit()

    r = await _post_outcomes(
        client,
        [
            {
                "call_id": call_b,
                "summary": "Scoped summary",
                "follow_up_sms_message_id": str(msg.id),
            }
        ],
    )
    assert r.status_code == 200, r.text

    set_org_context(session, org_b_id)
    row_b = (
        await session.execute(
            sa.select(CallScore).where(CallScore.call_id == uuid.UUID(call_b))
        )
    ).scalar_one()
    assert row_b.org_id == org_b_id
    assert row_b.follow_up_sms_message_id is None

    set_org_context(session, org_a_id)
    rows_a = (
        await session.execute(
            sa.select(CallScore).where(CallScore.call_id == uuid.UUID(call_a))
        )
    ).scalars().all()
    assert rows_a == []


async def test_handoff_assigns_thread_and_sets_unowned_contact_owner(app_with_agent, session):
    """Warm transfer assigns thread and only takes over unowned contacts."""
    client, _app = app_with_agent
    _token, org, _call = await _place_call(client, "handoff1@example.com", "Org Handoff1")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    user_id = await _get_user_id_by_email(session, "handoff1@example.com")

    contact_unowned = Contact(
        id=uuid.uuid4(), org_id=org_id, display_name="Caller", attributes={}
    )
    session.add(contact_unowned)
    session.add(
        ContactPhone(
            id=uuid.uuid4(), org_id=org_id, contact_id=contact_unowned.id, e164=THEIRS
        )
    )

    # contacts.owner_user_id is a real FK - a fabricated uuid fails the constraint
    # (conftest turns SQLite's foreign_keys pragma ON), so register a real second user.
    await register_and_login(client, "handoff1-other@example.com")
    other_owner = await _get_user_id_by_email(session, "handoff1-other@example.com")
    set_org_context(session, org_id)
    contact_owned = Contact(
        id=uuid.uuid4(),
        org_id=org_id,
        display_name="Owned Caller",
        attributes={},
        owner_user_id=other_owner,
    )
    session.add(contact_owned)
    session.add(
        ContactPhone(
            id=uuid.uuid4(), org_id=org_id, contact_id=contact_owned.id, e164=THEIRS_OWNED
        )
    )
    await session.commit()

    call_id = await _place_livekit_call(session, org_id)
    r = await client.post(
        "/api/v1/agent/handoff",
        json={
            "call_id": call_id,
            "reason": "warm",
            "summary": "transfer",
            "to_user_id": str(user_id),
        },
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 200, r.text

    set_org_context(session, org_id)
    thread = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR,
                MessageThread.contact_e164 == THEIRS,
            )
        )
    ).scalar_one()
    assert thread.assigned_user_id == user_id

    contact_u = (
        await session.execute(sa.select(Contact).where(Contact.id == contact_unowned.id))
    ).scalar_one()
    assert contact_u.owner_user_id == user_id

    call_id2 = await _place_livekit_call(session, org_id, contact_e164=THEIRS_OWNED)
    r2 = await client.post(
        "/api/v1/agent/handoff",
        json={
            "call_id": call_id2,
            "reason": "warm",
            "summary": "transfer owned",
            "to_user_id": str(user_id),
        },
        headers=worker_headers(worker_token()),
    )
    assert r2.status_code == 200, r2.text

    contact_o = (
        await session.execute(sa.select(Contact).where(Contact.id == contact_owned.id))
    ).scalar_one()
    assert contact_o.owner_user_id == other_owner


async def test_handoff_to_a_non_member_is_refused(app_with_agent, session):
    """A warm transfer to a user outside the org is a plain 422."""
    client, _app = app_with_agent
    _token, org, _call = await _place_call(client, "nonmember@example.com", "Org NonMember")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    call_id = await _place_livekit_call(session, org_id)
    r = await client.post(
        "/api/v1/agent/handoff",
        json={
            "call_id": call_id,
            "reason": "warm",
            "summary": "nope",
            "to_user_id": str(uuid.uuid4()),
        },
        headers=worker_headers(worker_token()),
    )
    assert r.status_code == 422
    assert "not on this team" in r.json()["error"]["message"]


async def test_ai_call_card_appears_in_the_inbox_timeline(app_with_agent, session):
    """A call with an outcome renders assistant; a plain call renders assistant: null."""
    client, _app = app_with_agent
    token, org, call_id = await _place_call(client, "card@example.com", "Org Card")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(), org_id=org_id, name="Front Desk", is_default=True
    )
    session.add(profile)
    await session.commit()

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    plain_call_id = r.json()["id"]

    outcome = {
        "call_id": call_id,
        "summary": "Card summary",
        "disposition": "booked",
        "sentiment": "positive",
    }
    await _post_outcomes(client, [outcome])

    transcript = {
        "call_id": call_id,
        "segments": [{"role": "agent", "text": "Hello", "at_ms": 0}],
    }
    r_transcript = await client.post(
        "/api/v1/agent/transcript",
        json=transcript,
        headers=worker_headers(worker_token()),
    )
    assert r_transcript.status_code == 200, r_transcript.text

    # The route is /conversations/{contact_e164}/timeline with our_e164 as a QUERY param.
    url = f"/api/v1/conversations/{quote(THEIRS, safe='')}/timeline"
    r_timeline = await client.get(
        url, params={"our_e164": OUR}, headers=auth_headers(token, org["id"])
    )
    assert r_timeline.status_code == 200, r_timeline.text
    body = r_timeline.json()
    items = body["items"] if isinstance(body, dict) else body

    ai_call = next(
        item for item in items
        if item.get("kind") == "call" and str(item.get("id")) == call_id
    )
    assert ai_call["assistant"] == {
        "name": "Front Desk",
        "summary": "Card summary",
        "disposition": "booked",
        "sentiment": "positive",
        "has_transcript": True,
    }

    plain_call = next(
        item for item in items
        if item.get("kind") == "call" and str(item.get("id")) == plain_call_id
    )
    assert plain_call["assistant"] is None


async def test_test_call_is_flagged_and_excluded_from_analytics(app_with_agent):
    """Analytics counts only non-test CallScore rows."""
    client, _app = app_with_agent
    token, org, call1 = await _place_call(client, "testflag@example.com", "Org TestFlag")

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    call2 = r.json()["id"]

    post = await _post_outcomes(
        client,
        [
            {"call_id": call1, "summary": "test call", "is_test": True},
            {"call_id": call2, "summary": "real call"},
        ],
    )
    assert post.status_code == 200, post.text

    r_analytics = await client.get(
        "/api/v1/analytics/assistant", headers=auth_headers(token, org["id"])
    )
    assert r_analytics.status_code == 200, r_analytics.text
    body = r_analytics.json()
    assert body["calls"] == 1
    assert body["booked"] == 0
    assert body["cost_micros"] is None


async def test_assistant_analytics_window_and_empty_state(app_with_agent):
    """Empty window yields zeros/None; malformed dates are plain 422."""
    client, _app = app_with_agent
    token, org, _call_id = await _place_call(client, "empty@example.com", "Org Empty")
    h = auth_headers(token, org["id"])

    r_empty = await client.get(
        "/api/v1/analytics/assistant",
        params={"from": "2000-01-01", "to": "2000-01-02"},
        headers=h,
    )
    assert r_empty.status_code == 200, r_empty.text
    body = r_empty.json()
    assert body["calls"] == 0
    assert body["minutes"] == 0.0
    assert body["answer_rate"] is None
    assert body["handoff_rate"] is None
    assert body["booked"] == 0
    assert body["avg_duration_seconds"] is None
    assert body["cost_micros"] is None

    r_bad = await client.get(
        "/api/v1/analytics/assistant",
        params={"from": "bad", "to": "2024-01-01"},
        headers=h,
    )
    assert r_bad.status_code == 422
    assert "YYYY-MM-DD" in r_bad.json()["error"]["message"]


async def test_assistant_analytics_can_be_filtered_to_one_assistant(app_with_agent, session):
    """The optional profile_id narrows the window to one assistant; a call whose outcome
    names a different assistant is not counted."""
    client, _app = app_with_agent
    token, org, call_a = await _place_call(client, "byprofile@example.com", "Org ByProfile")
    org_id = uuid.UUID(org["id"])

    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    call_b = r.json()["id"]

    set_org_context(session, org_id)
    first = AgentProfile(id=uuid.uuid4(), org_id=org_id, name="First")
    second = AgentProfile(id=uuid.uuid4(), org_id=org_id, name="Second")
    session.add_all([first, second])
    await session.commit()

    post = await _post_outcomes(
        client,
        [
            {"call_id": call_a, "summary": "one"},
            {"call_id": call_b, "summary": "two"},
        ],
    )
    assert post.status_code == 200, post.text

    # The seam stamps whichever profile resolve_call_profile returned; pin one row to the
    # OTHER assistant so the filter has something real to exclude.
    set_org_context(session, org_id)
    await session.execute(
        sa.update(CallScore)
        .where(CallScore.call_id == uuid.UUID(call_a))
        .values(profile_id=first.id)
    )
    await session.execute(
        sa.update(CallScore)
        .where(CallScore.call_id == uuid.UUID(call_b))
        .values(profile_id=second.id)
    )
    await session.commit()

    headers = auth_headers(token, org["id"])
    both = await client.get("/api/v1/analytics/assistant", headers=headers)
    assert both.status_code == 200, both.text
    assert both.json()["calls"] == 2

    only_first = await client.get(
        "/api/v1/analytics/assistant",
        params={"profile_id": str(first.id)},
        headers=headers,
    )
    assert only_first.status_code == 200, only_first.text
    assert only_first.json()["calls"] == 1

    other_org_profile = uuid.uuid4()
    none_match = await client.get(
        "/api/v1/analytics/assistant",
        params={"profile_id": str(other_org_profile)},
        headers=headers,
    )
    assert none_match.status_code == 200, none_match.text
    assert none_match.json()["calls"] == 0
    assert none_match.json()["answer_rate"] is None
