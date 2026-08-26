"""MMS media: upload, signed URLs, and inbound re-hosting.

Two constraints shape this module.

**Bandwidth only hosts inbound media for ~48 hours**, so we must fetch and re-host it. But
**the webhook path is DB-only and 2xx-fast** (ARCHITECTURE D6) and P3 does not change that.
So ingestion creates ``pending`` asset rows inside its existing deduped transaction — zero
HTTP — and the actual download happens afterwards, driven by the sweeper. The ``pending``
rows in the database *are* the queue.

**Two consumers need bytes without a JWT**: the carrier (which fetches outbound MMS from a
URL) and the browser's ``<img src>`` (which cannot send an Authorization header). One
endpoint serves both, using HMAC-signed URLs derived from ``jwt_secret`` — no new secret,
and a signature grants access to exactly one asset.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ValidationFailedError
from app.models import MediaAsset

log = structlog.get_logger("media")

#: Bandwidth's documented MMS ceiling. Carriers silently downres above this; we refuse
#: rather than pretend.
MAX_MEDIA_BYTES = 3_750_000

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "video/mp4",
        "video/3gpp",
        "audio/mpeg",
        "text/vcard",
        "application/pdf",
    }
)

CARRIER_URL_TTL = 72 * 3600  # the carrier may retry for a long time
BROWSER_URL_TTL = 15 * 60
MAX_FETCH_ATTEMPTS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def storage_key(org_id: uuid.UUID, asset_id: uuid.UUID) -> str:
    """Tenant-prefixed by construction - also the P13 metering/retention hook."""
    return f"org/{org_id}/media/{asset_id}"


# --------------------------------------------------------------------------------------
# Signed URLs
# --------------------------------------------------------------------------------------
def sign(asset_id: uuid.UUID, expires_at: int, secret: str) -> str:
    msg = f"media:{asset_id}:{expires_at}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_signature(asset_id: uuid.UUID, expires_at: int, signature: str, secret: str) -> bool:
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(sign(asset_id, expires_at, secret), signature or "")


def signed_url(base_url: str, asset_id: uuid.UUID, secret: str, ttl: int) -> str:
    exp = int(time.time()) + ttl
    return (
        f"{base_url.rstrip('/')}/api/v1/media/{asset_id}/content"
        f"?exp={exp}&sig={sign(asset_id, exp, secret)}"
    )


# --------------------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------------------
def validate_upload(content_type: str, size: int) -> None:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationFailedError(
            f"Unsupported media type {content_type!r}. Allowed: "
            f"{', '.join(sorted(ALLOWED_CONTENT_TYPES))}"
        )
    if size > MAX_MEDIA_BYTES:
        raise ValidationFailedError(
            f"Media is {size} bytes; the carrier limit is {MAX_MEDIA_BYTES}"
        )
    if size == 0:
        raise ValidationFailedError("Media is empty")


async def store_upload(
    session: AsyncSession,
    org_id: uuid.UUID,
    store,  # noqa: ANN001 - ObjectStore protocol
    *,
    data: bytes,
    content_type: str,
    retention_days: int = 0,
) -> MediaAsset:
    validate_upload(content_type, len(data))

    asset = MediaAsset(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        status="stored",
    )
    asset.storage_key = storage_key(org_id, asset.id)
    if retention_days > 0:
        asset.expires_at = _now() + timedelta(days=retention_days)

    await store.put(asset.storage_key, data, content_type)
    session.add(asset)
    await session.flush()
    return asset


# --------------------------------------------------------------------------------------
# Inbound fetching - runs OUTSIDE the webhook request path
# --------------------------------------------------------------------------------------
async def fetch_pending_media(
    session: AsyncSession,
    store,  # noqa: ANN001
    *,
    client: httpx.AsyncClient | None = None,
    carrier=None,  # noqa: ANN001 - supplies media_auth for carrier-hosted URLs
    limit: int = 25,
    now: datetime | None = None,
) -> int:
    """Download and re-host inbound media whose carrier URL is about to expire.

    Never called from the webhook path. Failures back off exponentially; after
    MAX_FETCH_ATTEMPTS the asset is marked failed and logged at ERROR - the carrier URL
    dies around 48h, so that log line is the alarm.
    """
    moment = now or _now()
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    bind_moment = moment.replace(tzinfo=None) if is_sqlite else moment

    stmt = (
        sa.select(MediaAsset)
        .where(
            MediaAsset.status == "pending",
            sa.or_(
                MediaAsset.next_attempt_at.is_(None),
                MediaAsset.next_attempt_at <= bind_moment,
            ),
        )
        .limit(limit)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    pending = list((await session.execute(stmt)).scalars().all())
    if not pending:
        return 0

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    fetched = 0
    try:
        for asset in pending:
            set_org_context(session, asset.org_id)
            ok = await _fetch_one(session, store, client, carrier, asset, moment)
            fetched += 1 if ok else 0
        await session.commit()
    finally:
        if owns_client:
            await client.aclose()
    return fetched


async def _fetch_one(
    session: AsyncSession,
    store,  # noqa: ANN001
    client: httpx.AsyncClient,
    carrier,  # noqa: ANN001
    asset: MediaAsset,
    moment: datetime,
) -> bool:
    url = asset.source_url or ""
    # Carrier-hosted media needs the carrier's own credentials - and those must NEVER be
    # sent to a foreign host, so the adapter decides per-URL.
    auth = None
    if carrier is not None and hasattr(carrier, "media_auth"):
        auth = carrier.media_auth(url)

    def _fail(reason: str, terminal: bool = False) -> bool:
        asset.fetch_attempts += 1
        asset.last_error = reason[:255]
        if terminal or asset.fetch_attempts >= MAX_FETCH_ATTEMPTS:
            asset.status = "failed" if not terminal else asset.status
            log.error(
                "media_fetch_gave_up",
                asset_id=str(asset.id),
                attempts=asset.fetch_attempts,
                reason=reason,
            )
        else:
            asset.next_attempt_at = moment + timedelta(minutes=2**asset.fetch_attempts)
        return False

    try:
        async with client.stream("GET", url, auth=auth) as response:
            if response.status_code >= 400:
                return _fail(f"http {response.status_code}")

            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            # A MISSING content-type is refused, not waved through. Treating blank as
            # "probably fine" let an arbitrary blob be stored as octet-stream and later
            # served back from our own origin - the allowlist has to be a floor, not a
            # filter that anything unlabelled walks around.
            if content_type not in ALLOWED_CONTENT_TYPES:
                asset.status = "unsupported"
                asset.content_type = content_type or None
                return _fail(f"unsupported type {content_type or '<missing>'}", terminal=True)

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    # Abort mid-stream rather than buffering something enormous.
                    asset.status = "too_large"
                    return _fail(f"exceeds {MAX_MEDIA_BYTES} bytes", terminal=True)
                chunks.append(chunk)
            data = b"".join(chunks)
    except httpx.HTTPError as exc:
        return _fail(f"{type(exc).__name__}: {exc}")

    asset.storage_key = storage_key(asset.org_id, asset.id)
    await store.put(asset.storage_key, data, asset.content_type or "application/octet-stream")
    asset.content_type = asset.content_type or "application/octet-stream"
    asset.size_bytes = len(data)
    asset.sha256 = hashlib.sha256(data).hexdigest()
    asset.status = "stored"
    asset.last_error = None
    asset.next_attempt_at = None
    return True


async def purge_expired_media(
    session: AsyncSession, store, *, now: datetime | None = None  # noqa: ANN001
) -> int:
    """Delete stored objects past their retention date. The ROW is kept - audit survives."""
    moment = now or _now()
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    bind_moment = moment.replace(tzinfo=None) if is_sqlite else moment

    stmt = (
        sa.select(MediaAsset)
        .where(
            MediaAsset.status == "stored",
            MediaAsset.expires_at.is_not(None),
            MediaAsset.expires_at <= bind_moment,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    expired = list((await session.execute(stmt)).scalars().all())
    for asset in expired:
        set_org_context(session, asset.org_id)
        if asset.storage_key:
            await store.delete(asset.storage_key)
        asset.status = "purged"
        asset.storage_key = None
    if expired:
        await session.commit()
    return len(expired)
