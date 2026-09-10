"""P28 — messaging completeness: MMS, send-later, tracked links, plain failure reasons.

The one test in here worth reading twice is
``test_send_later_sweeper_run_twice_does_not_double_send``: the send-later sweeper claims
each row with a committed conditional UPDATE *before* it calls the carrier, and a
batch-at-the-end commit (the P26/P27 StaticPool hazard) would silently resend everything
on the next pass. Running the sweeper twice is the cheapest possible proof it does not.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from random import Random

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import MediaAsset, Message
from app.models.links import LinkClick, ShortLink
from app.providers.domain import CarrierError, SendResult
from app.services import links as links_svc
from app.services import messaging as messaging_svc
from app.services import messaging_errors
from tests.conftest import (
    FakeCarrier,
    _install,
    auth_headers,
    make_org_with_number,
    make_settings,
)

pytestmark = pytest.mark.asyncio

WEBHOOK_USER = "bw-hook-user"
WEBHOOK_PASS = "bw-hook-pass"
PUBLIC_API = "https://api.test.example"
PUBLIC_WEB = "https://app.test.example"


@pytest.fixture
async def p28_app(engine):
    """Like ``app_with_carrier``, but with the two public URLs actually configured.

    Tracked links and the MMS fallback both build absolute URLs; with the defaults
    (``public_base_url=""``) they would produce relative strings that the safety check
    correctly refuses, and every link assertion here would be testing the wrong thing.
    """
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
        public_base_url=PUBLIC_API,
        public_web_url=PUBLIC_WEB,
    )
    application = create_app(settings)
    fake = FakeCarrier()
    _install(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


def _rejection(code: str, detail: str = "rejected") -> SendResult:
    return SendResult(
        "rejected",
        None,
        CarrierError(
            category="invalid_request", carrier_code=code, retryable=False, detail=detail
        ),
    )


async def _org_with_number(client, slug: str, e164: str):
    token, org, number = await make_org_with_number(
        client, f"p28-{slug}@example.com", f"Org {slug}", e164
    )
    return token, org, uuid.UUID(org["id"]), number


async def _stored_asset(session, org_id: uuid.UUID, *, size_bytes: int) -> uuid.UUID:
    set_org_context(session, org_id)
    asset = MediaAsset(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        content_type="image/jpeg",
        size_bytes=size_bytes,
        storage_key=f"org/{org_id}/media/{uuid.uuid4()}",
        status="stored",
    )
    session.add(asset)
    await session.commit()
    return asset.id


async def _message(session, org_id: uuid.UUID, message_id) -> Message | None:
    set_org_context(session, org_id)
    return await session.get(Message, uuid.UUID(str(message_id)))


# ==================================================================================
# 1. MMS
# ==================================================================================
async def test_mms_size_guard_422_plain(p28_app, session):
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "mms-big", "+12145550701")
    asset_id = await _stored_asset(session, org_id, size_bytes=9_000_000)

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558801", "body": "look", "media_ids": [str(asset_id)]},
        headers=auth_headers(token, org_id),
    )

    assert r.status_code == 422, r.text
    text = r.text.lower()
    assert "attachments up to" in text, "the refusal must state the limit, not just refuse"
    assert " mb" in text, "the limit must be expressed in MB, not raw bytes"
    for jargon in ("bandwidth", "telnyx", "carrier", "e.164", "mms provider"):
        assert jargon not in text, f"customer copy must never say {jargon!r}"


async def test_mms_under_the_limit_is_accepted(p28_app, session):
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "mms-ok", "+12145550702")
    asset_id = await _stored_asset(session, org_id, size_bytes=100_000)

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558802", "body": "look", "media_ids": [str(asset_id)]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    assert len(fake.sent) == 1
    assert fake.sent[0].media, "an accepted MMS must actually carry its attachment"


async def test_mms_reject_falls_back_to_sms_with_link_and_reason(p28_app, session):
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "mms-fb", "+12145550703")
    asset_id = await _stored_asset(session, org_id, size_bytes=100_000)
    fake.scripted.append(_rejection("4740", "mms not supported"))

    r = await client.post(
        "/api/v1/messages",
        json={
            "to": "+19725558803",
            "body": "here is the photo",
            "media_ids": [str(asset_id)],
            "track_links": True,
        },
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text

    assert len(fake.sent) == 2, "an attachment-specific rejection must fall back once"
    fallback = fake.sent[-1]
    assert not fallback.media, "the fallback is a TEXT - it must not carry the attachment"
    assert "/l/" in fallback.text, "the fallback must carry a tracked link to the picture"

    row = await _message(session, org_id, r.json()["id"])
    assert row.failure_reason_public == messaging_errors.MMS_FALLBACK_REASON
    assert row.status == "accepted", "the fallback text itself was accepted"


async def test_a_plain_rejection_does_not_fall_back(p28_app, session):
    """The fallback is for ATTACHMENT problems only. Resending a message the network
    refused on its merits is exactly what gets a number blocked."""
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "mms-nofb", "+12145550704")
    asset_id = await _stored_asset(session, org_id, size_bytes=100_000)
    fake.scripted.append(_rejection("4750", "looks like spam"))

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558804", "body": "hi", "media_ids": [str(asset_id)]},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    assert len(fake.sent) == 1, "a spam rejection must never be retried as a plain text"
    row = await _message(session, org_id, r.json()["id"])
    assert row.status == "rejected"
    assert row.failure_reason_public == messaging_errors.public_reason(error_code="4750")


# ==================================================================================
# 2. Send later
# ==================================================================================
async def _schedule(client, token, org_id, to: str, when: datetime, **extra):
    payload = {"to": to, "body": "later", "scheduled_for": when.isoformat()}
    payload.update(extra)
    return await client.post(
        "/api/v1/messages", json=payload, headers=auth_headers(token, org_id)
    )


async def test_send_later_stored_and_released_by_sweeper_with_quiet_hours(p28_app, session):
    client, fake, app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "later", "+12145550705")
    when = datetime.now(timezone.utc) + timedelta(hours=1)

    r = await _schedule(client, token, org_id, "+19725558805", when)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "scheduled"
    assert body["scheduled_for"] is not None
    assert fake.sent == [], "a scheduled message must not go out at scheduling time"

    released = await messaging_svc.release_scheduled_messages(
        session, fake, now=when + timedelta(minutes=1), registry=app.state.carriers
    )
    assert released == 1
    assert len(fake.sent) == 1, "the sweeper releases it exactly once"

    row = await _message(session, org_id, body["id"])
    assert row.status == "accepted"
    assert row.scheduled_for is None, "a released message is no longer scheduled"


async def test_send_later_not_yet_due_is_left_alone(p28_app, session):
    client, fake, app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "notdue", "+12145550706")
    when = datetime.now(timezone.utc) + timedelta(hours=6)
    r = await _schedule(client, token, org_id, "+19725558806", when)
    assert r.status_code == 201, r.text

    released = await messaging_svc.release_scheduled_messages(
        session, fake, now=when - timedelta(hours=1), registry=app.state.carriers
    )
    assert released == 0
    assert fake.sent == []


async def test_send_later_sweeper_run_twice_does_not_double_send(p28_app, session):
    client, fake, app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "twice", "+12145550707")
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await _schedule(client, token, org_id, "+19725558807", when)
    assert r.status_code == 201, r.text

    due = when + timedelta(minutes=1)
    first = await messaging_svc.release_scheduled_messages(
        session, fake, now=due, registry=app.state.carriers
    )
    second = await messaging_svc.release_scheduled_messages(
        session, fake, now=due, registry=app.state.carriers
    )

    assert first == 1
    assert second == 0, "the claim is committed per row; a second pass must find nothing"
    assert len(fake.sent) == 1, "a scheduled message must never be sent twice"


async def test_send_later_cancel(p28_app, session):
    client, fake, app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "cancel", "+12145550708")
    when = datetime.now(timezone.utc) + timedelta(hours=1)

    r = await _schedule(client, token, org_id, "+19725558808", when)
    message_id = r.json()["id"]
    d = await client.delete(
        f"/api/v1/messages/{message_id}/schedule", headers=auth_headers(token, org_id)
    )
    assert d.status_code == 204, d.text
    g = await client.get(
        f"/api/v1/messages/{message_id}", headers=auth_headers(token, org_id)
    )
    assert g.status_code == 404, "a cancelled send-later message is gone, not hidden"
    assert fake.sent == []

    # ...and one that has already gone out cannot be cancelled after the fact.
    r2 = await _schedule(client, token, org_id, "+19725558809", when)
    await messaging_svc.release_scheduled_messages(
        session, fake, now=when + timedelta(minutes=1), registry=app.state.carriers
    )
    d2 = await client.delete(
        f"/api/v1/messages/{r2.json()['id']}/schedule",
        headers=auth_headers(token, org_id),
    )
    assert d2.status_code == 409, "cancelling a sent message must refuse, not silently pass"


async def test_send_later_refuses_a_moment_in_the_past(p28_app):
    client, _fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "past", "+12145550710")
    r = await _schedule(
        client,
        token,
        org_id,
        "+19725558810",
        datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert r.status_code == 422, r.text
    assert "future" in r.text.lower()


async def test_scheduled_messages_are_invisible_to_the_other_sweepers(p28_app, session):
    """A scheduled row must not be claimable by the quiet-hours release or by crash
    recovery - both of those own status='queued', and a scheduled row is not queued."""
    client, fake, app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "invisible", "+12145550711")
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await _schedule(client, token, org_id, "+19725558811", when)
    assert r.status_code == 201, r.text

    later = when + timedelta(days=1)
    await messaging_svc.release_held_messages(
        session, fake, now=later, registry=app.state.carriers
    )
    await messaging_svc.recover_stale_queued(
        session, now=later, registry=app.state.carriers
    )
    assert fake.sent == [], "neither sweeper may touch a scheduled message"
    row = await _message(session, org_id, r.json()["id"])
    assert row.status == messaging_svc.SCHEDULED_STATUS


async def test_scheduled_filter_lists_only_scheduled_messages(p28_app, session):
    client, _fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "filter", "+12145550712")
    await client.post(
        "/api/v1/messages",
        json={"to": "+19725558812", "body": "now"},
        headers=auth_headers(token, org_id),
    )
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await _schedule(client, token, org_id, "+19725558813", when)

    listed = await client.get(
        "/api/v1/messages?status=scheduled", headers=auth_headers(token, org_id)
    )
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()]
    assert ids == [r.json()["id"]], "the scheduled view shows only what has not gone out"


# ==================================================================================
# 3. Tracked links
# ==================================================================================
async def test_link_replacement_and_redirect_records_click(p28_app, session):
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "links", "+12145550713")
    target = "https://example.com/offer?id=7"

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558814", "body": f"Check {target} today", "track_links": True},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    assert "example.com" not in fake.sent[0].text, "the raw URL must not go out untracked"
    assert f"{PUBLIC_WEB}/l/" in fake.sent[0].text
    assert fake.sent[0].text.endswith(" today"), "only the URL is replaced, not the prose"

    set_org_context(session, org_id)
    link = (await session.execute(sa.select(ShortLink))).scalar_one()
    assert link.target_url == target
    assert link.message_id == uuid.UUID(r.json()["id"])

    hop = await client.get(f"/l/{link.code}")
    assert hop.status_code == 302, hop.text
    assert hop.headers["location"] == target
    assert hop.headers["cache-control"] == "no-store", "a cached hop stops counting clicks"

    set_org_context(session, org_id)
    click = (await session.execute(sa.select(LinkClick))).scalar_one()
    assert click.org_id == org_id, "the click belongs to the link's org, never the request"
    assert click.ip_hash and "127.0.0.1" not in click.ip_hash, "no raw IP is ever stored"
    refreshed = await session.get(ShortLink, link.id)
    await session.refresh(refreshed)
    assert refreshed.clicks == 1

    await client.get(f"/l/{link.code}")
    await session.refresh(refreshed)
    assert refreshed.clicks == 2, "clicks accumulate"

    listed = await client.get(
        f"/api/v1/messages/{r.json()['id']}", headers=auth_headers(token, org_id)
    )
    assert listed.json()["clicks"] == 2
    assert listed.json()["links"][0]["target_url"] == target


async def test_link_tracking_is_off_unless_asked_for(p28_app, session):
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "notrack", "+12145550714")
    target = "https://example.com/plain"
    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558815", "body": f"see {target}"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    assert target in fake.sent[0].text, "tracking must never be applied silently"
    set_org_context(session, org_id)
    assert (await session.execute(sa.select(ShortLink))).first() is None


async def test_short_code_unguessable_and_unique():
    codes = {links_svc.generate_code() for _ in range(500)}
    assert len(codes) == 500, "500 random codes must not collide"
    for code in codes:
        assert len(code) == links_svc.CODE_LENGTH
        assert all(ch in links_svc.CODE_ALPHABET for ch in code)
    assert links_svc.CODE_LENGTH >= 8, "a short code IS the credential for the redirect"


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "https://user:pw@evil.example.com/x",
        "data:text/html,<script>alert(1)</script>",
        "/relative/path",
    ],
)
async def test_short_link_redirect_rejects_an_unsafe_target(p28_app, session, target):
    """The open-redirect guard. A row that fails the safety check gets a 404 - never a
    302 - even though something once managed to write it."""
    from tests.conftest import create_org, register_and_login

    client, _fake, _app = p28_app
    slug = uuid.uuid4().hex[:8]
    token = await register_and_login(client, f"p28-unsafe-{slug}@example.com")
    org_id = uuid.UUID((await create_org(client, token, f"Org {slug}"))["id"])

    set_org_context(session, org_id)
    link = ShortLink(
        id=uuid.uuid4(), org_id=org_id, code=links_svc.generate_code(), target_url=target
    )
    session.add(link)
    await session.commit()

    hop = await client.get(f"/l/{link.code}")
    assert hop.status_code == 404, f"{target!r} must never be redirected to"


async def test_short_link_redirect_for_an_unknown_code_is_404(p28_app):
    client, _fake, _app = p28_app
    hop = await client.get("/l/doesnotexist")
    assert hop.status_code == 404


async def test_unsafe_urls_are_left_in_the_body_untracked(session, client):
    """A URL we will not redirect to is also a URL we will not shorten - it stays in the
    body as the sender typed it rather than becoming a dead link."""
    from tests.conftest import create_org, register_and_login

    token = await register_and_login(client, "p28-unsafe-body@example.com")
    org = await create_org(client, token, "Org unsafe")
    org_id = uuid.UUID(org["id"])
    body, links = await links_svc.create_tracked_links(
        session,
        org_id,
        body="mail me at ftp://files.example.com/x and https://ok.example.com/y",
        public_web_url=PUBLIC_WEB,
    )
    assert "ftp://files.example.com/x" in body
    assert len(links) == 1, "only the http(s) URL is tracked"


async def test_tenancy_short_links_are_not_visible_across_orgs(p28_app, session):
    client, _fake, _app = p28_app
    token_a, _oa, org_a, _na = await _org_with_number(client, "ten-a", "+12145550715")
    token_b, _ob, org_b, _nb = await _org_with_number(client, "ten-b", "+12145550716")

    ra = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558816", "body": "a https://a.example.com/1", "track_links": True},
        headers=auth_headers(token_a, org_a),
    )
    rb = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558817", "body": "b https://b.example.com/2", "track_links": True},
        headers=auth_headers(token_b, org_b),
    )
    assert ra.status_code == 201 and rb.status_code == 201

    set_org_context(session, org_a)
    rows = (await session.execute(sa.select(ShortLink))).scalars().all()
    assert {row.org_id for row in rows} == {org_a}, "org A must see only its own links"

    cross = await client.get(
        f"/api/v1/messages/{ra.json()['id']}", headers=auth_headers(token_b, org_b)
    )
    assert cross.status_code == 404, "another org's message is not found, not forbidden"


# ==================================================================================
# 4. Plain-English failure reasons
# ==================================================================================
@pytest.mark.parametrize(
    "code,fragment",
    [
        ("4750", "spam"),
        ("4770", "spam"),
        ("4720", "can't receive text"),
        ("4700", "working number"),
        ("4432", "asked not to receive"),
        ("4451", "isn't approved"),
        ("4405", "faster than"),
        ("5000", "temporary problem"),
        ("4740", "attachment"),
    ],
)
def test_failure_reason_mapping_table_covers_known_codes(code, fragment):
    reason = messaging_errors.public_reason(error_code=code)
    assert fragment in reason.lower(), f"{code} mapped to the wrong sentence: {reason!r}"
    assert reason != messaging_errors.GENERIC_REASON


def test_failure_reason_falls_back_by_category_then_generically():
    assert messaging_errors.public_reason(error_code="9999") == (
        messaging_errors.GENERIC_REASON
    )
    assert "setup needs attention" in messaging_errors.public_reason(category="auth")
    assert messaging_errors.public_reason() == messaging_errors.GENERIC_REASON


def test_every_failure_sentence_is_plain_english():
    banned = (
        "carrier", "dlr", "e.164", "10dlc", "sms gateway", "smpp",
        "bandwidth", "telnyx", "twilio", "plivo", "signalwire",
    )
    for sentence in messaging_errors.ALL_REASONS:
        assert len(sentence) <= 255, sentence
        lowered = sentence.lower()
        for word in banned:
            assert word not in lowered, f"{sentence!r} leaks the word {word!r}"


async def test_failed_send_records_a_plain_failure_reason(p28_app, session):
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "reason", "+12145550717")
    fake.scripted.append(_rejection("4750", "blocked"))

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558818", "body": "hi"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 201, r.text
    assert r.json()["failure_reason_public"] == messaging_errors.public_reason(
        error_code="4750"
    )
    row = await _message(session, org_id, r.json()["id"])
    assert row.status == "rejected"
    assert row.failure_reason_public is not None


async def test_a_successful_retry_clears_the_failure_reason(p28_app, session):
    """A stale sentence on a message that eventually went out is worse than none."""
    client, fake, _app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "clears", "+12145550718")
    fake.scripted.append(_rejection("5000", "network"))
    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725558819", "body": "hi"},
        headers=auth_headers(token, org_id),
    )
    row = await _message(session, org_id, r.json()["id"])
    assert row.failure_reason_public is not None

    await messaging_svc._dispatch_to_carrier(session, org_id, fake, row)
    await session.refresh(row)
    assert row.status == "accepted"
    assert row.failure_reason_public is None


# ==================================================================================
# 5. D43 — campaigns go through routing and record the sentence
# ==================================================================================
async def test_campaign_route_reason_recorded(p28_app, session):
    from app.models import Contact, ContactList, ContactListRow
    from app.services import outbound as outbound_svc

    client, fake, app = p28_app
    token, _org_row, org_id, _num = await _org_with_number(client, "d43", "+12145550719")
    contact_e164 = "+19725558820"

    set_org_context(session, org_id)
    contact_list = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv",
        status="ready", total_rows=1, accepted_count=1,
    )
    session.add(contact_list)
    await session.flush()
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="c")
    session.add(contact)
    await session.flush()
    session.add(
        ContactListRow(
            id=uuid.uuid4(), org_id=org_id, list_id=contact_list.id, row_number=1,
            raw={"phone": contact_e164}, e164=contact_e164, contact_id=contact.id,
            status="accepted",
        )
    )
    await session.commit()

    campaign = await outbound_svc.create_campaign(
        session, org_id, name="C", channel="sms", list_id=contact_list.id, body="Hello",
        rate_per_minute=600, daily_cap=200, respect_warmup=False, max_attempts=2,
        retry_backoff_minutes=240,
    )
    await outbound_svc.start_campaign(session, campaign)
    counts = await outbound_svc.outbound_tick(
        session, fake, None, Random(1), registry=app.state.carriers
    )
    assert counts["sent"] == 1, counts

    set_org_context(session, org_id)
    sent = (
        await session.execute(
            sa.select(Message).where(
                Message.to_e164 == contact_e164, Message.direction == "outbound"
            )
        )
    ).scalar_one()
    assert sent.route_reason, "D43: a campaign send must record its route sentence"
    assert sent.route_reason.startswith("Sent via"), sent.route_reason
