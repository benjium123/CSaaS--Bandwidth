from __future__ import annotations

import uuid

from app.providers.domain import CarrierError, SendResult
from tests.conftest import auth_headers, create_org, make_org_with_number, register_and_login

OUR = "+12145550100"
THEIRS = "+19725550199"


async def test_send_happy_path(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "a@example.com", "Org A", OUR)

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "Hello there"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["from_e164"] == OUR
    assert body["to_e164"] == THEIRS
    assert body["segment_count_est"] == 1
    assert body["thread_id"]

    # The carrier saw our correlation id, which is the message id.
    assert len(fake.sent) == 1
    assert fake.sent[0].tag == body["id"]
    assert fake.sent[0].to == THEIRS

    threads = await client.get("/api/v1/threads", headers=auth_headers(token, org["id"]))
    assert threads.status_code == 200
    assert len(threads.json()) == 1


async def test_from_number_must_be_owned_by_this_org(app_with_carrier):
    """The tenancy teeth: another org's number must be indistinguishable from a
    non-existent one."""
    client, fake, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "a2@example.com", "Org A", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "b2@example.com", "Org B", "+12145550111"
    )

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hi", "from": OUR},  # org A's number, sent as org B
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"
    assert fake.sent == []


async def test_invalid_to_is_422(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "a3@example.com", "Org A", OUR)
    r = await client.post(
        "/api/v1/messages",
        json={"to": "not-a-number", "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422
    assert fake.sent == []


async def test_carrier_rejection_is_data_not_an_http_error(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "a4@example.com", "Org A", OUR)
    fake.scripted = [
        SendResult("rejected", None, CarrierError("invalid_request", "4720", False, "bad dest"))
    ]

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    # 201: a row WAS created. The client reads one uniform resource either way.
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "rejected"
    assert r.json()["error_code"] == "4720"


async def test_no_carrier_configured_is_503(app_with_carrier):
    client, fake, application = app_with_carrier
    token, org, _ = await make_org_with_number(client, "a5@example.com", "Org A", OUR)
    application.state.carrier = None

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "carrier_not_configured"

    msgs = await client.get("/api/v1/messages", headers=auth_headers(token, org["id"]))
    assert msgs.json() == []


async def test_tenancy_on_reads(app_with_carrier):
    client, fake, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "a6@example.com", "Org A", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "b6@example.com", "Org B", "+12145550122"
    )

    sent = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "secret"},
        headers=auth_headers(token_a, org_a["id"]),
    )
    a_msg_id = sent.json()["id"]

    hb = auth_headers(token_b, org_b["id"])
    assert (await client.get("/api/v1/threads", headers=hb)).json() == []
    assert (await client.get("/api/v1/messages", headers=hb)).json() == []
    assert (await client.get(f"/api/v1/messages/{a_msg_id}", headers=hb)).status_code == 404

    ha = auth_headers(token_a, org_a["id"])
    assert (await client.get(f"/api/v1/messages/{a_msg_id}", headers=ha)).status_code == 200


async def test_permission_denied_without_inbox_send(app_with_carrier, session):
    import sqlalchemy as sa

    from app.db.base import set_org_context
    from app.models import OrgMembership, Role
    from app.repositories import users as users_repo

    client, fake, _ = app_with_carrier
    owner_token, org, _ = await make_org_with_number(client, "own@example.com", "Org A", OUR)
    agent_token = await register_and_login(client, "agent2@example.com")
    agent_user = await users_repo.get_by_email(session, "agent2@example.com")

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    agent_role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    # Strip inbox:send from the agent role for this test.
    agent_role.permissions = ["inbox:read"]
    session.add(
        OrgMembership(
            id=uuid.uuid4(), org_id=org_id, user_id=agent_user.id, role_id=agent_role.id
        )
    )
    await session.commit()

    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hi"},
        headers=auth_headers(agent_token, org["id"]),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"
    assert fake.sent == []


async def test_number_is_globally_unique(app_with_carrier):
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "u1@example.com", "Org A", OUR)
    token_b = await register_and_login(client, "u2@example.com")
    org_b = await create_org(client, token_b, "Org B")
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR}, headers=auth_headers(token_b, org_b["id"])
    )
    assert r.status_code == 409


async def test_two_numbers_no_longer_ambiguous_sticky_picks_one(app_with_carrier):
    """P2 DR-10 recorded deviation.

    P1 answered 422 when an org had several active numbers and no explicit `from`.
    P2 replaced that with sticky-sender selection: a brand-new conversation gets a
    deterministic pick from the pool instead of an error.
    """
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "amb@example.com", "Org A", OUR)
    second = "+12145550133"
    await client.post(
        "/api/v1/numbers", json={"e164": second}, headers=auth_headers(token, org["id"])
    )
    r = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["from_e164"] in (OUR, second)

    # Deterministic: the same contact always lands on the same number.
    again = await client.post(
        "/api/v1/messages",
        json={"to": THEIRS, "body": "hi again"},
        headers=auth_headers(token, org["id"]),
    )
    assert again.json()["from_e164"] == r.json()["from_e164"]
