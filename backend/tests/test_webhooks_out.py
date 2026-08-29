"""P13 DR-4/DR-5: webhook endpoint CRUD, fan-out, signed delivery, backoff/dead,
auto-disable, manual redeliver, and the SSRF guard."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.config import Settings
from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import DELIVERY_BACKOFF_SECONDS, AuditLogEntry, PlatformEvent, WebhookDelivery
from app.services import outbox, webhooks_out
from tests.conftest import auth_headers, create_org, make_settings, register_and_login

FERNET_KEY = Fernet.generate_key().decode()
#: A fixed instant works here (unlike a real-time-relative one) because every producer of
#: a time-dependent column in this file is handed this SAME `now=` explicitly -
#: fan_out_pending_events stamps next_attempt_at from it, never from the real wall clock.
FROZEN = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings():
    return make_settings(credential_encryption_key=FERNET_KEY)


async def _org(client) -> tuple[str, dict]:
    token = await register_and_login(client, "wh1@example.com")
    org = await create_org(client, token, "Org WH")
    return token, org


def _mock_client(responder) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responder(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured


async def _make_endpoint(client, h, event_types: list[str]) -> dict:
    r = await client.post(
        "/api/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "event_types": event_types},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed_event(session, org_id: uuid.UUID, event_type: str, payload: dict) -> PlatformEvent:
    set_org_context(session, org_id)
    row = outbox.record_platform_event(session, org_id, event_type, payload)
    await session.commit()
    return row


async def _delivery_for(session, event_id: uuid.UUID) -> WebhookDelivery:
    stmt = sa.select(WebhookDelivery).where(WebhookDelivery.event_id == event_id)
    return (await session.execute(stmt)).scalar_one()


async def test_fan_out_creates_one_delivery_per_subscribed_active_endpoint(client, session):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    ep = await _make_endpoint(client, h, ["message.received"])

    await _seed_event(session, org_id, "message.received", {"message_id": "m1"})

    created = await webhooks_out.fan_out_pending_events(session, now=FROZEN)
    assert created == 1

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(ep["id"]))
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"

    # Re-running fan-out over the SAME event is a no-op - the UNIQUE constraint dedupes,
    # and the "no delivery row yet" scan skips an event that already has one.
    again = await webhooks_out.fan_out_pending_events(session, now=FROZEN)
    assert again == 0
    rows_again = (
        await session.execute(
            sa.select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(ep["id"]))
        )
    ).scalars().all()
    assert len(rows_again) == 1


async def test_fan_out_skips_a_non_subscribed_event_type(client, session):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    await _make_endpoint(client, h, ["message.received"])

    await _seed_event(session, org_id, "call.completed", {"call_id": "c1"})
    created = await webhooks_out.fan_out_pending_events(session, now=FROZEN)
    assert created == 0


async def test_disabled_endpoint_gets_no_new_deliveries(client, session):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    ep = await _make_endpoint(client, h, ["message.received"])
    patched = await client.patch(
        f"/api/v1/webhook-endpoints/{ep['id']}", json={"status": "disabled"}, headers=h
    )
    assert patched.status_code == 200, patched.text

    await _seed_event(session, org_id, "message.received", {"message_id": "m1"})
    created = await webhooks_out.fan_out_pending_events(session, now=FROZEN)
    assert created == 0


async def test_fan_out_excludes_events_older_than_the_endpoint(client, session):
    """Opus review B1 regression: a brand-new endpoint must NOT inherit the org's entire
    event history on its first fan-out pass - only events at/after its own created_at."""
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    set_org_context(session, org_id)
    old_event = PlatformEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        event_type="message.received",
        payload={"n": "old"},
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    session.add(old_event)
    await session.commit()

    await _make_endpoint(client, h, ["message.received"])
    created = await webhooks_out.fan_out_pending_events(session)
    assert created == 0

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(WebhookDelivery).where(WebhookDelivery.event_id == old_event.id)
        )
    ).scalars().all()
    assert rows == []


async def test_fan_out_recreated_endpoint_does_not_reinherit_old_events(client, session):
    """Opus review B1 regression, the "recreate" case: deleting an endpoint CASCADEs its
    delivery rows, so a naive "events with zero delivery rows anywhere" scan would
    re-offer an old event to a brand-new endpoint recreated at the same URL. The new
    endpoint's own created_at floor must exclude events that predate IT."""
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    ep1 = await _make_endpoint(client, h, ["message.received"])
    old_event = await _seed_event(session, org_id, "message.received", {"n": "old"})
    fanned = await webhooks_out.fan_out_pending_events(session)
    assert fanned == 1
    set_org_context(session, org_id)
    ep1_delivery = await _delivery_for(session, old_event.id)
    assert ep1_delivery.endpoint_id == uuid.UUID(ep1["id"])

    deleted = await client.delete(f"/api/v1/webhook-endpoints/{ep1['id']}", headers=h)
    assert deleted.status_code == 204, deleted.text  # CASCADEs ep1's delivery row away

    ep2 = await _make_endpoint(client, h, ["message.received"])  # recreated: new id/created_at
    created = await webhooks_out.fan_out_pending_events(session)
    assert created == 0  # old_event predates ep2 - must NOT be re-inherited

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(ep2["id"]))
        )
    ).scalars().all()
    assert rows == []


