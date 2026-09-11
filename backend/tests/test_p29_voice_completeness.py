"""P29 voice completeness: business hours, flow activation, call dispositions, callback
queue, dual-channel recording, consent announcements, and coaching privacy.

The two tests worth reading twice are
``test_stitch_produces_two_channels_and_never_downmixes`` (it proves the two recordings
are interleaved, never summed) and
``test_a_customer_leg_subscribing_to_a_coaching_track_is_removed_and_audited`` (it proves
the webhook path both force-unsubscribes a leaked PSTN leg and writes the security audit).
"""

from __future__ import annotations

import base64
import io
import uuid
import wave
from array import array
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes.webhooks import _outbound_answer_commands
from app.db.base import set_org_context
from app.errors import FeatureUnavailableError, ValidationFailedError
from app.main import create_app
from app.models import (
    AuditLogEntry,
    BusinessHours,
    Call,
    CallFlow,
    CallQueue,
    CallRecording,
    Org,
    OrgMembership,
    OrgNumber,
    QueueEntry,
    RingGroupDef,
    Role,
    VoiceEvent,
)
from app.providers import voice
from app.providers.bandwidth.adapter import BandwidthMessagingCarrier
from app.providers.numbers import NumberSearch
from app.repositories import users as users_repo
from app.services import calling_settings as calling_settings_svc
from app.services import flows as flows_svc
from app.services import recordings as recordings_svc
from app.services import routing_exec as routing_exec_svc
from app.services import supervisor as supervisor_svc
from tests.conftest import (
    FakeCarrier,
    _install,
    auth_headers,
    create_org,
    make_org_with_number,
    make_settings,
    register_and_login,
)

pytestmark = pytest.mark.asyncio

PUBLIC_API = "https://api.test.example"
DEFAULT_ANNOUNCEMENT = calling_settings_svc.DEFAULT_ANNOUNCEMENT_TEXT
VALID_FLOW = {
    "entry": "hello",
    "nodes": {
        "hello": {"type": "speak", "text": "Hi", "next": "bye"},
        "bye": {"type": "hangup"},
    },
}


class FakeLiveKit:
    def __init__(self, participants):
        self.participants = participants
        self.unsubscribed = []

    async def list_participants(self, room):
        return self.participants

    async def update_subscriptions(self, *, room, identity, track_sids, subscribe):
        self.unsubscribed.append((identity, tuple(track_sids), subscribe))
        return {}


