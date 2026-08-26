"""P8 machine seams: GET /api/v1/agent/context/{call_id}, POST /api/v1/agent/transcript.

Both are worker-only (JWT signed with the LiveKit secret, no OrgContext/X-Org-Id) -
`verify_worker_token`'s whole job is to make a stolen/forged token, a wrong-secret token,
or a perfectly good USER bearer token all fail the same way (401), and to make org
resolution come ONLY from the Call row the worker names, never from anything the caller
asserts about itself.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import AgentProfile, CallTranscriptSegment
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

OUR = "+12145550100"
THEIRS = "+19725550199"

LK_KEY = "lk-test-key"
LK_SECRET = "lk-test-secret-value-padded-to-32-bytes-plus"


def make_agent_settings(**overrides):
    base = {
        "bandwidth_webhook_username": WEBHOOK_USER,
        "bandwidth_webhook_password": WEBHOOK_PASS,
        "livekit_api_key": LK_KEY,
        "livekit_api_secret": LK_SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


def worker_token(*, key=LK_KEY, secret=LK_SECRET, sub="agent-worker", exp_offset=3600, **extra):
    claims = {"iss": key, "sub": sub, "exp": int(time.time()) + exp_offset, **extra}
    return jwt.encode(claims, secret, algorithm="HS256")


def worker_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def app_with_agent(engine):
    """Bandwidth (to place a carrier-path call cheaply) + a LiveKit key/secret pair the
    worker's JWTs are signed with. No real LiveKit needed - the agent seams never call it."""
    settings = make_agent_settings()
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    import httpx

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, application


async def _place_call(client, email: str, org_name: str, e164: str = OUR) -> tuple[str, dict, str]:
    """token, org, call_id for a fresh org with one active number and one carrier call."""
    token, org, _number = await make_org_with_number(client, email, org_name, e164)
    r = await client.post(
        "/api/v1/calls", json={"to": THEIRS}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    return token, org, r.json()["id"]


# ==================================================================================
# Worker auth
# ==================================================================================
async def test_worker_valid_token_reads_context(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "w1@example.com", "Org W1")

    r = await client.get(
        f"/api/v1/agent/context/{call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contact_e164"] == THEIRS
    assert body["direction"] == "outbound"
    assert body["system_prompt"] == ""  # no profile yet -> defaults


async def test_worker_wrong_secret_401(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "w2@example.com", "Org W2")

    bad = worker_token(secret="totally-different-secret-padded-to-32-bytes")
    r = await client.get(f"/api/v1/agent/context/{call_id}", headers=worker_headers(bad))
    assert r.status_code == 401


async def test_worker_wrong_iss_401(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "w3@example.com", "Org W3")

    bad = worker_token(key="some-other-key")
    r = await client.get(f"/api/v1/agent/context/{call_id}", headers=worker_headers(bad))
    assert r.status_code == 401


async def test_worker_wrong_sub_401(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "w4@example.com", "Org W4")

    bad = worker_token(sub="someone-else")
    r = await client.get(f"/api/v1/agent/context/{call_id}", headers=worker_headers(bad))
    assert r.status_code == 401


async def test_worker_expired_token_401(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "w5@example.com", "Org W5")

    bad = worker_token(exp_offset=-10)
    r = await client.get(f"/api/v1/agent/context/{call_id}", headers=worker_headers(bad))
    assert r.status_code == 401


async def test_worker_missing_exp_401(app_with_agent):
    """`exp` is required, not merely checked-if-present - a token that omits it entirely
    (rather than one that is expired) must be rejected too."""
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "w5b@example.com", "Org W5b")

    claims = {"iss": LK_KEY, "sub": "agent-worker"}
    bad = jwt.encode(claims, LK_SECRET, algorithm="HS256")
    r = await client.get(f"/api/v1/agent/context/{call_id}", headers=worker_headers(bad))
    assert r.status_code == 401


async def test_user_bearer_token_rejected_on_machine_seam(app_with_agent):
    """A perfectly valid USER access token (signed with jwt_secret, not the LiveKit
    secret) must not work here - these seams are not user endpoints."""
    client, _app = app_with_agent
    user_token, _org, call_id = await _place_call(client, "w6@example.com", "Org W6")

    r = await client.get(
        f"/api/v1/agent/context/{call_id}", headers={"Authorization": f"Bearer {user_token}"}
    )
    assert r.status_code == 401