async def test_delivery_tick_signs_correctly_and_marks_delivered_on_2xx(
    client, session, settings
):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    ep = await _make_endpoint(client, h, ["message.received"])
    secret = ep["secret"]

    event = await _seed_event(session, org_id, "message.received", {"message_id": "m1"})
    await webhooks_out.fan_out_pending_events(session, now=FROZEN)

    mock_client, captured = _mock_client(lambda _req: httpx.Response(200, json={"ok": True}))
    async with mock_client:
        counts = await webhooks_out.delivery_tick(
            session, settings, client=mock_client, now=FROZEN
        )
    assert counts == {"delivered": 1, "failed": 0, "dead": 0, "disabled": 0}
    assert len(captured) == 1

    req = captured[0]
    assert req.headers["X-Webhook-Id"] == str(event.id)
    timestamp = req.headers["X-Webhook-Timestamp"]
    expected_sig = webhooks_out.sign(secret, str(event.id), timestamp, req.content)
    assert req.headers["X-Webhook-Signature"] == expected_sig

    set_org_context(session, org_id)
    delivery = await _delivery_for(session, event.id)
    assert delivery.status == "delivered"
    assert delivery.last_status_code == 200
    assert delivery.next_attempt_at is None


async def test_delivery_backoff_schedule_then_dead(client, session, settings):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    await _make_endpoint(client, h, ["message.received"])
    event = await _seed_event(session, org_id, "message.received", {"message_id": "m1"})
    await webhooks_out.fan_out_pending_events(session, now=FROZEN)

    set_org_context(session, org_id)
    delivery = await _delivery_for(session, event.id)

    failing_client, _captured = _mock_client(lambda _req: httpx.Response(500, text="boom"))
    now = FROZEN
    async with failing_client:
        for expected_attempt, backoff in enumerate(DELIVERY_BACKOFF_SECONDS, start=1):
            counts = await webhooks_out.delivery_tick(
                session, settings, client=failing_client, now=now
            )
            assert counts["failed"] == 1, f"attempt {expected_attempt}"
            assert delivery.attempts == expected_attempt
            assert delivery.status == "pending"
            assert delivery.last_status_code == 500
            assert delivery.next_attempt_at == now + timedelta(seconds=backoff)
            now = delivery.next_attempt_at + timedelta(seconds=1)

        # One more failure past the last backoff exhausts the schedule -> dead.
        counts = await webhooks_out.delivery_tick(session, settings, client=failing_client, now=now)
        assert counts == {"delivered": 0, "failed": 0, "dead": 1, "disabled": 0}
        assert delivery.status == "dead"
        assert delivery.attempts == len(DELIVERY_BACKOFF_SECONDS) + 1