@pytest.fixture
async def p29_app(engine):
    settings = make_settings(public_base_url=PUBLIC_API)
    application = create_app(settings)
    fake = FakeCarrier()
    _install(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


def _mono_wav(samples: list[int], *, framerate: int = 8000, sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(sampwidth)
        wav.setframerate(framerate)
        wav.writeframes(array("h", samples).tobytes())
    return buf.getvalue()


def _stereo_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(array("h", [0] * 4000).tobytes())
    return buf.getvalue()


async def _org(client: httpx.AsyncClient, slug: str) -> tuple[str, dict, uuid.UUID]:
    token = await register_and_login(client, f"p29-{slug}@example.com")
    org = await create_org(client, token, f"Org {slug}")
    return token, org, uuid.UUID(org["id"])


async def _org_with_number(
    client: httpx.AsyncClient, slug: str, e164: str
) -> tuple[str, dict, uuid.UUID, dict]:
    token, org, number = await make_org_with_number(
        client, f"p29-{slug}@example.com", f"Org {slug}", e164
    )
    return token, org, uuid.UUID(org["id"]), number


async def _register_member(
    client: httpx.AsyncClient,
    session,
    org_id: uuid.UUID,
    email: str,
    role_name: str = "agent",
) -> str:
    token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    set_org_context(session, org_id)
    role = (await session.execute(sa.select(Role).where(Role.name == role_name))).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return token


async def _add_call(
    session,
    org_id: uuid.UUID,
    *,
    direction: str = "inbound",
    contact: str = "+12145550100",
    our: str = "+12145550101",
    status: str = "answered",
    extra: dict | None = None,
) -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction=direction,
        contact_e164=contact,
        our_e164=our,
        carrier="bandwidth",
        status=status,
        extra=extra or {},
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call


async def _set_announcement(session, org_id: uuid.UUID, enabled: bool) -> None:
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.recording_announcement = enabled
    await session.commit()


async def _seed_ring_flow(session, org_id: uuid.UUID) -> CallFlow:
    set_org_context(session, org_id)
    rg = RingGroupDef(
        id=uuid.uuid4(),
        org_id=org_id,
        name=str(uuid.uuid4()),
        ring_timeout_seconds=20,
    )
    flow = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name=str(uuid.uuid4()),
        version=1,
        status="active",
        definition={
            "entry": "ring",
            "nodes": {
                "ring": {
                    "type": "ring_group",
                    "ring_group_id": str(rg.id),
                    "no_answer": "bye",
                },
                "bye": {"type": "hangup"},
            },
        },
    )
    session.add_all([rg, flow])
    await session.commit()
    await session.refresh(flow)
    return flow


# ---- Business hours (D18 regression) -------------------------------------------
async def test_overnight_hours_span_midnight():
    chicago = ZoneInfo("America/Chicago")
    bh = BusinessHours(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="overnight",
        timezone="America/Chicago",
        schedule={"mon": [["22:00", "06:00"]]},
        holidays=[],
    )
    open_tue = datetime(2026, 6, 16, 2, 0, tzinfo=chicago).astimezone(timezone.utc)
    closed_mon = datetime(2026, 6, 15, 12, 0, tzinfo=chicago).astimezone(timezone.utc)
    open_mon = datetime(2026, 6, 15, 23, 0, tzinfo=chicago).astimezone(timezone.utc)

    assert flows_svc.evaluate_hours(bh, open_tue) == "open"
    assert flows_svc.evaluate_hours(bh, closed_mon) == "closed"
    assert flows_svc.evaluate_hours(bh, open_mon) == "open"


async def test_business_hours_rejects_an_unknown_weekday_key(session):
    with pytest.raises(ValidationFailedError):
        await flows_svc.create_business_hours(
            session,
            uuid.uuid4(),
            name="bad",
            timezone_name="America/Chicago",
            schedule={"monday": [["09:00", "17:00"]]},
            holidays=[],
        )


# ---- Flow activation (D17) -----------------------------------------------------
async def test_activate_flow_repoints_numbers_to_the_new_version(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id, number = await _org_with_number(
        client, "flow-activate", "+12145550120"
    )
    set_org_context(session, org_id)

    v1 = await flows_svc.create_flow(session, org_id, name="sales", definition=VALID_FLOW)
    await flows_svc.activate_flow(session, org_id, v1.id)
    number_id = uuid.UUID(number["id"])
    await flows_svc.bind_number(session, org_id, number_id, v1.id)

    v2 = await flows_svc.create_version(
        session, org_id, flow_id=v1.id, definition=VALID_FLOW
    )
    await flows_svc.activate_flow(session, org_id, v2.id)

    await session.refresh(v1)
    number_row = await session.get(OrgNumber, number_id)
    assert number_row.call_flow_id == v2.id
    assert v1.status == "archived"


async def test_resolve_inbound_flow_returns_the_active_version_when_the_pinned_row_is_archived(
    p29_app, session
):
    client, _fake, _app = p29_app
    e164 = "+12145550121"
    token, org, org_id, number = await _org_with_number(client, "flow-resolve", e164)
    set_org_context(session, org_id)

    v1 = await flows_svc.create_flow(
        session, org_id, name="support", definition=VALID_FLOW
    )
    await flows_svc.activate_flow(session, org_id, v1.id)
    await flows_svc.bind_number(session, org_id, uuid.UUID(number["id"]), v1.id)

    v2 = await flows_svc.create_version(
        session, org_id, flow_id=v1.id, definition=VALID_FLOW
    )
    await flows_svc.activate_flow(session, org_id, v2.id)

    number_row = await session.get(OrgNumber, uuid.UUID(number["id"]))
    number_row.call_flow_id = v1.id
    await session.commit()

    resolved = await routing_exec_svc.resolve_inbound_flow(session, e164)
    assert resolved is not None
    assert resolved.id == v2.id


async def test_resolve_inbound_flow_returns_none_when_no_version_is_active(p29_app, session):
    client, _fake, _app = p29_app
    e164 = "+12145550122"
    token, org, org_id, number = await _org_with_number(client, "flow-none", e164)
    set_org_context(session, org_id)

    flow = await flows_svc.create_flow(session, org_id, name="old", definition=VALID_FLOW)
    await flows_svc.activate_flow(session, org_id, flow.id)
    await flows_svc.bind_number(session, org_id, uuid.UUID(number["id"]), flow.id)

    flow = await session.get(CallFlow, flow.id)
    flow.status = "archived"
    await session.commit()

    assert await routing_exec_svc.resolve_inbound_flow(session, e164) is None


# ---- Calling settings + call results (dispositions) ----------------------------
async def test_calling_settings_defaults(p29_app):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-defaults")

    r = await client.get(
        "/api/v1/orgs/current/calling", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recording_announcement"] is False
    assert body["recording_announcement_text"] is None
    assert body["channel_layout"] == "mixed"
    assert body["dispositions"] == list(calling_settings_svc.DEFAULT_DISPOSITIONS)
    assert body["announcement_text_effective"] == DEFAULT_ANNOUNCEMENT


async def test_dispositions_list_is_configurable(p29_app):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-dispositions")
    headers = auth_headers(token, org_id)
    dispositions = ["Deal won", "Deal lost"]

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"dispositions": dispositions},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispositions"] == dispositions

    r2 = await client.get("/api/v1/orgs/current/calling", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["dispositions"] == dispositions


async def test_dispositions_reject_duplicates_and_overlong_entries(p29_app):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-dispo-invalid")
    headers = auth_headers(token, org_id)

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"dispositions": ["Same", "same"]},
        headers=headers,
    )
    assert r.status_code == 422, r.text

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"dispositions": ["A" * 40]},
        headers=headers,
    )
    assert r.status_code == 422, r.text

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"dispositions": []},
        headers=headers,
    )
    assert r.status_code == 422, r.text