async def test_worker_context_unknown_call_404(app_with_agent):
    client, _app = app_with_agent
    r = await client.get(
        f"/api/v1/agent/context/{uuid.uuid4()}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 404


# ==================================================================================
# Context resolution (default profile / single profile / no profile)
# ==================================================================================
async def test_context_default_profile_wins_over_nondefault(app_with_agent, session):
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "ctx1@example.com", "Org CTX1")
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    session.add_all(
        [
            AgentProfile(
                id=uuid.uuid4(),
                org_id=org_id,
                name="Backup",
                system_prompt="backup prompt",
                is_default=False,
            ),
            AgentProfile(
                id=uuid.uuid4(),
                org_id=org_id,
                name="Main",
                system_prompt="main prompt",
                greeting="Hi there",
                voice_id="voice-1",
                llm_provider="anthropic",
                llm_model="claude-haiku",
                is_default=True,
                extra={"rules": ["be polite"]},
            ),
        ]
    )
    await session.commit()

    r = await client.get(
        f"/api/v1/agent/context/{call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["system_prompt"] == "main prompt"
    assert body["greeting"] == "Hi there"
    assert body["voice_id"] == "voice-1"
    assert body["llm_provider"] == "anthropic"
    assert body["llm_model"] == "claude-haiku"
    assert body["extra_rules"] == ["be polite"]
    assert body["org_name"] == "Org CTX1"


async def test_context_carries_voicemail_message_to_the_worker(app_with_agent):
    """The worker's voicemail drop reads context["voicemail_message"] - the two sides of
    this seam were built by different implementers, and the field going missing here
    silently disables voicemail drop for every org. Pin it end-to-end through the route."""
    client, _app = app_with_agent
    token, org, call_id = await _place_call(client, "ctxvm@example.com", "Org CTXVM")
    h = auth_headers(token, org["id"])
    created = await client.post(
        "/api/v1/agent/profiles",
        json={"name": "vm", "voicemail_message": "Sorry we missed you - call us back."},
        headers=h,
    )
    assert created.status_code == 201, created.text
    await client.post(
        f"/api/v1/agent/profiles/{created.json()['id']}/default", headers=h
    )

    r = await client.get(
        f"/api/v1/agent/context/{call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200
    assert r.json()["voicemail_message"] == "Sorry we missed you - call us back."


async def test_context_no_profiles_returns_defaults(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "ctx2@example.com", "Org CTX2")

    r = await client.get(
        f"/api/v1/agent/context/{call_id}", headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["system_prompt"] == ""
    assert body["greeting"] == ""
    assert body["extra_rules"] == []


# ==================================================================================
# Transcript ingest
# ==================================================================================
async def test_transcript_batch_stored_and_visible_in_detail(app_with_agent):
    client, _app = app_with_agent
    token, org, call_id = await _place_call(client, "t1@example.com", "Org T1")

    batch = {
        "call_id": call_id,
        "segments": [
            {"role": "agent", "text": "Hello, how can I help?", "at_ms": 500},
            {"role": "user", "text": "I have a question.", "at_ms": 1200},
        ],
    }
    r = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 2}

    detail = await client.get(f"/api/v1/calls/{call_id}", headers=auth_headers(token, org["id"]))
    assert detail.status_code == 200, detail.text
    transcript = detail.json()["transcript"]
    assert transcript == [
        {"role": "agent", "text": "Hello, how can I help?", "at_ms": 500},
        {"role": "user", "text": "I have a question.", "at_ms": 1200},
    ]


async def test_transcript_exact_redelivery_is_idempotent(app_with_agent, session):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "t2@example.com", "Org T2")

    batch = {
        "call_id": call_id,
        "segments": [
            {"role": "agent", "text": "Hi.", "at_ms": 0},
            {"role": "user", "text": "Hey.", "at_ms": 800},
        ],
    }
    first = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert first.json() == {"accepted": 2}

    # Exact redelivery of the SAME batch: nothing new is accepted, nothing duplicates.
    second = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"accepted": 0}

    from app.db.base import ALLOW_UNSCOPED_KEY

    rows = (
        await session.execute(
            sa.select(CallTranscriptSegment)
            .where(CallTranscriptSegment.call_id == uuid.UUID(call_id))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    assert len(rows) == 2


async def test_transcript_interleaved_batches_order_correctly(app_with_agent):
    client, _app = app_with_agent
    token, org, call_id = await _place_call(client, "t3@example.com", "Org T3")

    batch_a = {
        "call_id": call_id,
        "segments": [
            {"role": "agent", "text": "First.", "at_ms": 0},
            {"role": "agent", "text": "Third.", "at_ms": 2000},
        ],
    }
    batch_b = {
        "call_id": call_id,
        "segments": [{"role": "user", "text": "Second.", "at_ms": 1000}],
    }
    r1 = await client.post(
        "/api/v1/agent/transcript", json=batch_a, headers=worker_headers(worker_token())
    )
    r2 = await client.post(
        "/api/v1/agent/transcript", json=batch_b, headers=worker_headers(worker_token())
    )
    assert r1.status_code == 200 and r2.status_code == 200

    detail = await client.get(f"/api/v1/calls/{call_id}", headers=auth_headers(token, org["id"]))
    texts = [seg["text"] for seg in detail.json()["transcript"]]
    assert texts == ["First.", "Second.", "Third."]


async def test_transcript_over_200_segments_is_422(app_with_agent):
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "t4@example.com", "Org T4")

    batch = {
        "call_id": call_id,
        "segments": [
            {"role": "user", "text": f"msg {i}", "at_ms": i} for i in range(201)
        ],
    }
    r = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert r.status_code == 422


async def test_transcript_at_ms_boundary(app_with_agent):
    """F6: at_ms is capped at the Postgres INTEGER ceiling - within it is a normal
    accept, one past it is a 422 (caught by pydantic validation, never reaching the
    DB as a DataError that would escape the per-segment IntegrityError savepoint)."""
    client, _app = app_with_agent
    _token, _org, call_id = await _place_call(client, "atms1@example.com", "Org ATMS1")

    within = {
        "call_id": call_id,
        "segments": [{"role": "user", "text": "at the ceiling", "at_ms": 2_147_483_647}],
    }
    r_ok = await client.post(
        "/api/v1/agent/transcript", json=within, headers=worker_headers(worker_token())
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json() == {"accepted": 1}

    over = {
        "call_id": call_id,
        "segments": [{"role": "user", "text": "past the ceiling", "at_ms": 2_147_483_648}],
    }
    r_over = await client.post(
        "/api/v1/agent/transcript", json=over, headers=worker_headers(worker_token())
    )
    assert r_over.status_code == 422, r_over.text


async def test_transcript_invalid_segment_skipped_valid_siblings_land(app_with_agent):
    """A segment failing basic validation (bad role, empty text) is skipped silently -
    the caller only ever sees a single `accepted` count - but must NOT take its valid
    siblings in the same batch down with it."""
    client, _app = app_with_agent
    _token, org, call_id = await _place_call(client, "inv1@example.com", "Org INV1")

    batch = {
        "call_id": call_id,
        "segments": [
            {"role": "user", "text": "good one", "at_ms": 0},
            {"role": "unknown", "text": "bad role", "at_ms": 100},
            {"role": "user", "text": "   ", "at_ms": 200},
            {"role": "agent", "text": "also good", "at_ms": 300},
        ],
    }
    r = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 2}

    detail = await client.get(f"/api/v1/calls/{call_id}", headers=auth_headers(_token, org["id"]))
    texts = [seg["text"] for seg in detail.json()["transcript"]]
    assert texts == ["good one", "also good"]


async def test_transcript_unknown_call_404(app_with_agent):
    client, _app = app_with_agent
    batch = {
        "call_id": str(uuid.uuid4()),
        "segments": [{"role": "user", "text": "hi", "at_ms": 0}],
    }
    r = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert r.status_code == 404


async def test_transcript_scoped_to_the_calls_org(app_with_agent, session):
    """Segments land under the CALL's org - posting to org A's call must be invisible to
    org B, even though the same worker identity can name any call id at all."""
    client, _app = app_with_agent
    _token_a, org_a, call_a = await _place_call(client, "scopeA@example.com", "Org Scope A")
    _token_b, org_b, _call_b = await _place_call(
        client, "scopeB@example.com", "Org Scope B", "+12145550101"
    )
    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    batch = {
        "call_id": call_a,
        "segments": [{"role": "user", "text": "org A only", "at_ms": 0}],
    }
    r = await client.post(
        "/api/v1/agent/transcript", json=batch, headers=worker_headers(worker_token())
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 1}

    set_org_context(session, org_b_id)
    b_rows = (
        await session.execute(sa.select(CallTranscriptSegment))
    ).scalars().all()
    assert b_rows == []

    set_org_context(session, org_a_id)
    a_rows = (
        await session.execute(sa.select(CallTranscriptSegment))
    ).scalars().all()
    assert len(a_rows) == 1
    assert a_rows[0].org_id == org_a_id


# ==================================================================================
# P9: voicemail_message on the agent profile (spoken after the beep on outbound drops).
# ==================================================================================
async def test_profile_voicemail_message_roundtrips_through_create_and_patch(app_with_agent):
    client, _app = app_with_agent
    token, org, _call_id = await _place_call(client, "vm1@example.com", "Org VM1")
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/agent/profiles",
        json={"name": "Main", "voicemail_message": "Sorry we missed you, call back soon."},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["voicemail_message"] == "Sorry we missed you, call back soon."

    r2 = await client.patch(
        f"/api/v1/agent/profiles/{body['id']}",
        json={"voicemail_message": "Please leave a message after the tone."},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["voicemail_message"] == "Please leave a message after the tone."


async def test_profile_voicemail_message_defaults_empty(app_with_agent):
    client, _app = app_with_agent
    token, org, _call_id = await _place_call(client, "vm2@example.com", "Org VM2")
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/agent/profiles", json={"name": "NoDrop"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["voicemail_message"] == ""