async def test_endpoint_auto_disables_after_20_consecutive_failures_and_audits(
    client, session, settings
):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    ep = await _make_endpoint(client, h, ["message.received"])

    # Push the endpoint right to the edge of the streak without 20 real ticks.
    set_org_context(session, org_id)
    from app.models import WebhookEndpoint

    endpoint_row = await session.get(WebhookEndpoint, uuid.UUID(ep["id"]))
    endpoint_row.failure_streak = webhooks_out.DISABLE_STREAK - 1
    await session.commit()

    event = await _seed_event(session, org_id, "message.received", {"message_id": "m1"})
    await webhooks_out.fan_out_pending_events(session, now=FROZEN)

    failing_client, _captured = _mock_client(lambda _req: httpx.Response(500, text="boom"))
    async with failing_client:
        counts = await webhooks_out.delivery_tick(
            session, settings, client=failing_client, now=FROZEN
        )
    assert counts["disabled"] == 1

    set_org_context(session, org_id)
    await session.refresh(endpoint_row)
    assert endpoint_row.status == "disabled"
    assert endpoint_row.failure_streak == webhooks_out.DISABLE_STREAK

    audit_row = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "webhook_endpoint.auto_disabled")
        )
    ).scalar_one()
    assert audit_row.target_id == str(endpoint_row.id)

    # A disabled endpoint's rows are excluded at the QUERY level (Opus review B2) - future
    # ticks never even fetch them, so they stay "pending" without being retried.
    delivery = await _delivery_for(session, event.id)
    assert delivery.status == "pending"


async def test_disabled_endpoint_never_starves_other_endpoints_pending_queue(
    client, session, settings
):
    """Opus review B2 regression: before the query-level status filter, a disabled
    endpoint's older pending rows would still be FETCHED into the LIMIT-bounded batch and
    then skipped one at a time in the loop - crowding out every other (healthy, different
    tenant) endpoint's due rows for as long as the disabled endpoint had rows to spare."""
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])

    stalled_ep = await _make_endpoint(client, h, ["message.received"])
    for i in range(5):
        await _seed_event(session, org_id, "message.received", {"n": i})
    await webhooks_out.fan_out_pending_events(session, now=FROZEN)
    disabled = await client.patch(
        f"/api/v1/webhook-endpoints/{stalled_ep['id']}", json={"status": "disabled"}, headers=h
    )
    assert disabled.status_code == 200, disabled.text

    healthy_ep = await _make_endpoint(client, h, ["message.received"])
    healthy_event = await _seed_event(session, org_id, "message.received", {"n": "healthy"})
    await webhooks_out.fan_out_pending_events(session, now=FROZEN + timedelta(seconds=1))

    # limit=3 is smaller than the 5 older (stalled) rows - under the pre-fix query, the
    # oldest 3 rows fetched would ALL belong to the disabled endpoint, and the healthy
    # endpoint's newer row would never be reached in this tick at all.
    success_client, captured = _mock_client(lambda _req: httpx.Response(200))
    async with success_client:
        counts = await webhooks_out.delivery_tick(
            session, settings, client=success_client, limit=3, now=FROZEN + timedelta(seconds=2)
        )
    assert counts == {"delivered": 1, "failed": 0, "dead": 0, "disabled": 0}
    assert len(captured) == 1
    assert captured[0].headers["X-Webhook-Id"] == str(healthy_event.id)

    set_org_context(session, org_id)
    healthy_ep_id = uuid.UUID(healthy_ep["id"])
    healthy_delivery = (
        await session.execute(
            sa.select(WebhookDelivery).where(WebhookDelivery.endpoint_id == healthy_ep_id)
        )
    ).scalar_one()
    assert healthy_delivery.status == "delivered"

    stalled_ep_id = uuid.UUID(stalled_ep["id"])
    stalled_rows = (
        await session.execute(
            sa.select(WebhookDelivery).where(WebhookDelivery.endpoint_id == stalled_ep_id)
        )
    ).scalars().all()
    assert len(stalled_rows) == 5
    assert all(row.status == "pending" for row in stalled_rows)  # untouched, not starving anyone


