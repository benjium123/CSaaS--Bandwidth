"""MMS media: upload, signed URLs, and the re-hosting pipeline.

The load-bearing assertion here is ``test_webhook_makes_zero_http_calls``: inbound media
must be *recorded* in the webhook path and *fetched* outside it. Bandwidth only hosts media
~48h so we must re-host, but ingestion has to stay DB-only and 2xx-fast.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import MediaAsset
from app.services import media as media_svc
from app.storage.base import InMemoryObjectStore
from tests.conftest import (
    TEST_JWT_SECRET,
    auth_headers,
    fixture_bytes,
    make_org_with_number,
    webhook_auth_headers,
)

HOOK = "/api/v1/webhooks/bandwidth/messaging"
OUR = "+12145550100"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


async def _assets(session) -> list[MediaAsset]:
    return list(
        (
            await session.execute(
                sa.select(MediaAsset).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )


# ---------------------------------------------------------------------------------
# Signed URLs
# ---------------------------------------------------------------------------------
def test_signature_roundtrip_and_tamper():
    asset_id = uuid.uuid4()
    exp = int(time.time()) + 600
    sig = media_svc.sign(asset_id, exp, TEST_JWT_SECRET)

    assert media_svc.verify_signature(asset_id, exp, sig, TEST_JWT_SECRET)
    assert not media_svc.verify_signature(asset_id, exp, "deadbeef", TEST_JWT_SECRET)
    assert not media_svc.verify_signature(uuid.uuid4(), exp, sig, TEST_JWT_SECRET)
    assert not media_svc.verify_signature(asset_id, exp + 1, sig, TEST_JWT_SECRET)


def test_expired_signature_is_refused():
    asset_id = uuid.uuid4()
    past = int(time.time()) - 5
    assert not media_svc.verify_signature(
        asset_id, past, media_svc.sign(asset_id, past, TEST_JWT_SECRET), TEST_JWT_SECRET
    )


def test_signature_grants_access_to_one_asset_only():
    """A leaked link must not become org-wide access."""
    a, b = uuid.uuid4(), uuid.uuid4()
    exp = int(time.time()) + 600
    assert not media_svc.verify_signature(
        b, exp, media_svc.sign(a, exp, TEST_JWT_SECRET), TEST_JWT_SECRET
    )


# ---------------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------------
def test_upload_validation():
    from app.errors import ValidationFailedError

    media_svc.validate_upload("image/png", 1000)  # ok
    with pytest.raises(ValidationFailedError):
        media_svc.validate_upload("application/x-msdownload", 10)
    with pytest.raises(ValidationFailedError):
        media_svc.validate_upload("image/png", media_svc.MAX_MEDIA_BYTES + 1)
    with pytest.raises(ValidationFailedError):
        media_svc.validate_upload("image/png", 0)


async def test_upload_and_fetch_content(app_with_carrier, session):
    client, _, application = app_with_carrier
    token, org, _ = await make_org_with_number(client, "m1@example.com", "Org A", OUR)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/media",
        files={"file": ("pic.png", PNG, "image/png")},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "stored"
    assert body["size_bytes"] == len(PNG)

    asset_id = body["id"]
    exp = int(time.time()) + 600
    sig = media_svc.sign(uuid.UUID(asset_id), exp, TEST_JWT_SECRET)
    got = await client.get(f"/api/v1/media/{asset_id}/content?exp={exp}&sig={sig}")
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["content-type"].startswith("image/png")


async def test_content_requires_a_valid_signature(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "m2@example.com", "Org A", OUR)
    r = await client.post(
        "/api/v1/media",
        files={"file": ("pic.png", PNG, "image/png")},
        headers=auth_headers(token, org["id"]),
    )
    asset_id = r.json()["id"]

    assert (await client.get(f"/api/v1/media/{asset_id}/content")).status_code == 403
    bad = await client.get(f"/api/v1/media/{asset_id}/content?exp=9999999999&sig=nope")
    assert bad.status_code == 403


# ---------------------------------------------------------------------------------
# Inbound: recorded in the webhook, fetched outside it
# ---------------------------------------------------------------------------------
async def _inbound_with_media(client, url: str, msg_id: str = "mms-1"):
    payload = json.loads(fixture_bytes("message-received.json"))
    payload[0]["message"]["id"] = msg_id
    payload[0]["message"]["media"] = [url]
    payload[0]["message"]["channel"] = "mms"
    r = await client.post(
        HOOK, content=json.dumps(payload).encode(), headers=webhook_auth_headers()
    )
    assert r.status_code == 200, r.text


async def test_webhook_makes_zero_http_calls(app_with_carrier, session):
    """THE CONTRACT: inbound media is RECORDED in the request path, never FETCHED there."""
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m3@example.com", "Org A", OUR)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(str(request.url))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    await _inbound_with_media(client, "https://media.bandwidth.com/x.png")

    assert calls == [], "the webhook path must make no outbound HTTP calls"
    assets = await _assets(session)
    assert len(assets) == 1
    assert assets[0].status == "pending"
    assert assets[0].direction == "inbound"
    assert assets[0].storage_key is None


async def test_fetch_pending_rehosts_the_media(app_with_carrier, session):
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m4@example.com", "Org A", OUR)
    await _inbound_with_media(client, "https://media.bandwidth.com/x.png")

    store = InMemoryObjectStore()
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        count = await media_svc.fetch_pending_media(session, store, client=http)

    assert count == 1
    assert fetched == ["https://media.bandwidth.com/x.png"]
    asset = (await _assets(session))[0]
    assert asset.status == "stored"
    assert asset.size_bytes == len(PNG)
    assert await store.get(asset.storage_key) == PNG


async def test_oversized_media_is_refused_midstream(app_with_carrier, session):
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m5@example.com", "Org A", OUR)
    await _inbound_with_media(client, "https://media.bandwidth.com/big.png")

    huge = b"x" * (media_svc.MAX_MEDIA_BYTES + 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=huge, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await media_svc.fetch_pending_media(session, InMemoryObjectStore(), client=http)

    asset = (await _assets(session))[0]
    assert asset.status == "too_large"


async def test_unsupported_type_is_terminal(app_with_carrier, session):
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m6@example.com", "Org A", OUR)
    await _inbound_with_media(client, "https://media.bandwidth.com/x.exe")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"MZ", headers={"content-type": "application/x-msdownload"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await media_svc.fetch_pending_media(session, InMemoryObjectStore(), client=http)

    assert (await _assets(session))[0].status == "unsupported"


async def test_transient_failure_backs_off_then_gives_up(app_with_carrier, session):
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m7@example.com", "Org A", OUR)
    await _inbound_with_media(client, "https://media.bandwidth.com/flaky.png")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    store = InMemoryObjectStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await media_svc.fetch_pending_media(session, store, client=http)
        asset = (await _assets(session))[0]
        assert asset.fetch_attempts == 1
        assert asset.next_attempt_at is not None, "must back off, not hammer"
        assert asset.status == "pending"

        # Exhaust the attempts. The clock must advance CUMULATIVELY: each pass sets the
        # next attempt relative to the `now` we pass, so a fixed offset would never clear
        # the growing backoff.
        from datetime import timedelta

        base = media_svc._now()
        for i in range(media_svc.MAX_FETCH_ATTEMPTS):
            later = base + timedelta(days=i + 1)
            await media_svc.fetch_pending_media(session, store, client=http, now=later)

    asset = (await _assets(session))[0]
    assert asset.status == "failed", "the carrier URL dies ~48h; giving up must be explicit"


async def test_media_auth_only_goes_to_bandwidth_hosts():
    """Sending our API credentials to a foreign host because a payload said so would be a
    credential-leak primitive."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    carrier = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id="app"
    )
    assert carrier.media_auth("https://media.bandwidth.com/x.png") == ("u", "p")
    assert carrier.media_auth("https://evil.example.com/x.png") is None
    assert carrier.media_auth("not a url at all") is None


