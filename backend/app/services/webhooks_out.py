"""Outbound webhooks: endpoint CRUD, event fan-out, signed delivery (P13 DR-4/DR-5).

Endpoints subscribe to a subset of ``PLATFORM_EVENT_TYPES``. The signing secret is shown
to the operator exactly ONCE at creation and stored Fernet-encrypted (the deliverer needs
it back to sign every request - unlike API keys, this cannot be hash-only).

Fan-out is PER ENDPOINT (Opus review B1): for each active endpoint it selects events of
its subscribed types created at or after ``max(endpoint.created_at, now - lookback)``
that don't already have a delivery row for THAT endpoint. Two failure modes a naive
"global scan for events with zero delivery rows anywhere" has, that this avoids:
  1. A brand-new endpoint would otherwise inherit the org's ENTIRE event history on its
     first fan-out pass (a backlog flood) - the ``endpoint.created_at`` floor fixes this.
  2. A deleted-then-recreated endpoint (CASCADE wipes its old delivery rows) would
     otherwise re-inherit events from before it ever existed the SECOND time around -
     same floor, applied to the NEW endpoint's own ``created_at``, fixes this too.
The UNIQUE(endpoint_id, event_id) constraint stays the crash-safety backstop: a repeated
pass over the same (endpoint, event) pair is a no-op regardless of the selection query.

Delivery signing is Svix-compatible (``X-Webhook-Id``/``X-Webhook-Timestamp``/
``X-Webhook-Signature: v1=...``) so DR-1's later swap to a hosted deliverer stays cheap.

SSRF guard caveat (Opus review item 5): the private/loopback/reserved-range check
resolves the hostname and inspects THOSE addresses at guard time - it is a TOCTOU check,
not a hard guarantee. DNS can rebind between the guard and the actual request, and httpx
re-resolves the host independently when it opens the connection; nothing here pins the
connection to the address the guard inspected. This is the same class of gap Svix/most
webhook deliverers accept in practice (full protection needs a connect-time IP pin at the
transport layer, out of scope for this lean v1) - honest about the limit, not a hole this
module pretends not to have.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets as _secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import anyio.to_thread
import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decrypt_credential, encrypt_credential
from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, ValidationFailedError
from app.models import (
    DELIVERY_BACKOFF_SECONDS,
    PLATFORM_EVENT_TYPES,
    PlatformEvent,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services import audit as audit_svc

#: Consecutive failed delivery ATTEMPTS across an endpoint's rows before it auto-disables
#: (DR-5), audit-logged.
DISABLE_STREAK = 20
DELIVERY_TIMEOUT_SECONDS = 10.0
_LOOPBACK_HOSTNAMES = ("localhost",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """SQLite round-trips a tz-aware DateTime column back as naive (a known
    SQLAlchemy+SQLite quirk) - every instant this app writes is UTC, so a bare value is
    treated as UTC rather than compared incorrectly against an aware one."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def generate_webhook_secret() -> str:
    return f"whsec_{_secrets.token_urlsafe(32)}"


def sign(secret: str, event_id: str, timestamp: str, body: bytes) -> str:
    """``v1=HMAC_SHA256(secret, "{id}.{timestamp}.{body}")`` - Svix-compatible (DR-5)."""
    payload = f"{event_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _fernet_key(settings: Settings) -> str:
    key = settings.credential_encryption_key.get_secret_value().strip()
    if not key:
        raise FeatureUnavailableError("Webhooks need CREDENTIAL_ENCRYPTION_KEY to be set")
    return key