async def test_manual_redeliver_reuses_the_same_event_id(client, session, settings):
    token, org = await _org(client)
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org["id"])
    ep = await _make_endpoint(client, h, ["message.received"])
    secret = ep["secret"]

    event = await _seed_event(session, org_id, "message.received", {"message_id": "m1"})
    await webhooks_out.fan_out_pending_events(session, now=FROZEN)

    failing_client, _captured = _mock_client(lambda _req: httpx.Response(500))
    async with failing_client:
        await webhooks_out.delivery_tick(session, settings, client=failing_client, now=FROZEN)

    set_org_context(session, org_id)
    delivery = await _delivery_for(session, event.id)
    assert delivery.status == "pending"  # first backoff, not dead yet
    assert delivery.attempts == 1

    r = await client.post(f"/api/v1/webhook-deliveries/{delivery.id}/redeliver", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["attempts"] == 0
    assert r.json()["status"] == "pending"

    # redeliver() stamps next_attempt_at from the REAL wall clock (it runs behind a real
    # HTTP route, not a `now=` seam) - so the tick that picks it back up must use the
    # real clock too, not the fixed FROZEN instant used above.
    success_client, captured = _mock_client(lambda _req: httpx.Response(200))
    async with success_client:
        await webhooks_out.delivery_tick(session, settings, client=success_client)
    assert len(captured) == 1
    req = captured[0]
    assert req.headers["X-Webhook-Id"] == str(event.id)
    timestamp = req.headers["X-Webhook-Timestamp"]
    expected_sig = webhooks_out.sign(secret, str(event.id), timestamp, req.content)
    assert req.headers["X-Webhook-Signature"] == expected_sig


# --------------------------------------------------------------------------------------
# SSRF guard (DR-5). `_guard_ssrf` is async (Opus review item 6 - the private-target
# resolution runs off the event loop via anyio.to_thread), so every case here awaits it.
# --------------------------------------------------------------------------------------
async def test_https_url_is_always_allowed():
    await webhooks_out._guard_ssrf("https://example.com/hook", make_settings())


async def test_http_to_localhost_allowed_outside_production():
    await webhooks_out._guard_ssrf(
        "http://localhost:8080/hook", make_settings(app_env="development")
    )


async def test_http_to_non_localhost_is_rejected():
    with pytest.raises(ValidationFailedError):
        await webhooks_out._guard_ssrf(
            "http://example.com/hook", make_settings(app_env="development")
        )


async def test_http_to_localhost_is_rejected_in_production():
    prod_settings = Settings(
        app_env="production",
        jwt_secret="x" * 32,
        session_secret="x" * 32,
        credential_encryption_key=FERNET_KEY,
        public_base_url="https://app.csaas-prod.io",
        database_url="postgresql+asyncpg://csaas_real:pw@db/csaas",
        bandwidth_enabled=False,
    )
    with pytest.raises(ValidationFailedError):
        await webhooks_out._guard_ssrf("http://localhost/hook", prod_settings)


async def test_private_ip_target_rejected_in_production(monkeypatch):
    prod_settings = Settings(
        app_env="production",
        jwt_secret="x" * 32,
        session_secret="x" * 32,
        credential_encryption_key=FERNET_KEY,
        public_base_url="https://app.csaas-prod.io",
        database_url="postgresql+asyncpg://csaas_real:pw@db/csaas",
        bandwidth_enabled=False,
    )

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(webhooks_out.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValidationFailedError):
        await webhooks_out._guard_ssrf("https://internal.example.com/hook", prod_settings)


async def test_public_ip_target_allowed_in_production(monkeypatch):
    prod_settings = Settings(
        app_env="production",
        jwt_secret="x" * 32,
        session_secret="x" * 32,
        credential_encryption_key=FERNET_KEY,
        public_base_url="https://app.csaas-prod.io",
        database_url="postgresql+asyncpg://csaas_real:pw@db/csaas",
        bandwidth_enabled=False,
    )

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(webhooks_out.socket, "getaddrinfo", fake_getaddrinfo)
    await webhooks_out._guard_ssrf("https://public.example.com/hook", prod_settings)
