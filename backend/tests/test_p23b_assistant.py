"""P23b assistant dispatch, per-call worker tokens, voice preview, Call me, and BYOK."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    AgentProfile,
    AiProviderAccount,
    AuditLogEntry,
    Call,
    CallFlow,
    CallScore,
    Org,
    OrgNumber,
)
from app.services import agent as agent_svc
from app.services import ai_providers as ai_providers_svc
from app.services import voice_preview
from app.storage.base import InMemoryObjectStore
from tests.conftest import auth_headers, make_org_with_number
from tests.test_agent_seams import app_with_agent  # noqa: F401 - pytest fixture by name
from tests.test_agent_seams import (
    _place_call,
    make_agent_settings,
    worker_headers,
    worker_token,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

_NUMBER_SEQ = iter(range(20, 89))


def _next_number() -> str:
    """org_numbers.e164 is globally unique - every org in this file needs its own, and it
    has to be a REAL, parseable NANP number (the route validates with phonenumbers)."""
    return f"+12145550{next(_NUMBER_SEQ)}0"


class FakeLiveKitApi:
    def __init__(self) -> None:
        self.rooms: list[str] = []
        self.sip_participants: list[dict] = []
        self.dispatches: list[dict] = []
        self.deleted_rooms: list[str] = []

    async def create_room(self, room: str) -> None:
        self.rooms.append(room)

    async def create_sip_participant(self, **kwargs) -> dict:
        self.sip_participants.append(kwargs)
        return {"participant_id": "p"}

    async def create_agent_dispatch(
        self, *, room: str, agent_name: str, metadata: str = ""
    ) -> dict:
        self.dispatches.append({"room": room, "agent_name": agent_name, "metadata": metadata})
        return {"dispatch": True}

    async def delete_room(self, room: str) -> None:
        self.deleted_rooms.append(room)


@pytest.fixture
async def app_with_assistant(engine):
    settings = make_agent_settings(livekit_sip_outbound_trunk_id="trunk-test")
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    api = FakeLiveKitApi()
    application.state.livekit = api
    application.state.media_store = InMemoryObjectStore()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, application, api


async def test_per_call_worker_token_is_bound_to_its_call(app_with_assistant):
    """A token minted for call A is accepted for A and refused (401) for call B."""
    client, application, _api = app_with_assistant
    settings = application.state.settings
    _token, org_a, call_a = await _place_call(client, "tok-a@example.com", "Org Token A")
    _token_b, _org_b, call_b = await _place_call(
        client, "tok-b@example.com", "Org Token B", e164="+12145550101"
    )

    call_token = agent_svc.mint_call_worker_token(
        settings, call_id=uuid.UUID(call_a), org_id=uuid.UUID(org_a["id"])
    )
    ok = await client.get(f"/api/v1/agent/config/{call_a}", headers=worker_headers(call_token))
    assert ok.status_code == 200, ok.text

    stolen = await client.get(f"/api/v1/agent/config/{call_b}", headers=worker_headers(call_token))
    assert stolen.status_code == 401


async def test_legacy_global_worker_token_is_refused_by_the_config_seam_but_not_the_transcript_seam(
    app_with_assistant,
):
    """The config seam now requires a call-bound token; the transcript seam stays global."""
    client, _application, _api = app_with_assistant
    _token, _org, call_id = await _place_call(client, "legacy@example.com", "Org Legacy")

    global_token = worker_token()
    config = await client.get(
        f"/api/v1/agent/config/{call_id}", headers=worker_headers(global_token)
    )
    assert config.status_code == 401

    transcript = await client.post(
        "/api/v1/agent/transcript",
        json={"call_id": call_id, "segments": [{"role": "user", "text": "hi", "at_ms": 0}]},
        headers=worker_headers(global_token),
    )
    assert transcript.status_code == 200


async def test_expired_call_token_is_refused(app_with_assistant):
    """A call-bound token with an exp in the past must be rejected by the config seam."""
    client, application, _api = app_with_assistant
    settings = application.state.settings
    _token, org, call_id = await _place_call(client, "expired@example.com", "Org Expired")

    call_token = agent_svc.mint_call_worker_token(
        settings,
        call_id=uuid.UUID(call_id),
        org_id=uuid.UUID(org["id"]),
        ttl_seconds=-10,
    )
    r = await client.get(f"/api/v1/agent/config/{call_id}", headers=worker_headers(call_token))
    assert r.status_code == 401


async def test_resolve_call_profile_prefers_the_dispatch_marker(session):
    """A call marked for profile X resolves to X even when a DIFFERENT profile is the
    org default."""
    # agent_profiles.org_id is a real FK and conftest turns SQLite's foreign_keys ON.
    org = Org(id=uuid.uuid4(), name="Marker Org", slug=f"marker-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    org_id = org.id
    set_org_context(session, org_id)

    profile_x = AgentProfile(id=uuid.uuid4(), org_id=org_id, name="X")
    profile_default = AgentProfile(id=uuid.uuid4(), org_id=org_id, name="Default", is_default=True)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="telnyx",
        status="queued",
        extra={"assistant": {"profile_id": str(profile_x.id)}},
    )
    session.add_all([profile_x, profile_default, call])
    await session.commit()

    resolved = await agent_svc.resolve_call_profile(session, call)
    assert resolved is not None
    assert resolved.id == profile_x.id


async def test_resolve_call_profile_uses_the_numbers_flow_for_inbound(
    app_with_assistant, session, monkeypatch
):
    """An inbound call on a number bound to an assistant flow resolves to that flow's
    profile; the same call on a ring flow falls back to the org default."""
    client, _application, _api = app_with_assistant
    _token, org, number = await make_org_with_number(
        client, "flow-profile@example.com", "Org Flow", "+12145550100"
    )
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    profile_flow = AgentProfile(id=uuid.uuid4(), org_id=org_id, name="Flow Assistant")
    profile_default = AgentProfile(
        id=uuid.uuid4(), org_id=org_id, name="Default", is_default=True
    )
    session.add_all([profile_flow, profile_default])
    await session.commit()

    # Real definitions: the entry node is what decides, so the shape has to be honest.
    assistant_flow = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Answer",
        version=1,
        status="active",
        definition={
            "entry": "assistant",
            "nodes": {
                "assistant": {"type": "assistant", "profile_id": str(profile_flow.id)}
            },
        },
    )
    ring_flow = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name="Ring",
        version=1,
        status="active",
        definition={
            "entry": "hangup",
            "nodes": {"hangup": {"type": "hangup"}},
        },
    )
    session.add_all([assistant_flow, ring_flow])
    await session.flush()

    number_id = uuid.UUID(number["id"])
    number_row = await session.get(OrgNumber, number_id)
    number_row.call_flow_id = assistant_flow.id
    await session.commit()

    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="telnyx",
        status="queued",
        extra={"via": "livekit", "room": "call-test"},
    )
    session.add(call)
    await session.commit()

    resolved_assistant = await agent_svc.resolve_call_profile(session, call)
    assert resolved_assistant is not None
    assert resolved_assistant.id == profile_flow.id

    number_row.call_flow_id = ring_flow.id
    await session.commit()

    # The ring flow's entry node is not an assistant node, so the number falls back to the
    # org default exactly as it did before P23b - the human ring path is unchanged.
    session.expire(number_row)
    resolved_human_ring = await agent_svc.resolve_call_profile(session, call)
    assert resolved_human_ring is not None
    assert resolved_human_ring.id == profile_default.id


async def test_voice_preview_is_cached_per_provider_voice_and_text(session, monkeypatch):
    """A fake upstream counts ONE call for two identical previews and TWO for different
    text; the api key never appears in a failure message."""
    upstream_calls: list[str] = []

    async def handler(request):
        upstream_calls.append(str(request.url))
        return httpx.Response(200, content=b"mp3-bytes", headers={"content-type": "audio/mpeg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_resolve_call_config(session, settings, *, org, profile, include_keys):
        return {
            "tts": {"provider": "elevenlabs", "model": "eleven_turbo_v2_5", "voice_id": "voice-1"},
            "keys": {"tts": "secret-api-key"},
        }

    monkeypatch.setattr(ai_providers_svc, "resolve_call_config", fake_resolve_call_config)
    store = InMemoryObjectStore()
    org = SimpleNamespace(id=uuid.uuid4())
    profile = SimpleNamespace()

    audio1 = await voice_preview.preview(
        session,
        None,
        store,
        org=org,
        profile=profile,
        tts_provider="",
        voice_id="voice-1",
        text="Hello",
        client=client,
    )
    audio2 = await voice_preview.preview(
        session,
        None,
        store,
        org=org,
        profile=profile,
        tts_provider="",
        voice_id="voice-1",
        text="Hello",
        client=client,
    )
    audio3 = await voice_preview.preview(
        session,
        None,
        store,
        org=org,
        profile=profile,
        tts_provider="",
        voice_id="voice-1",
        text="Different",
        client=client,
    )
    assert len(upstream_calls) == 2
    assert audio1 == audio2 == audio3 == b"mp3-bytes"
    assert voice_preview.PREVIEW_CONTENT_TYPE == "audio/mpeg"

    async def fail_handler(request):
        return httpx.Response(400, text="provider says secret-api-key is bad")

    fail_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handler))
    with pytest.raises(Exception) as exc_info:
        await voice_preview.preview(
            session,
            None,
            InMemoryObjectStore(),
            org=org,
            profile=profile,
            tts_provider="",
            voice_id="voice-1",
            text="fail",
            client=fail_client,
        )
    assert "secret-api-key" not in str(exc_info.value)
    await client.aclose()
    await fail_client.aclose()


async def test_voice_preview_without_a_voice_connection_is_422_not_500(app_with_assistant):
    """A missing TTS key is a plain 422, not an unhandled 500."""
    client, _application, _api = app_with_assistant
    token, org, _number = await make_org_with_number(
        client, "novoice@example.com", "Org NoVoice", _next_number()
    )
    r = await client.post(
        "/api/v1/agent/voices/preview",
        json={"voice_id": "voice-1"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code != 500, r.text
    assert r.status_code == 422


async def test_call_me_places_a_test_call_and_flags_it(app_with_assistant, session, monkeypatch):
    """Call me creates a room call, stamps the assistant marker with is_test true, and
    creates a call_scores row with is_test true."""
    client, _application, api = app_with_assistant
    token, org, _number = await make_org_with_number(
        client, "callme@example.com", "Org CallMe", _next_number()
    )
    # "Call me" dials over the LiveKit SIP trunk, which has exactly one carrier today
    # (routes/calls.py::_ROOM_TRUNK_CARRIER). make_org_with_number registers on the
    # deployment's primary, so move this number onto the trunk carrier.
    set_org_context(session, uuid.UUID(org["id"]))
    number_row = await session.get(OrgNumber, uuid.UUID(_number["id"]))
    number_row.carrier = "telnyx"
    await session.commit()

    created = await client.post(
        "/api/v1/agent/profiles",
        json={"name": "Call Me Assistant"},
        headers=auth_headers(token, org["id"]),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    async def fake_ready(*args, **kwargs):
        return {"ready": True}

    async def fake_apply_outcome(session, call, payload, *, profile=None):
        session.add(
            CallScore(
                id=uuid.uuid4(),
                org_id=call.org_id,
                call_id=call.id,
                is_test=payload.get("is_test", False),
            )
        )
        return {"ok": True}

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_ready)
    monkeypatch.setattr(agent_svc, "apply_outcome", fake_apply_outcome)

    r = await client.post(
        f"/api/v1/agent/profiles/{profile_id}/call-me",
        json={"to_e164": "+19725550199"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_test"] is True

    from app.voice_plane import service as voice_service

    await voice_service.wait_for_pending_dial_tasks()

    set_org_context(session, uuid.UUID(org["id"]))
    call_id = uuid.UUID(body["call_id"])
    call = await session.get(Call, call_id)
    assert call is not None
    assert call.extra["assistant"]["is_test"] is True
    assert call.extra["assistant"]["profile_id"] == profile_id

    score = (
        await session.execute(sa.select(CallScore).where(CallScore.call_id == call_id))
    ).scalar_one_or_none()
    assert score is not None
    assert score.is_test is True


async def test_call_me_without_livekit_is_a_plain_error(app_with_agent, monkeypatch):
    """Call me without LiveKit configured is a feature-unavailable error, not a 500."""
    client, _application = app_with_agent
    token, org, _number = await make_org_with_number(
        client, "nolive@example.com", "Org NoLive", _next_number()
    )
    created = await client.post(
        "/api/v1/agent/profiles",
        json={"name": "No LiveKit"},
        headers=auth_headers(token, org["id"]),
    )
    profile_id = created.json()["id"]

    async def fake_ready(*args, **kwargs):
        return {"ready": True}

    monkeypatch.setattr(agent_svc, "go_live_readiness", fake_ready)

    r = await client.post(
        f"/api/v1/agent/profiles/{profile_id}/call-me",
        json={"to_e164": "+19725550199"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code != 500, r.text


async def test_kb_document_create_and_delete_write_audit_rows(app_with_assistant, session):
    """Create and delete each write exactly one targeted knowledge audit row."""
    client, _application, _api = app_with_assistant
    token, org, _number = await make_org_with_number(
        client, "kb-audit@example.com", "Org Kb Audit", _next_number()
    )
    headers = auth_headers(token, org["id"])

    created = await client.post(
        "/api/v1/agent/kb/documents",
        json={"text": "hello world", "title": "Doc One"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    doc_id = created.json()["id"]

    set_org_context(session, uuid.UUID(org["id"]))
    created_audit = (
        await session.execute(
            sa.select(AuditLogEntry).where(
                AuditLogEntry.action == "knowledge.document.created",
                AuditLogEntry.target_id == str(doc_id),
            )
        )
    ).scalar_one_or_none()
    assert created_audit is not None
    assert created_audit.detail["title"] == "Doc One"

    deleted = await client.delete(f"/api/v1/agent/kb/documents/{doc_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    deleted_audit = (
        await session.execute(
            sa.select(AuditLogEntry).where(
                AuditLogEntry.action == "knowledge.document.deleted",
                AuditLogEntry.target_id == str(doc_id),
            )
        )
    ).scalar_one_or_none()
    assert deleted_audit is not None
    assert deleted_audit.detail["title"] == "Doc One"


async def test_byok_resolves_the_account_matching_the_profiles_provider(
    app_with_assistant, session, monkeypatch
):
    """An org with TWO active language-model connections resolves the one the profile
    names, and the other connection's key is never returned."""
    client, _application, _api = app_with_assistant
    _token, org, _number = await make_org_with_number(
        client, "byok@example.com", "Org BYOK", _next_number()
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    org_row = await session.get(Org, org_id)
    org_row.ai_key_mode = "byok"

    session.add_all(
        [
            AiProviderAccount(
                id=uuid.uuid4(),
                org_id=org_id,
                kind="llm",
                provider="openai",
                status="active",
                credentials_encrypted="enc:openai",
            ),
            AiProviderAccount(
                id=uuid.uuid4(),
                org_id=org_id,
                kind="llm",
                provider="anthropic",
                status="active",
                credentials_encrypted="enc:anthropic",
            ),
        ]
    )
    profile = AgentProfile(
        id=uuid.uuid4(), org_id=org_id, name="Byok Anthropic", llm_provider="anthropic"
    )
    session.add(profile)
    await session.commit()

    def fake_decrypt(settings, credentials_encrypted):
        if "anthropic" in credentials_encrypted:
            return {"api_key": "sk-anthropic"}
        return {"api_key": "sk-openai"}

    monkeypatch.setattr(ai_providers_svc.credential_svc, "decrypt", fake_decrypt)

    cfg = await ai_providers_svc.resolve_call_config(
        session,
        _application.state.settings,
        org=org_row,
        profile=profile,
        include_keys=True,
    )
    assert cfg["llm"]["provider"] == "anthropic"
    assert cfg["keys"]["llm"] == "sk-anthropic"
    assert cfg["keys"]["llm"] != "sk-openai"


async def test_voice_preview_refuses_a_provider_the_org_has_not_connected(session):
    """Naming a provider the org has no connection for must NOT send the connected
    provider's key to it - the same class of mistake D47 fixed in resolve_call_config."""
    org = Org(id=uuid.uuid4(), name="Wrong Vendor", slug=f"wv-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)
    profile = AgentProfile(id=uuid.uuid4(), org_id=org.id, name="V", tts_provider="")
    session.add(profile)
    await session.commit()

    settings = make_agent_settings(elevenlabs_api_key="eleven-key", elevenlabs_voice_id="v1")
    store = InMemoryObjectStore()

    upstream: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(str(request.url))
        return httpx.Response(200, content=b"never")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception) as exc:
            await voice_preview.preview(
                session,
                settings,
                store,
                org=org,
                profile=profile,
                tts_provider="cartesia",
                voice_id="v1",
                text="Hello",
                client=client,
            )
    assert "not connected" in str(exc.value)
    assert upstream == []
    assert "eleven-key" not in str(exc.value)