async def test_channel_layout_must_be_mixed_or_dual(p29_app):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-layout")
    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"channel_layout": "stereo"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 422, r.text


async def test_announcement_text_can_be_set_and_cleared(p29_app):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-announce")
    headers = auth_headers(token, org_id)

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"recording_announcement_text": "  Custom announce  "},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recording_announcement_text"] == "Custom announce"
    assert body["announcement_text_effective"] == "Custom announce"

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"recording_announcement_text": None},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recording_announcement_text"] is None
    assert body["announcement_text_effective"] == DEFAULT_ANNOUNCEMENT


async def test_disposition_is_saved_and_shows_on_the_call(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-disposition-save")
    call = await _add_call(session, org_id)
    headers = auth_headers(token, org_id)

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested", "note": " left a message "},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disposition"] == "Interested"
    assert body["disposition_note"] == "left a message"

    r = await client.get("/api/v1/calls", params={"disposition": "Interested"},
                         headers=headers)
    assert r.status_code == 200, r.text
    assert str(call.id) in {row["id"] for row in r.json()}

    r = await client.get("/api/v1/calls", params={"disposition": "Callback"},
                         headers=headers)
    assert r.status_code == 200, r.text
    assert str(call.id) not in {row["id"] for row in r.json()}


async def test_disposition_must_be_on_the_configured_list(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-disposition-config")
    call = await _add_call(session, org_id)
    headers = auth_headers(token, org_id)

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"dispositions": ["Deal won"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested"},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "not on this workspace's list" in r.text.lower()


async def test_disposition_can_be_cleared_and_a_bare_note_is_refused(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-disposition-clear")
    call = await _add_call(session, org_id)
    headers = auth_headers(token, org_id)

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": None},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disposition"] is None
    assert body["disposition_note"] is None

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"note": "x"},
        headers=headers,
    )
    assert r.status_code == 422, r.text


async def test_disposition_patch_requires_an_inbox_grant(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "call-disposition-grant")
    call = await _add_call(session, org_id)

    agent_token = await _register_member(
        client, session, org_id, "p29-agent-disposition@example.com", "agent"
    )
    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested"},
        headers=auth_headers(agent_token, org_id),
    )
    assert r.status_code == 404, r.text

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text


async def test_tenancy_a_call_in_another_org_is_not_patchable(p29_app, session):
    client, _fake, _app = p29_app
    token_a, org_a, org_a_id = await _org(client, "tenant-a")
    call = await _add_call(session, org_a_id)

    token_b, org_b, org_b_id = await _org(client, "tenant-b")
    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested"},
        headers=auth_headers(token_b, org_b_id),
    )
    assert r.status_code == 404, r.text

    r = await client.patch(
        f"/api/v1/calls/{call.id}/disposition",
        json={"disposition": "Interested"},
        headers=auth_headers(token_a, org_a_id),
    )
    assert r.status_code == 200, r.text


