"""P12 services/supervisor.py (DR-9): monitor (canPublish=false), whisper (B7: raises
FeatureUnavailableError - no server-side subscription-permission API exists), barge (full
token). monitor/barge each write a VoiceEvent; a non-supervisor role is RBAC-denied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
import jwt
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ConflictError, FeatureUnavailableError
from app.main import create_app
from app.models import OrgMembership, Role
from app.models.org import Org
from app.models.voice import Call, CallLeg
from app.models.voice import VoiceEvent as VoiceEventRow
from app.services import supervisor as supervisor_svc
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

CALLER_SIP_IDENTITY = "sip-caller-1"
LIVEKIT_SECRET = "lk-secret"


@dataclass
class FakeLiveKitApi:
    update_subscriptions_calls: list[dict] = field(default_factory=list)

    async def update_subscriptions(self, *, room, identity, track_sids, subscribe):  # noqa: ANN001
        self.update_subscriptions_calls.append(
            {"room": room, "identity": identity, "track_sids": track_sids, "subscribe": subscribe}
        )
        return {}


def _settings():
    return make_settings(livekit_api_key="lk-key", livekit_api_secret=LIVEKIT_SECRET)


def _room_call(org_id: uuid.UUID, *, status: str = "answered") -> Call:
    return Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="telnyx",
        status=status,
        extra={"via": "livekit", "room": "call-abc"},
    )


def _decode(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})


async def _make_org(session) -> uuid.UUID:
    """A real Org row - Call/CallLeg/VoiceEvent are all TenantScoped with a genuine FK to
    orgs.id (SQLite enforces it here - conftest.py's PRAGMA foreign_keys=ON)."""
    org = Org(id=uuid.uuid4(), name="Test Org", slug=f"org-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()
    return org.id


async def test_monitor_token_cannot_publish(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _room_call(org_id)
    session.add(call)
    await session.flush()
    settings = _settings()

    token = await supervisor_svc.monitor(
        session, settings, call, identity="supervisor-1", name="Sup One"
    )
    claims = _decode(token, LIVEKIT_SECRET)
    assert claims["video"]["canPublish"] is False
    assert claims["video"]["canSubscribe"] is True
    assert claims["video"]["room"] == "call-abc"

    events = (await session.execute(sa.select(VoiceEventRow))).scalars().all()
    assert any(e.event_type == "supervisor.monitor" for e in events)


async def test_whisper_raises_feature_unavailable(session, engine):
    """B7 (verified against the live LiveKit server): RoomService has no server-side
    subscription-permission API, so whisper cannot be honestly enforced here - it must
    raise rather than mint a token that would silently behave like barge. No VoiceEvent
    is recorded either, since the action never actually happened."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _room_call(org_id)
    session.add(call)
    await session.flush()
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id=CALLER_SIP_IDENTITY,
        to_e164=call.our_e164,
        from_e164=call.contact_e164,
        status="answered",
        reason="original",
        extra={"sip_identity": CALLER_SIP_IDENTITY},
    )
    session.add(leg)
    await session.flush()
    settings = _settings()
    fake_api = FakeLiveKitApi()

    with pytest.raises(FeatureUnavailableError):
        await supervisor_svc.whisper(
            session, settings, fake_api, call, identity="supervisor-1", name="Sup One"
        )

    assert fake_api.update_subscriptions_calls == []
    events = (await session.execute(sa.select(VoiceEventRow))).scalars().all()
    assert not any(e.event_type == "supervisor.whisper" for e in events)


async def test_whisper_raises_feature_unavailable_even_with_no_livekit_api_configured(
    session, engine
):
    """No live LiveKit configured (api=None) - whisper is unavailable either way, so the
    call-state validation still runs first (a bad call still 404s/409s) but the outcome
    is the same FeatureUnavailableError, not a token."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _room_call(org_id)
    session.add(call)
    await session.flush()
    settings = _settings()

    with pytest.raises(FeatureUnavailableError):
        await supervisor_svc.whisper(
            session, settings, None, call, identity="supervisor-1", name="Sup One"
        )


async def test_whisper_still_validates_the_call_before_declaring_unavailable(session, engine):
    """A carrier-path (non-room) call is refused for the call-state reason, not the
    whisper-unavailable one - the call-state check still runs first, so the error message
    tells an operator WHY (wrong call type) rather than always saying "not implemented"."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    non_room_call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="bandwidth",
        status="answered",
        extra={},
    )
    session.add(non_room_call)
    await session.flush()

    with pytest.raises(FeatureUnavailableError, match="LiveKit room call"):
        await supervisor_svc.whisper(
            session, _settings(), None, non_room_call, identity="supervisor-1", name="Sup One"
        )


async def test_barge_full_token(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    call = _room_call(org_id)
    session.add(call)
    await session.flush()
    settings = _settings()

    token = await supervisor_svc.barge(
        session, settings, call, identity="supervisor-1", name="Sup One"
    )
    claims = _decode(token, LIVEKIT_SECRET)
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True

    events = (await session.execute(sa.select(VoiceEventRow))).scalars().all()
    assert any(e.event_type == "supervisor.barge" for e in events)


async def test_monitor_refuses_a_carrier_path_call(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    non_room_call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="bandwidth",
        status="answered",
        extra={},
    )
    session.add(non_room_call)
    await session.flush()
    with pytest.raises(FeatureUnavailableError):
        await supervisor_svc.monitor(
            session, _settings(), non_room_call, identity="supervisor-1", name="Sup One"
        )


async def test_monitor_refuses_a_call_that_already_ended(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    ended_call = _room_call(org_id, status="completed")
    session.add(ended_call)
    await session.flush()
    with pytest.raises(ConflictError):
        await supervisor_svc.monitor(
            session, _settings(), ended_call, identity="supervisor-1", name="Sup One"
        )


# --------------------------------------------------------------------------------------
# RBAC (via the actual HTTP route): agent role denied, owner allowed.
#
# Needs a client wired with LiveKit configured (mint_access_token needs a non-empty HMAC
# key), so this uses its own local fixture rather than conftest's plain `client`.
# --------------------------------------------------------------------------------------
@pytest.fixture
async def livekit_client(engine):
    application = create_app(_settings())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_room_call(org_id: uuid.UUID) -> str:
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as db_session:
        set_org_context(db_session, org_id)
        call = _room_call(org_id)
        db_session.add(call)
        await db_session.commit()
        return str(call.id)


async def test_agent_role_is_rbac_denied_from_supervisor_routes(livekit_client, session):
    owner_token = await register_and_login(livekit_client, "sup-owner@example.com")
    org = await create_org(livekit_client, owner_token, "Org Sup")
    org_id = uuid.UUID(org["id"])
    call_id = await _seed_room_call(org_id)

    agent_token = await register_and_login(livekit_client, "sup-agent@example.com")
    from app.repositories import users as users_repo

    agent_user = await users_repo.get_by_email(session, "sup-agent@example.com")
    set_org_context(session, org_id)
    agent_role = (await session.execute(sa.select(Role).where(Role.name == "agent"))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=agent_user.id, role_id=agent_role.id)
    )
    await session.commit()

    r = await livekit_client.post(
        f"/api/v1/calls/{call_id}/monitor", json={}, headers=auth_headers(agent_token, org["id"])
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"


async def test_owner_can_reach_monitor_route(livekit_client):
    owner_token = await register_and_login(livekit_client, "sup-owner2@example.com")
    org = await create_org(livekit_client, owner_token, "Org Sup 2")
    org_id = uuid.UUID(org["id"])
    call_id = await _seed_room_call(org_id)

    r = await livekit_client.post(
        f"/api/v1/calls/{call_id}/monitor", json={}, headers=auth_headers(owner_token, org["id"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["room"] == "call-abc"