# --------------------------------------------------------------------------------------
# SSRF guard (DR-5): https only; http allowed only for localhost outside production; in
# production the resolved target is checked against private/loopback/link-local ranges.
# --------------------------------------------------------------------------------------
def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _reject_private_target(host: str) -> None:
    # socket.getaddrinfo is a BLOCKING call (Opus review item 6) - run it off the event
    # loop so a slow/hanging DNS lookup on one request cannot stall every other request
    # this worker is serving.
    try:
        infos = await anyio.to_thread.run_sync(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise ValidationFailedError(f"Could not resolve webhook host: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValidationFailedError("Webhook URL resolves to a disallowed private address")


async def _guard_ssrf(url: str, settings: Settings) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationFailedError("Webhook URL must be http:// or https://")
    host = parsed.hostname
    if not host:
        raise ValidationFailedError("Webhook URL must include a host")
    if parsed.scheme == "http" and (settings.is_production or not _is_loopback_host(host)):
        raise ValidationFailedError(
            "http:// webhook URLs are only allowed for localhost outside production"
        )
    if settings.is_production:
        await _reject_private_target(host)


# --------------------------------------------------------------------------------------
# Endpoint CRUD
# --------------------------------------------------------------------------------------
def _validate_event_types(event_types: list[str]) -> list[str]:
    if not event_types:
        raise ValidationFailedError("At least one event type is required")
    unknown = [t for t in event_types if t not in PLATFORM_EVENT_TYPES]
    if unknown:
        raise ValidationFailedError(f"Unknown event types: {', '.join(sorted(unknown))}")
    return event_types


async def create_endpoint(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    *,
    url: str,
    event_types: list[str],
    created_by: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> tuple[WebhookEndpoint, str]:
    """Returns ``(row, secret)``. ``secret`` is shown ONCE - only its Fernet ciphertext
    is persisted (the deliverer decrypts it again on every attempt)."""
    await _guard_ssrf(url, settings)
    _validate_event_types(event_types)
    key = _fernet_key(settings)
    secret = generate_webhook_secret()
    row = WebhookEndpoint(
        id=uuid.uuid4(),
        org_id=org_id,
        url=url,
        secret_encrypted=encrypt_credential(secret, key),
        event_types=list(event_types),
        status="active",
        failure_streak=0,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    audit_svc.record(
        session,
        org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action="webhook_endpoint.created",
        target_type="webhook_endpoint",
        target_id=str(row.id),
        detail={"url": url, "event_types": list(event_types)},
    )
    await session.commit()
    return row, secret


async def list_endpoints(session: AsyncSession, org_id: uuid.UUID) -> list[WebhookEndpoint]:
    stmt = (
        sa.select(WebhookEndpoint)
        .where(WebhookEndpoint.org_id == org_id)
        .order_by(WebhookEndpoint.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_endpoint(
    session: AsyncSession,
    settings: Settings,
    endpoint: WebhookEndpoint,
    *,
    url: str | None = None,
    event_types: list[str] | None = None,
    status: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> WebhookEndpoint:
    changes: dict = {}
    if url is not None:
        await _guard_ssrf(url, settings)
        endpoint.url = url
        changes["url"] = url
    if event_types is not None:
        _validate_event_types(event_types)
        endpoint.event_types = list(event_types)
        changes["event_types"] = list(event_types)
    if status is not None:
        if status not in ("active", "disabled"):
            raise ValidationFailedError("status must be 'active' or 'disabled'")
        endpoint.status = status
        changes["status"] = status
        if status == "active":
            endpoint.failure_streak = 0
    if changes:
        audit_svc.record(
            session,
            endpoint.org_id,
            actor_user_id=actor_user_id,
            actor_api_key_id=actor_api_key_id,
            action="webhook_endpoint.updated",
            target_type="webhook_endpoint",
            target_id=str(endpoint.id),
            detail=changes,
        )
    await session.commit()
    return endpoint


async def delete_endpoint(
    session: AsyncSession,
    endpoint: WebhookEndpoint,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> None:
    audit_svc.record(
        session,
        endpoint.org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action="webhook_endpoint.deleted",
        target_type="webhook_endpoint",
        target_id=str(endpoint.id),
        detail={"url": endpoint.url},
    )
    await session.delete(endpoint)
    await session.commit()


# --------------------------------------------------------------------------------------
# Fan-out: platform_events -> webhook_deliveries
# --------------------------------------------------------------------------------------
#: How far back a fan-out pass looks for events, on top of the endpoint's own creation
#: instant - a floor so a long-running deployment's pass over an old, quiet endpoint does
#: not rescan its entire lifetime forever (see module docstring, Opus review B1).
FAN_OUT_LOOKBACK = timedelta(hours=24)


async def fan_out_pending_events(
    session: AsyncSession, *, limit: int = 500, now: datetime | None = None
) -> int:
    """PER ENDPOINT (Opus review B1 - see module docstring for why): for every active
    endpoint, select its subscribed-type events created at or after
    ``max(endpoint.created_at, now - FAN_OUT_LOOKBACK)`` that don't already have a
    delivery row for THIS endpoint, and create one. ``limit`` bounds events per endpoint
    per pass, not a global cap. Crash-safe: re-running over the same (endpoint, event)
    pair is a no-op - the UNIQUE constraint absorbs a race even if this scan somehow
    re-selected one."""
    moment = now or _now()
    lookback_floor = moment - FAN_OUT_LOOKBACK

    endpoints = (
        (
            await session.execute(
                sa.select(WebhookEndpoint)
                .where(WebhookEndpoint.status == "active")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for ep in endpoints:
        event_types = ep.event_types or []
        if not event_types:
            continue
        set_org_context(session, ep.org_id)
        floor = max(_as_utc(ep.created_at), lookback_floor)
        has_delivery_for_this_endpoint = sa.select(WebhookDelivery.id).where(
            WebhookDelivery.endpoint_id == ep.id, WebhookDelivery.event_id == PlatformEvent.id
        )
        events = (
            (
                await session.execute(
                    sa.select(PlatformEvent)
                    .where(
                        PlatformEvent.org_id == ep.org_id,
                        PlatformEvent.event_type.in_(event_types),
                        PlatformEvent.created_at >= floor,
                        ~has_delivery_for_this_endpoint.exists(),
                    )
                    .order_by(PlatformEvent.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for evt in events:
            session.add(
                WebhookDelivery(
                    id=uuid.uuid4(),
                    org_id=ep.org_id,
                    endpoint_id=ep.id,
                    event_id=evt.id,
                    event_type=evt.event_type,
                    status="pending",
                    attempts=0,
                    next_attempt_at=moment,
                )
            )
            created += 1
        await session.commit()
    return created


# --------------------------------------------------------------------------------------
# Delivery tick (DR-5)
# --------------------------------------------------------------------------------------
def _endpoint_should_disable(endpoint: WebhookEndpoint) -> bool:
    return endpoint.status == "active" and endpoint.failure_streak >= DISABLE_STREAK


async def _attempt_delivery(
    settings: Settings,
    client: httpx.AsyncClient,
    endpoint: WebhookEndpoint,
    event: PlatformEvent,
    delivery: WebhookDelivery,
    moment: datetime,
) -> str:
    """One HTTP attempt. Mutates ``delivery``/``endpoint`` in place; returns the outcome
    label (``"delivered"`` | ``"failed"`` | ``"dead"``)."""
    delivery.attempts += 1
    body = json.dumps(
        {"id": str(event.id), "type": event.event_type, "payload": event.payload},
        separators=(",", ":"),
        default=str,
    ).encode()
    timestamp = str(int(moment.timestamp()))
    status_code: int | None = None
    error: str | None = None
    ok = False
    try:
        await _guard_ssrf(endpoint.url, settings)
        key = _fernet_key(settings)
        secret = decrypt_credential(endpoint.secret_encrypted, key)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Id": str(event.id),
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": sign(secret, str(event.id), timestamp, body),
        }
        response = await client.post(
            endpoint.url,
            content=body,
            headers=headers,
            timeout=DELIVERY_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        status_code = response.status_code
        ok = 200 <= status_code < 300
        if not ok:
            error = f"HTTP {status_code}"
    except httpx.HTTPError as exc:
        error = str(exc)[:255]
    except (ValidationFailedError, FeatureUnavailableError, ValueError) as exc:
        error = str(exc)[:255]

    delivery.last_status_code = status_code
    delivery.last_error = error

    if ok:
        delivery.status = "delivered"
        delivery.next_attempt_at = None
        endpoint.failure_streak = 0
        return "delivered"

    endpoint.failure_streak += 1
    if delivery.attempts > len(DELIVERY_BACKOFF_SECONDS):
        delivery.status = "dead"
        delivery.next_attempt_at = None
        return "dead"
    backoff = DELIVERY_BACKOFF_SECONDS[delivery.attempts - 1]
    delivery.next_attempt_at = moment + timedelta(seconds=backoff)
    return "failed"


async def delivery_tick(
    session: AsyncSession,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, int]:
    """Sweeper-driven. Commits PER ROW - same discipline as
    ``routing_exec.routing_tick``/``voicemail.transcribe_pending``: one endpoint's outage
    must never roll back another endpoint's successful delivery in the same pass.

    The candidate SELECT joins ``webhook_endpoints`` and filters ``status == "active"``
    (Opus review B2): a disabled endpoint's pending rows must never be FETCHED into the
    ``LIMIT``-bounded batch at all, or enough of them can crowd every other org's due
    rows out of every tick indefinitely - a cross-tenant outage from one disabled
    endpoint. Filtering in the query (not skipping in the loop after fetching) also means
    re-activating the endpoint resumes delivery for free on the very next tick, with no
    separate "wake it back up" step."""
    moment = now or _now()
    counts = {"delivered": 0, "failed": 0, "dead": 0, "disabled": 0}

    stmt = (
        sa.select(WebhookDelivery)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(
            WebhookDelivery.status == "pending",
            WebhookEndpoint.status == "active",
            sa.or_(
                WebhookDelivery.next_attempt_at.is_(None),
                WebhookDelivery.next_attempt_at <= moment,
            ),
        )
        .order_by(WebhookDelivery.created_at.asc())
        .limit(limit)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    pending = list((await session.execute(stmt)).scalars().all())
    if not pending:
        return counts

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS)
    try:
        for delivery in pending:
            set_org_context(session, delivery.org_id)
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id)
            event = await session.get(PlatformEvent, delivery.event_id)
            if endpoint is None or event is None:
                # Only reachable via a race with a delete (both FKs CASCADE) - nothing
                # left to deliver.
                delivery.status = "dead"
                await session.commit()
                counts["dead"] += 1
                continue
            if endpoint.status != "active":
                # A race between the SELECT above and this fetch (another request
                # disabled it meanwhile) - leave it pending; the query already won't
                # re-offer it while disabled, so this never crowds out other rows.
                continue

            outcome = await _attempt_delivery(settings, client, endpoint, event, delivery, moment)
            counts[outcome] += 1
            if _endpoint_should_disable(endpoint):
                endpoint.status = "disabled"
                audit_svc.record(
                    session,
                    endpoint.org_id,
                    action="webhook_endpoint.auto_disabled",
                    target_type="webhook_endpoint",
                    target_id=str(endpoint.id),
                    detail={"failure_streak": endpoint.failure_streak},
                )
                counts["disabled"] += 1
            await session.commit()
    finally:
        if owns_client:
            await client.aclose()
    return counts


async def redeliver(
    session: AsyncSession,
    delivery: WebhookDelivery,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_id: uuid.UUID | None = None,
) -> WebhookDelivery:
    """Manual retry (DR-5). Reuses the SAME event row, so ``X-Webhook-Id`` on the next
    attempt is unchanged - consumer-side dedupe still holds. Attempts reset to give the
    endpoint a fresh full backoff cycle rather than immediately re-exhausting it."""
    delivery.status = "pending"
    delivery.attempts = 0
    delivery.next_attempt_at = _now()
    delivery.last_status_code = None
    delivery.last_error = None
    audit_svc.record(
        session,
        delivery.org_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action="webhook.redelivered",
        target_type="webhook_delivery",
        target_id=str(delivery.id),
        detail={"endpoint_id": str(delivery.endpoint_id), "event_id": str(delivery.event_id)},
    )
    await session.commit()
    return delivery