# ---- Callback queue ------------------------------------------------------------
async def test_callback_queue_lists_missed_calls_with_the_number_to_call_back(
    p29_app, session
):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "callback-list")
    set_org_context(session, org_id)

    queue = CallQueue(id=uuid.uuid4(), org_id=org_id, name="support")
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164="+12145550110",
        our_e164="+12145550111",
        carrier="bandwidth",
        status="no_answer",
    )
    entry = QueueEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        queue_id=queue.id,
        call_id=call.id,
        state="callback_requested",
        callback_e164="+12145550112",
        enqueued_at=datetime.now(timezone.utc),
        resolved_at=None,
    )
    session.add_all([queue, call])
    await session.flush()
    session.add(entry)
    await session.commit()

    headers = auth_headers(token, org_id)
    r = await client.get("/api/v1/calls/callbacks", headers=headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["callback_e164"] == "+12145550112"
    assert row["our_e164"] == "+12145550111"
    assert row["contact_e164"] == "+12145550110"
    assert row["dialing"] is False

    set_org_context(session, org_id)
    entry = await session.get(QueueEntry, entry.id)
    entry.dial_now_claimed_at = datetime.now(timezone.utc)
    await session.commit()

    r = await client.get("/api/v1/calls/callbacks", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()[0]["dialing"] is True


async def test_callback_queue_is_scoped_to_the_org(p29_app, session):
    client, _fake, _app = p29_app
    token_a, org_a, org_a_id = await _org(client, "callback-scope-a")
    set_org_context(session, org_a_id)

    queue = CallQueue(id=uuid.uuid4(), org_id=org_a_id, name="support")
    call = Call(
        id=uuid.uuid4(),
        org_id=org_a_id,
        direction="inbound",
        contact_e164="+12145550130",
        our_e164="+12145550131",
        carrier="bandwidth",
        status="no_answer",
    )
    entry = QueueEntry(
        id=uuid.uuid4(),
        org_id=org_a_id,
        queue_id=queue.id,
        call_id=call.id,
        state="callback_requested",
        callback_e164="+12145550132",
        enqueued_at=datetime.now(timezone.utc),
        resolved_at=None,
    )
    session.add_all([queue, call])
    await session.flush()
    session.add(entry)
    await session.commit()

    token_b, org_b, org_b_id = await _org(client, "callback-scope-b")
    r = await client.get(
        "/api/v1/calls/callbacks", headers=auth_headers(token_b, org_b_id)
    )
    assert r.status_code == 200, r.text
    assert r.json() == []

    r = await client.get(
        "/api/v1/calls/callbacks", headers=auth_headers(token_a, org_a_id)
    )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


# ---- Dual-channel recording ------------------------------------------------------
async def test_stitch_produces_two_channels_and_never_downmixes():
    agent = _mono_wav([1000] * 4000)
    customer = _mono_wav([-1000] * 2000)

    stitched = recordings_svc.stitch_dual_channel(agent, customer)
    with wave.open(io.BytesIO(stitched), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 8000
        assert wav.getnframes() == 4000
        frames = wav.readframes(wav.getnframes())

    samples = array("h", frames)
    assert samples[0] == 1000
    assert samples[1] == -1000

    min_frames = min(4000, 2000)
    assert all(sample != 0 for sample in samples[: min_frames * 2])

    tail_start = 2000 * 2
    assert all(samples[tail_start + 1 + 2 * i] == 0 for i in range(2000))


async def test_stitch_refuses_mismatched_formats():
    a8 = _mono_wav([1000] * 100, framerate=8000)
    a16 = _mono_wav([1000] * 100, framerate=16000)
    with pytest.raises(ValidationFailedError):
        recordings_svc.stitch_dual_channel(a8, a16)

    stereo = _stereo_wav()
    mono = _mono_wav([1000] * 100)
    with pytest.raises(ValidationFailedError):
        recordings_svc.stitch_dual_channel(stereo, mono)


async def test_finalize_dual_recording_stores_three_files_and_the_download_offers_them_all(
    p29_app, session
):
    client, _fake, application = p29_app
    token, org, org_id = await _org(client, "dual-finalize")
    call = await _add_call(session, org_id)

    set_org_context(session, org_id)
    rec_id = uuid.uuid4()
    key = recordings_svc.storage_key(org_id, rec_id)
    recording = CallRecording(
        id=rec_id,
        org_id=org_id,
        call_id=call.id,
        provider_recording_id=f"rec-{rec_id}",
        storage_key=key,
        status="pending",
        channel_layout="mixed",
    )
    session.add(recording)
    await session.commit()
    await session.refresh(recording)

    agent = _mono_wav([1000] * 4000)
    customer = _mono_wav([-1000] * 2000)
    store = application.state.media_store
    recording = await recordings_svc.finalize_dual_recording(
        session, store, recording, agent_wav=agent, customer_wav=customer
    )
    await session.commit()
    await session.refresh(recording)

    assert recording.status == "stored"
    assert recording.channel_layout == "dual"
    mixed = await store.get(recordings_svc.layout_storage_key(recording, "mixed"))
    agent_bytes = await store.get(recordings_svc.layout_storage_key(recording, "agent"))
    customer_bytes = await store.get(
        recordings_svc.layout_storage_key(recording, "customer")
    )
    assert mixed is not None
    assert agent_bytes is not None
    assert customer_bytes is not None

    headers = auth_headers(token, org_id)
    r = await client.get(
        f"/api/v1/calls/{call.id}/recordings/{rec_id}", headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.content == mixed

    r = await client.get(
        f"/api/v1/calls/{call.id}/recordings/{rec_id}",
        params={"layout": "agent"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.content == agent_bytes

    r = await client.get(
        f"/api/v1/calls/{call.id}/recordings/{rec_id}",
        params={"layout": "customer"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.content == customer_bytes

    r = await client.get(f"/api/v1/calls/{call.id}", headers=headers)
    assert r.status_code == 200, r.text
    recordings = r.json()["recordings"]
    assert len(recordings) == 1
    layouts = {file["layout"] for file in recordings[0]["files"]}
    assert layouts == {"mixed", "agent", "customer"}


async def test_a_mixed_recording_does_not_offer_the_separate_files(p29_app, session):
    client, _fake, application = p29_app
    token, org, org_id = await _org(client, "mixed-only")
    call = await _add_call(session, org_id)

    set_org_context(session, org_id)
    rec_id = uuid.uuid4()
    key = recordings_svc.storage_key(org_id, rec_id)
    data = _mono_wav([1000] * 100)
    recording = CallRecording(
        id=rec_id,
        org_id=org_id,
        call_id=call.id,
        provider_recording_id=f"mixed-{rec_id}",
        storage_key=key,
        status="stored",
        channel_layout="mixed",
        content_type="audio/wav",
        size_bytes=len(data),
    )
    session.add(recording)
    await session.commit()
    await application.state.media_store.put(key, data, "audio/wav")

    headers = auth_headers(token, org_id)
    r = await client.get(
        f"/api/v1/calls/{call.id}/recordings/{rec_id}",
        params={"layout": "agent"},
        headers=headers,
    )
    assert r.status_code == 404, r.text

    r = await client.get(f"/api/v1/calls/{call.id}", headers=headers)
    assert r.status_code == 200, r.text
    files = r.json()["recordings"][0]["files"]
    assert [file["layout"] for file in files] == ["mixed"]


# ---- Consent announcement -------------------------------------------------------
async def test_consent_announcement_is_played_before_connecting_inbound(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "consent-on")
    await _set_announcement(session, org_id, True)
    flow = await _seed_ring_flow(session, org_id)
    call = await _add_call(session, org_id, status="queued")

    commands = await routing_exec_svc.start_carrier_flow(session, None, call, flow)
    assert len(commands) >= 2
    assert isinstance(commands[0], voice.Speak)
    assert commands[0].text == DEFAULT_ANNOUNCEMENT
    assert isinstance(commands[1], voice.Speak)
    assert commands[1].text == "Please hold while we try to connect you."


async def test_no_consent_announcement_when_the_toggle_is_off(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "consent-off")
    await _set_announcement(session, org_id, False)
    flow = await _seed_ring_flow(session, org_id)
    call = await _add_call(session, org_id, status="queued")

    commands = await routing_exec_svc.start_carrier_flow(session, None, call, flow)
    assert len(commands) >= 1
    assert isinstance(commands[0], voice.Speak)
    assert commands[0].text == "Please hold while we try to connect you."
    speak_texts = [cmd.text for cmd in commands if isinstance(cmd, voice.Speak)]
    assert DEFAULT_ANNOUNCEMENT not in speak_texts


async def test_consent_announcement_plays_once_per_call(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "consent-once")
    await _set_announcement(session, org_id, True)
    flow = await _seed_ring_flow(session, org_id)
    call = await _add_call(session, org_id, status="queued")

    await routing_exec_svc.start_carrier_flow(session, None, call, flow)
    assert (call.extra or {}).get("consent_announced") is True


async def test_consent_announcement_is_played_before_recording_on_an_answered_outbound_call():
    call = SimpleNamespace(extra={"record": True})
    org_on = SimpleNamespace(recording_announcement=True, recording_announcement_text=None)
    commands = _outbound_answer_commands(call, org_on, needs_pause=False)
    assert len(commands) == 2
    assert isinstance(commands[0], voice.Speak)
    assert commands[0].text == DEFAULT_ANNOUNCEMENT
    assert isinstance(commands[1], voice.StartRecording)

    off_call = SimpleNamespace(extra={"record": True})
    org_off = SimpleNamespace(
        recording_announcement=False, recording_announcement_text=None
    )
    commands = _outbound_answer_commands(off_call, org_off, needs_pause=False)
    assert len(commands) == 1
    assert isinstance(commands[0], voice.StartRecording)

    no_org_call = SimpleNamespace(extra={"record": True})
    commands = _outbound_answer_commands(no_org_call, None, needs_pause=False)
    assert len(commands) == 1
    assert isinstance(commands[0], voice.StartRecording)


# ---- Supervisor coaching (D15) --------------------------------------------------
async def test_whisper_without_a_media_server_connection_is_refused(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "coach-no-api")
    call = await _add_call(
        session,
        org_id,
        status="answered",
        extra={"via": "livekit", "room": f"call-{uuid.uuid4()}"},
    )
    settings = make_settings()
    with pytest.raises(FeatureUnavailableError):
        await supervisor_svc.whisper(
            session, settings, None, call, identity="sup", name="Sup"
        )


async def test_whisper_records_the_coaching_identity_on_the_call(p29_app, session):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "coach-identity")
    call = await _add_call(
        session,
        org_id,
        status="answered",
        extra={"via": "livekit", "room": f"call-{uuid.uuid4()}"},
    )
    fake = FakeLiveKit(participants=[])
    # A real signing key: make_settings() leaves the LiveKit secret blank, and minting a
    # token with an empty HMAC key raises rather than returning something unverifiable.
    settings = make_settings(livekit_api_key="lk-key", livekit_api_secret="lk-secret")

    token_result = await supervisor_svc.whisper(
        session, settings, fake, call, identity="coach-1", name="Coach"
    )
    assert isinstance(token_result, str)
    assert token_result
    await session.refresh(call)
    assert "coach-1" in (call.extra or {}).get("coaching_identities", [])


async def test_a_customer_leg_subscribing_to_a_coaching_track_is_removed_and_audited(
    p29_app, session
):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "coach-leak")
    call = await _add_call(
        session,
        org_id,
        status="answered",
        extra={"coaching_identities": ["coach"]},
    )
    fake = FakeLiveKit(participants=[])
    event = {
        "event": "track_subscribed",
        "room": {"name": f"call-{call.id}"},
        "participant": {"identity": "coach"},
        "subscriber": {"identity": "pstn-sub", "attributes": {"sip.callID": "sip-1"}},
        "track": {"sid": "TR-SUB"},
    }

    acted = await supervisor_svc.enforce_coaching_privacy(session, fake, event)
    assert acted is True
    assert fake.unsubscribed == [("pstn-sub", ("TR-SUB",), False)]

    set_org_context(session, org_id)
    audits = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.target_id == str(call.id))
        )
    ).scalars().all()
    assert any(row.action == "supervisor.coaching_leak" for row in audits)

    voice_rows = (
        await session.execute(
            sa.select(VoiceEvent).where(
                VoiceEvent.call_id == call.id,
                VoiceEvent.event_type == "supervisor.coaching_leak",
            )
        )
    ).scalars().all()
    assert len(voice_rows) == 1


async def test_a_track_published_by_a_coaching_supervisor_preemptively_unsubscribes_the_phone_leg(
    p29_app, session
):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "coach-published")
    call = await _add_call(
        session,
        org_id,
        status="answered",
        extra={"coaching_identities": ["coach"]},
    )
    fake = FakeLiveKit(
        participants=[
            {"identity": "pstn-leg", "attributes": {"sip.callID": "sip-1"}},
            {"identity": "web-leg", "attributes": {}},
        ]
    )
    event = {
        "event": "track_published",
        "room": {"name": f"call-{call.id}"},
        "participant": {"identity": "coach"},
        "track": {"sid": "TR-PUB"},
    }

    acted = await supervisor_svc.enforce_coaching_privacy(session, fake, event)
    assert acted is True
    assert fake.unsubscribed == [("pstn-leg", ("TR-PUB",), False)]

    set_org_context(session, org_id)
    audits = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.target_id == str(call.id))
        )
    ).scalars().all()
    assert audits == []