async def test_purge_expired_keeps_the_row(app_with_carrier, session):
    from datetime import timedelta

    client, _, _ = app_with_carrier
    token, org, _ = await make_org_with_number(client, "m8@example.com", "Org A", OUR)
    store = application_store = InMemoryObjectStore()

    from app.db.base import set_org_context

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    asset = await media_svc.store_upload(
        session, org_id, store, data=PNG, content_type="image/png", retention_days=1
    )
    asset.expires_at = media_svc._now() - timedelta(days=2)
    await session.commit()

    purged = await media_svc.purge_expired_media(session, application_store)
    assert purged == 1
    refreshed = (await _assets(session))[0]
    assert refreshed.status == "purged"
    assert refreshed.storage_key is None, "object gone"
    assert refreshed.id == asset.id, "row kept for audit"


# ---------------------------------------------------------------------------------
# Regression: a failed persist must never be reported as success
#
# History: inbound media rows were added to the SAME flush as their parent message.
# SQLAlchemy does not guarantee messages are INSERTed before media_assets, so with
# foreign keys enforced the child hit a violation. That IntegrityError surfaced in the
# dedupe handler -- which was written for duplicates -- so the handler rolled the
# message back and answered 200 OK. A real inbound MMS was destroyed, silently, and the
# carrier was told to stop retrying.
#
# Two properties keep it dead: the parent flushes first, and "duplicate" is CONFIRMED
# rather than assumed.
# ---------------------------------------------------------------------------------
async def test_inbound_mms_persists_message_and_its_media(app_with_carrier, session):
    """The original trigger: message and media must survive the same transaction."""
    from app.models import Message

    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m9@example.com", "Org A", OUR)
    await _inbound_with_media(client, "https://media.bandwidth.com/x.png", msg_id="mms-keep")

    messages = list(
        (
            await session.execute(
                sa.select(Message)
                .where(Message.provider_message_id == "mms-keep")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    assert len(messages) == 1, "the inbound MMS was dropped"
    assets = await _assets(session)
    assert len(assets) == 1
    assert assets[0].message_id == messages[0].id, "media must hang off the persisted message"


async def test_non_duplicate_integrity_error_asks_for_retry(app_with_carrier, session, monkeypatch):
    """THE HONESTY PROPERTY.

    A constraint violation that is *not* a duplicate must produce a retry, never a 200.
    Injected as a real foreign-key violation rather than a fake exception, because that is
    exactly the shape of the bug that got swallowed.
    """
    import uuid as _uuid

    from app.models import Message
    from app.services import messaging as messaging_svc

    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m10@example.com", "Org A", OUR)

    real_event_cls = messaging_svc.MessageEvent

    def orphaned_event(**kwargs):
        # Points at a message that does not exist -> FK violation at commit. A genuine
        # failure, and emphatically not a duplicate.
        kwargs["message_id"] = _uuid.uuid4()
        return real_event_cls(**kwargs)

    monkeypatch.setattr(messaging_svc, "MessageEvent", orphaned_event)

    payload = json.loads(fixture_bytes("message-received.json"))
    payload[0]["message"]["id"] = "mms-doomed"
    payload[0]["message"]["media"] = ["https://media.bandwidth.com/y.png"]
    payload[0]["message"]["channel"] = "mms"
    r = await client.post(
        HOOK, content=json.dumps(payload).encode(), headers=webhook_auth_headers()
    )

    assert r.status_code == 500, (
        "a persist failure must ask the carrier to retry; answering 200 destroys the message"
    )

    survivors = list(
        (
            await session.execute(
                sa.select(Message)
                .where(Message.provider_message_id == "mms-doomed")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    assert survivors == [], "the failed transaction must roll back whole, not half-persist"


async def test_a_real_duplicate_still_dedupes_to_200(app_with_carrier, session):
    """The counterpart: the retry path must not have broken idempotency."""
    from app.models import Message

    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m11@example.com", "Org A", OUR)

    await _inbound_with_media(client, "https://media.bandwidth.com/x.png", msg_id="mms-dupe")
    await _inbound_with_media(client, "https://media.bandwidth.com/x.png", msg_id="mms-dupe")

    messages = list(
        (
            await session.execute(
                sa.select(Message)
                .where(Message.provider_message_id == "mms-dupe")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    assert len(messages) == 1, "replay must not double-insert"
    assert len(await _assets(session)) == 1, "nor duplicate the media rows"


async def test_media_with_no_content_type_is_refused(app_with_carrier, session):
    """An unlabelled response must not walk around the allowlist.

    It used to: the type check was skipped when the header was blank, so an arbitrary
    payload was stored as octet-stream and became servable from our own origin.
    """
    client, _, _ = app_with_carrier
    await make_org_with_number(client, "m12@example.com", "Org A", OUR)
    await _inbound_with_media(client, "https://media.bandwidth.com/mystery")

    def handler(request: httpx.Request) -> httpx.Response:
        # No content-type header at all.
        return httpx.Response(200, content=b"<html>anything</html>")

    store = InMemoryObjectStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await media_svc.fetch_pending_media(session, store, client=http)

    asset = (await _assets(session))[0]
    assert asset.status == "unsupported"
    assert asset.storage_key is None, "nothing unverified may reach the object store"
