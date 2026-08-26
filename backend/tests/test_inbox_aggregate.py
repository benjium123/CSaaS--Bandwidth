from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Message, MessageThread
from tests.conftest import auth_headers, create_contact, create_tag, make_org_with_number

OUR = "+12145550100"


async def _seed(session, org_id: uuid.UUID, threads: int, per_thread: int) -> list[uuid.UUID]:
    """Seed N threads x M inbound messages directly, cheaply."""
    set_org_context(session, org_id)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ids = []
    made = []
    for t in range(threads):
        thread = MessageThread(
            id=uuid.uuid4(),
            org_id=org_id,
            our_e164=OUR,
            contact_e164=f"+1972555{t:04d}",
            last_message_at=base + timedelta(minutes=t),
            status="open",
        )
        session.add(thread)
        ids.append(thread.id)
        made.append((t, thread))
    # Flush the parents before the children: with SQLite FKs enforced, a single mixed
    # flush is not guaranteed to order these correctly.
    await session.flush()

    for t, thread in made:
        for m in range(per_thread):
            session.add(
                Message(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    thread_id=thread.id,
                    direction="inbound",
                    status="received",
                    from_e164=thread.contact_e164,
                    to_e164=OUR,
                    body=f"thread {t} message {m}",
                    media=[],
                    carrier="bandwidth",
                    provider_message_id=f"seed-{t}-{m}",
                    created_at=base + timedelta(minutes=t, seconds=m),
                )
            )
    await session.commit()
    return ids


async def test_shape_contract(app_with_carrier, session):
    """This key set IS the frontend's contract. Changing it breaks the console."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "i1@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])

    await _seed(session, org_id, threads=2, per_thread=3)

    r = await client.get("/api/v1/inbox/threads", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"items", "next_cursor"}
    assert len(body["items"]) == 2

    item = body["items"][0]
    assert set(item.keys()) == {
        "thread",
        "last_message",
        "unread",
        "contact",
        "assignee",
        "labels",
    }
    assert set(item["thread"].keys()) == {
        "id",
        "our_e164",
        "contact_e164",
        "status",
        "assigned_user_id",
        "last_message_at",
    }
    assert set(item["last_message"].keys()) == {
        "id",
        "direction",
        "body",
        "status",
        "created_at",
    }
    # Newest thread first, and its preview is that thread's TRUE latest message.
    assert item["last_message"]["body"] == "thread 1 message 2"
    assert item["unread"] == 3


async def test_query_count_constant(app_with_carrier, session, query_counter):
    """THE N+1 GATE.

    Query count must be independent of page size. A lazy load slipping into the
    serializer breaks this immediately rather than quietly degrading production.
    """
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "i2@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    await _seed(session, uuid.UUID(org["id"]), threads=30, per_thread=5)

    query_counter.reset()
    r5 = await client.get("/api/v1/inbox/threads?limit=5", headers=h)
    count_5 = query_counter.count
    assert r5.status_code == 200 and len(r5.json()["items"]) == 5

    query_counter.reset()
    r25 = await client.get("/api/v1/inbox/threads?limit=25", headers=h)
    count_25 = query_counter.count
    assert r25.status_code == 200 and len(r25.json()["items"]) == 25

    assert count_5 == count_25, (
        f"query count grew with page size ({count_5} -> {count_25}): that is an N+1"
    )
    assert count_25 <= 8, f"aggregate used {count_25} statements; ceiling is 8"


async def test_keyset_pagination_stable(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "i3@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])
    await _seed(session, org_id, threads=12, per_thread=1)

    seen: list[str] = []
    cursor = None
    for _ in range(3):
        url = "/api/v1/inbox/threads?limit=5" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url, headers=h)).json()
        seen.extend(i["thread"]["id"] for i in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)), "keyset pagination returned duplicates"
    assert len(seen) == 12


async def test_filters(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "i4@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])
    ids = await _seed(session, org_id, threads=3, per_thread=1)

    # status
    closed = await client.patch(
        f"/api/v1/threads/{ids[0]}", json={"status": "closed"}, headers=h
    )
    assert closed.status_code == 200
    open_only = (await client.get("/api/v1/inbox/threads?status=open", headers=h)).json()
    assert {i["thread"]["id"] for i in open_only["items"]} == {str(ids[1]), str(ids[2])}
    closed_only = (await client.get("/api/v1/inbox/threads?status=closed", headers=h)).json()
    assert [i["thread"]["id"] for i in closed_only["items"]] == [str(ids[0])]

    # assigned
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    await client.patch(
        f"/api/v1/threads/{ids[1]}", json={"assigned_user_id": me["id"]}, headers=h
    )
    mine = (await client.get("/api/v1/inbox/threads?assigned=me", headers=h)).json()
    assert [i["thread"]["id"] for i in mine["items"]] == [str(ids[1])]
    unassigned = (
        await client.get("/api/v1/inbox/threads?assigned=unassigned", headers=h)
    ).json()
    assert str(ids[1]) not in {i["thread"]["id"] for i in unassigned["items"]}

    # label
    tag = await create_tag(client, token, org["id"], "urgent")
    await client.put(
        f"/api/v1/threads/{ids[2]}/labels", json={"tag_ids": [tag["id"]]}, headers=h
    )
    labelled = (
        await client.get(f"/api/v1/inbox/threads?label_id={tag['id']}", headers=h)
    ).json()
    assert [i["thread"]["id"] for i in labelled["items"]] == [str(ids[2])]
    assert labelled["items"][0]["labels"][0]["name"] == "urgent"

    # q by phone fragment
    by_phone = (await client.get("/api/v1/inbox/threads?q=5550002", headers=h)).json()
    assert [i["thread"]["id"] for i in by_phone["items"]] == [str(ids[2])]

    # q by contact name
    contact = await create_contact(
        client, token, org["id"], "Zebra Corp", ["+19725550000"]
    )
    assert contact["id"]
    by_name = (await client.get("/api/v1/inbox/threads?q=zebra", headers=h)).json()
    assert [i["thread"]["id"] for i in by_name["items"]] == [str(ids[0])]


async def test_tenancy_on_inbox(app_with_carrier, session):
    client, _, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "i5@example.com", "Org A", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "i6@example.com", "Org B", "+12145550111"
    )
    await _seed(session, uuid.UUID(org_a["id"]), threads=3, per_thread=1)

    b = (
        await client.get(
            "/api/v1/inbox/threads", headers=auth_headers(token_b, org_b["id"])
        )
    ).json()
    assert b["items"] == []


@pytest.mark.pg_only
async def test_aggregate_on_postgres(app_with_carrier, session, query_counter):
    """Window function + keyset parity on the real backend."""
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "i7@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])
    await _seed(session, uuid.UUID(org["id"]), threads=30, per_thread=5)

    query_counter.reset()
    r = await client.get("/api/v1/inbox/threads?limit=25", headers=h)
    assert r.status_code == 200
    assert len(r.json()["items"]) == 25
    assert query_counter.count <= 8
    assert r.json()["items"][0]["last_message"]["body"].startswith("thread 29")


async def test_sqlite_supports_window_functions(session):
    """The preview query needs ROW_NUMBER(). Fail loudly here rather than mysteriously
    inside the aggregate if the bundled SQLite is ever too old."""
    version = (await session.execute(sa.text("select sqlite_version()"))).scalar_one()
    major, minor = (int(p) for p in str(version).split(".")[:2])
    assert (major, minor) >= (3, 25), f"SQLite {version} lacks window functions"