async def test_enforcement_ignores_a_track_from_a_non_coaching_participant(
    p29_app, session
):
    client, _fake, _app = p29_app
    token, org, org_id = await _org(client, "coach-ignore")
    call = await _add_call(
        session,
        org_id,
        status="answered",
        extra={"coaching_identities": ["coach"]},
    )
    fake = FakeLiveKit(
        participants=[
            {"identity": "pstn-leg", "attributes": {"sip.callID": "sip-1"}}
        ]
    )
    event = {
        "event": "track_published",
        "room": {"name": f"call-{call.id}"},
        "participant": {"identity": "not-coach"},
        "track": {"sid": "TR-NOPE"},
    }

    acted = await supervisor_svc.enforce_coaching_privacy(session, fake, event)
    assert acted is False
    assert fake.unsubscribed == []

    set_org_context(session, org_id)
    audits = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.target_id == str(call.id))
        )
    ).scalars().all()
    assert audits == []
    voice_rows = (
        await session.execute(
            sa.select(VoiceEvent).where(VoiceEvent.call_id == call.id)
        )
    ).scalars().all()
    assert voice_rows == []


async def test_enforcement_ignores_rooms_that_are_not_ours(session):
    fake = FakeLiveKit(participants=[])
    event = {
        "event": "track_published",
        "room": {"name": "lobby"},
        "participant": {"identity": "coach"},
        "track": {"sid": "TR-X"},
    }

    acted = await supervisor_svc.enforce_coaching_privacy(session, fake, event)
    assert acted is False
    assert fake.unsubscribed == []


