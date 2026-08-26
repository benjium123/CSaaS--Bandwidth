from __future__ import annotations

import pytest

from app.services.sender import pick_deterministic
from tests.conftest import auth_headers, make_org_with_number, register_and_login

A = "+12145550100"
B = "+12145550111"
C = "+12145550122"
# Chosen so that pick_deterministic(CONTACT, [A, B]) == B - the test needs the
# deterministic pick to DISAGREE with the sticky answer, or it proves nothing.
CONTACT = "+19725550201"


# ----------------------------------------------------------------------------------
# Pure function — pinned so a refactor cannot silently reshuffle every conversation
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "contact,expected",
    [
        ("+19725550199", "+12145550111"),
        ("+19725550200", "+12145550100"),
        ("+19725550201", "+12145550100"),
        ("+19725550202", "+12145550122"),
    ],
)
def test_pick_deterministic_pinned(contact, expected):
    """These outputs are PINNED on purpose.

    Changing the hash or the ordering re-shuffles which number every future conversation
    is assigned to. That must never happen as an accidental side effect of a refactor.
    """
    assert pick_deterministic(contact, [A, B, C]) == expected


def test_pick_deterministic_is_order_independent():
    """The pool comes back from the DB in arbitrary order; the mapping must not."""
    for contact in ("+19725550199", "+19725550200", "+19725550202"):
        assert pick_deterministic(contact, [A, B, C]) == pick_deterministic(contact, [C, B, A])


def test_pick_deterministic_empty_pool_raises():
    from app.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError):
        pick_deterministic(CONTACT, [])


# ----------------------------------------------------------------------------------
# Integration
# ----------------------------------------------------------------------------------
async def test_sticky_reuses_the_threads_number(app_with_carrier):
    """Affinity beats the deterministic pick: an existing conversation keeps its number."""
    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "s1@example.com", "Org A", A)
    h = auth_headers(token, org["id"])
    await client.post("/api/v1/numbers", json={"e164": B}, headers=h)

    # Force the thread onto A explicitly...
    first = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "one", "from": A}, headers=h
    )
    assert first.status_code == 201
    assert first.json()["from_e164"] == A

    # ...then send with no `from`. Sticky must return A even though the deterministic
    # pick for this contact (with this 2-number pool) is B.
    assert pick_deterministic(CONTACT, [A, B]) == B, "test premise: pool pick must differ"
    second = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "two"}, headers=h)
    assert second.status_code == 201
    assert second.json()["from_e164"] == A


async def test_inactive_sticky_fails_loudly(app_with_carrier, session):
    """GOTCHA #18: a retired pool number must never silently move a conversation."""
    import sqlalchemy as sa

    from app.db.base import set_org_context
    from app.models import OrgNumber

    client, fake, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "s2@example.com", "Org A", A)
    h = auth_headers(token, org["id"])
    await client.post("/api/v1/numbers", json={"e164": B}, headers=h)

    await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "one", "from": A}, headers=h
    )
    sent_before = len(fake.sent)

    # Retire A.
    import uuid

    set_org_context(session, uuid.UUID(org["id"]))
    number_a = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == A))
    ).scalar_one()
    number_a.is_active = False
    await session.commit()

    blocked = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "two"}, headers=h)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "sticky_sender_unavailable"
    assert len(fake.sent) == sent_before, "no carrier call may happen on a blocked send"

    # The same send WITH explicit consent goes out on the surviving number.
    allowed = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "two", "allow_reassign": True},
        headers=h,
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["from_e164"] == B
    # A new bucket, because a thread is keyed on (our number, contact number).
    assert allowed.json()["thread_id"] != blocked.json().get("thread_id")


async def test_explicit_from_still_enforced_across_orgs(app_with_carrier):
    """P1's tenancy teeth survive the sticky rewrite."""
    client, fake, _ = app_with_carrier
    token_a, org_a, _ = await make_org_with_number(client, "s3@example.com", "Org A", A)
    token_b = await register_and_login(client, "s4@example.com")
    from tests.conftest import create_org

    org_b = await create_org(client, token_b, "Org B")
    await client.post(
        "/api/v1/numbers", json={"e164": C}, headers=auth_headers(token_b, org_b["id"])
    )

    r = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "body": "hi", "from": A},  # org A's number
        headers=auth_headers(token_b, org_b["id"]),
    )
    assert r.status_code == 422
    assert fake.sent == []
