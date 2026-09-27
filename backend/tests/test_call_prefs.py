"""Do Not Disturb + internal forwarding (services/call_prefs.py): who rings, the event gate,
sequential ring groups skipping DND members, and the /me/call-prefs routes."""

from __future__ import annotations

import types
import uuid

import pytest

from app.api.routes import softphone as softphone_routes
from app.api.routes.softphone import _event_visible
from app.errors import ValidationFailedError
from app.services import call_prefs, routing_exec
from app.services.inbox_access import InboxAccess
from tests.conftest import auth_headers, create_org, register_and_login

A, B, C = (str(uuid.uuid4()) for _ in range(3))
NUM = "+14694617145"
MEMBER = InboxAccess(is_admin=False, member_e164s=frozenset({NUM}), viewer_e164s=frozenset())
ADMIN = InboxAccess(is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset())


def test_ring_visible_rules():
    dnd = {A: None, B: C}  # A on DND, B on DND forwarding to C
    assert call_prefs.ring_visible(dnd, uuid.UUID(A), None) is False
    assert call_prefs.ring_visible(dnd, uuid.UUID(B), [B]) is False
    assert call_prefs.ring_visible(dnd, uuid.UUID(C), [B]) is True
    assert call_prefs.ring_visible(dnd, uuid.UUID(C), [A]) is None
    assert call_prefs.ring_visible({}, uuid.UUID(C), None) is None


@pytest.mark.asyncio
async def test_event_gate_dnd_and_forwarding(monkeypatch):
    async def fake_dnd(org_id):
        return {A: None, B: C}

    monkeypatch.setattr(call_prefs, "dnd_users_cached", fake_dnd)
    org = uuid.uuid4()
    fanout = {"type": "call.ring", "to": NUM}
    # DND silences the ring, admins included.
    assert await _event_visible(fanout, MEMBER, org, uuid.UUID(A)) is False
    assert await _event_visible(fanout, ADMIN, org, uuid.UUID(A)) is False
    # Everyone else still rings as before.
    assert await _event_visible(fanout, MEMBER, org, uuid.UUID(C)) is True
    # A ring offered only to B (DND, forwards to C) rings C instead.
    offered_b = {"type": "call.ring", "to": NUM, "ring_user_ids": [B]}
    assert await _event_visible(offered_b, MEMBER, org, uuid.UUID(C)) is True
    assert await _event_visible(offered_b, MEMBER, org, uuid.UUID(B)) is False
    # ...but only if C may take calls on that number.
    outsider = InboxAccess(is_admin=False, member_e164s=frozenset(), viewer_e164s=frozenset())
    assert await _event_visible(offered_b, outsider, org, uuid.UUID(C)) is False
    assert softphone_routes is not None


@pytest.mark.asyncio
async def test_sequential_ring_group_skips_dnd_members(monkeypatch):
    async def fake_dnd(session, org_id):
        return {A: None}

    monkeypatch.setattr(call_prefs, "dnd_users", fake_dnd)
    group = types.SimpleNamespace(strategy="sequential", member_user_ids=[A, B, C])

    class Session:
        async def get(self, model, key):
            return group

    events = []
    bus = types.SimpleNamespace(publish=lambda org_id, e: events.append(e))
    call = types.SimpleNamespace(id=uuid.uuid4(), our_e164=NUM)
    queue = types.SimpleNamespace(id=uuid.uuid4(), ring_group_id=uuid.uuid4())
    entry = types.SimpleNamespace(
        org_id=uuid.uuid4(), offered_user_id=None, state="waiting", offered_at=None
    )
    await routing_exec._offer_to_ring_group(Session(), bus, call, queue, entry, None)
    assert events[-1]["ring_user_ids"] == [B]  # A (DND, no forward) skipped
    assert str(entry.offered_user_id) == B


async def test_call_prefs_routes(client, session):
    token = await register_and_login(client, "dnd-owner@example.com")
    org = await create_org(client, token, "DND Org")
    h = auth_headers(token, org["id"])
    r = await client.get("/api/v1/me/call-prefs", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["dnd"] is False and r.json()["forward_to"] is None

    r = await client.put(
        "/api/v1/me/call-prefs", json={"dnd": True}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"dnd": True, "forward_to": None}
    r = await client.get("/api/v1/me/call-prefs", headers=h)
    assert r.json()["dnd"] is True

    # Forwarding only to a teammate in this workspace - never a stranger.
    r = await client.put(
        "/api/v1/me/call-prefs",
        json={"dnd": True, "forward_to": str(uuid.uuid4())},
        headers=h,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_set_prefs_refuses_self_forward():
    membership = types.SimpleNamespace(user_id=uuid.uuid4(), org_id=uuid.uuid4(), call_prefs=None)
    with pytest.raises(ValidationFailedError):
        await call_prefs.set_prefs(
            object(), membership, dnd=True, forward_to=membership.user_id
        )