# ---- Bandwidth Numbers API auth (D37 regression) --------------------------------
_BANDWIDTH_NUMBERS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<SearchResult>
  <TelephoneNumberDetailList>
    <TelephoneNumberDetail>
      <FullNumber>2145550100</FullNumber>
      <City>Dallas</City>
      <State>TX</State>
    </TelephoneNumberDetail>
  </TelephoneNumberDetailList>
</SearchResult>
"""


async def test_bandwidth_numbers_api_uses_basic_auth_even_under_oauth2():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=_BANDWIDTH_NUMBERS_XML, headers={"Content-Type": "application/xml"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthMessagingCarrier(
            account_id="acct-123",
            api_username="api_user",
            api_password="api_pass",
            application_id="app-1",
            auth_mode="oauth2",
            client=http,
        )
        results = await carrier.search_numbers(NumberSearch(area_code="214"))

    assert len(seen) == 1
    authorization = seen[0].headers.get("Authorization")
    assert authorization is not None
    assert authorization.startswith("Basic ")
    decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode()
    assert decoded == "api_user:api_pass"
    assert "Bearer" not in authorization
    assert results[0].e164 == "+12145550100"


async def test_agents_can_read_the_call_result_list(p29_app, session):
    """An agent (calls:read, no settings:read) records call results, so it must be able
    to read the list PATCH /calls/{id}/disposition validates against - without being
    handed the rest of the calling settings."""
    client, _fake, _app = p29_app
    token, _org_row, org_id = await _org(client, "dispositions-agent-read")
    agent_token = await _register_member(
        client, session, org_id, "p29-agent-list@example.com", "agent"
    )

    r = await client.get("/api/v1/calls/dispositions", headers=auth_headers(agent_token, org_id))
    assert r.status_code == 200, r.text
    assert r.json() == {"dispositions": list(calling_settings_svc.DEFAULT_DISPOSITIONS)}

    r = await client.get("/api/v1/orgs/current/calling", headers=auth_headers(agent_token, org_id))
    assert r.status_code == 403, r.text

    r = await client.patch(
        "/api/v1/orgs/current/calling",
        json={"dispositions": ["Booked", "Not now"]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/calls/dispositions", headers=auth_headers(agent_token, org_id))
    assert r.status_code == 200, r.text
    assert r.json() == {"dispositions": ["Booked", "Not now"]}
