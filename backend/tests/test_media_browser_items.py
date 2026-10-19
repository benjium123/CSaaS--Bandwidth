"""Inbox previews: each message's attachments become ``{url, content_type, size_bytes}``
with a signed link to OUR stored copy, one query per page, scoped to the workspace."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models import MediaAsset, Message, MessageThread
from app.models.org import Org
from app.services import media as media_svc
from tests.conftest import TEST_JWT_SECRET

BASE = "https://ringlite.test"
TELNYX_URL = "https://tlnx-mms-media.s3.amazonaws.com/mms/abc/photo.jpeg"


async def _org(session) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Media Co", slug=f"media-{org_id.hex[:12]}"))
    await session.commit()
    return org_id


async def _add(session, org_id, *rows) -> None:
    """Write tenant rows under that workspace's context, as the app does."""
    session.info["org_id"] = org_id
    try:
        session.add_all(rows)
        await session.commit()
    finally:
        session.info.pop("org_id", None)


async def _message(session, org_id, media) -> Message:
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164="+14695550100", contact_e164="+14695550199"
    )
    await _add(session, org_id, thread)
    msg = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
        status="received", from_e164="+14695550199", to_e164="+14695550100", media=media,
    )
    await _add(session, org_id, msg)
    return msg


def _asset(org_id, **kw) -> MediaAsset:
    defaults = {
        "id": uuid.uuid4(), "org_id": org_id, "status": "stored",
        "content_type": "image/png", "size_bytes": 1234, "storage_key": "k",
    }
    defaults.update(kw)
    return MediaAsset(**defaults)


async def _items(session, org_id, messages):
    session.info["org_id"] = org_id
    try:
        return await media_svc.browser_items(
            session, messages, base_url=BASE, secret=TEST_JWT_SECRET
        )
    finally:
        session.info.pop("org_id", None)


async def test_inbound_stored_copy_gets_signed_link_and_real_type(session):
    org_id = await _org(session)
    msg = await _message(session, org_id, [TELNYX_URL])
    # Legacy row stored before the type fix: octet-stream, type recovered from the name.
    asset = _asset(
        org_id, message_id=msg.id, direction="inbound", source_url=TELNYX_URL,
        content_type="application/octet-stream",
    )
    await _add(session, org_id, asset)

    out = await _items(session, org_id, [msg])
    [item] = out[msg.id]
    assert item["url"].startswith(f"{BASE}/api/v1/media/{asset.id}/content?exp=")
    assert item["content_type"] == "image/jpeg"
    assert item["size_bytes"] == 1234
    exp = int(item["url"].split("exp=")[1].split("&")[0])
    assert exp % 3600 == 0, "expiry is rounded so the link is stable within the hour"
    sig = item["url"].split("sig=")[1]
    assert media_svc.verify_signature(asset.id, exp, sig, TEST_JWT_SECRET)


async def test_outbound_own_link_is_resigned(session):
    org_id = await _org(session)
    asset = _asset(org_id, content_type="application/pdf")
    await _add(session, org_id, asset)
    old = media_svc.signed_url(BASE, asset.id, TEST_JWT_SECRET, 60)
    msg = SimpleNamespace(id=uuid.uuid4(), media=[old])

    [item] = (await _items(session, org_id, [msg]))[msg.id]
    assert f"/api/v1/media/{asset.id}/content" in item["url"]
    assert item["url"] != old
    assert item["content_type"] == "application/pdf"


async def test_unknown_link_falls_back_with_guessed_type(session):
    org_id = await _org(session)
    msg = SimpleNamespace(id=uuid.uuid4(), media=["https://cdn.example.com/clip.mp4"])
    [item] = (await _items(session, org_id, [msg]))[msg.id]
    assert item == {
        "url": "https://cdn.example.com/clip.mp4", "content_type": "video/mp4",
        "size_bytes": None,
    }


async def test_other_workspace_asset_is_not_resolved(session):
    owner = await _org(session)
    stranger = await _org(session)
    asset = _asset(owner)
    await _add(session, owner, asset)
    foreign = f"{BASE}/api/v1/media/{asset.id}/content?exp=1&sig=x"
    msg = SimpleNamespace(id=uuid.uuid4(), media=[foreign])

    [item] = (await _items(session, stranger, [msg]))[msg.id]
    assert item["url"] == foreign, "no fresh signature for another workspace's file"


async def test_messages_without_media_are_skipped(session):
    org_id = await _org(session)
    msg = SimpleNamespace(id=uuid.uuid4(), media=[])
    assert await _items(session, org_id, [msg]) == {}
