"""D2: the daily delivery digest - schedule, build/render, ticks and the three routes."""

from __future__ import annotations

import itertools
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import (
    Message,
    MessageEvent,
    MessageThread,
    OrgMembership,
    OrgMessagingDaily,
    ReportSchedule,
    Role,
    User,
)
from app.services import delivery_digest as delivery_digest_svc
from app.services import mailer
from app.services import messaging_health as messaging_health_svc
from tests.conftest import (
    auth_headers,
    make_org_with_number,
    make_platform_operator,
    register_and_login,
)

OUR = "+12145550100"
OUR2 = "+12145550101"
#: 09:00 America/Chicago - at/after the default 08:00 send hour.
CHICAGO_EVENING = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
#: 06:00 America/Chicago - before the default send hour.
CHICAGO_EARLY = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)

_CONTACT_SEQ = itertools.count(1)


def _next_contact() -> str:
    return f"+1972555{next(_CONTACT_SEQ):04d}"


@pytest.fixture(autouse=True)
def _clear_outbox():
    mailer.outbox.clear()
    yield
    mailer.outbox.clear()


@pytest.fixture
async def org(client, session):
    token, org_row, _number = await make_org_with_number(
        client, "digest@example.com", "Org Digest", OUR
    )
    # Registration itself sends confirmation mail; the tests below count digest mail only.
    mailer.outbox.clear()
    return token, uuid.UUID(org_row["id"])


# ----------------------------------------------------------------------------------
# Seeding helpers (mirrors tests/test_p41_messaging_health.py)
# ----------------------------------------------------------------------------------
def _plain_body(message) -> str:
    part = message.get_body(preferencelist=("plain",))
    return part.get_content() if part is not None else ""


async def _thread(session, org_id, our_e164: str, contact: str) -> MessageThread:
    set_org_context(session, org_id)
    existing = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == our_e164,
                MessageThread.contact_e164 == contact,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=our_e164, contact_e164=contact
    )
    session.add(thread)
    await session.flush()
    return thread


async def _seed_messages(
    session,
    org_id: uuid.UUID,
    *,
    count: int = 1,
    status: str = "delivered",
    carrier: str = "bandwidth",
    created_at: datetime | None = None,
    from_e164: str = OUR,
) -> list[Message]:
    set_org_context(session, org_id)
    moment = created_at if created_at is not None else CHICAGO_EVENING
    messages: list[Message] = []
    for _ in range(count):
        contact = _next_contact()
        thread = await _thread(session, org_id, from_e164, contact)
        msg = Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread.id,
            direction="outbound",
            status=status,
            from_e164=from_e164,
            to_e164=contact,
            body="x",
            media=[],
            carrier=carrier,
        )
        session.add(msg)
        await session.flush()
        msg.created_at = moment
        messages.append(msg)
    await session.commit()
    return messages


async def _seed_message_event(
    session, org_id: uuid.UUID, *, carrier: str, event_type: str, event_time: datetime
) -> None:
    messages = await _seed_messages(
        session,
        org_id,
        count=1,
        carrier=carrier,
        created_at=event_time - timedelta(hours=1),
    )
    set_org_context(session, org_id)
    session.add(
        MessageEvent(
            id=uuid.uuid4(),
            message_id=messages[0].id,
            carrier=carrier,
            provider_message_id=f"pm-{carrier}-{uuid.uuid4().hex[:8]}",
            event_type=event_type,
            payload={},
            event_time=event_time,
        )
    )
    await session.commit()


async def _add_rollup(
    session,
    org_id: uuid.UUID,
    day: date,
    *,
    sent: int = 0,
    delivered: int = 0,
    failed: int = 0,
    carrier: str = "bandwidth",
    **extra: int,
) -> None:
    set_org_context(session, org_id)
    session.add(
        OrgMessagingDaily(
            id=uuid.uuid4(),
            org_id=org_id,
            period_date=day,
            carrier=carrier,
            sent=sent,
            delivered=delivered,
            failed=failed,
            **extra,
        )
    )
    await session.commit()


# ==================================================================================
# schedule_view
# ==================================================================================
def test_schedule_view_defaults_without_a_row():
    view = delivery_digest_svc.schedule_view(None)
    assert view == {
        "enabled": True,
        "hour": 8,
        "tz": "America/Chicago",
        "recipients": [],
        "last_sent_at": None,
    }


def test_schedule_view_invalid_tz_falls_back():
    row = ReportSchedule(
        org_id=uuid.uuid4(),
        report="delivery",
        cadence="daily",
        params={"enabled": False, "hour": 20, "tz": "Not/AZone"},
        recipients=["boss@example.com"],
    )
    view = delivery_digest_svc.schedule_view(row)
    assert view["enabled"] is False
    assert view["hour"] == 20
    assert view["tz"] == "America/Chicago"
    assert view["recipients"] == ["boss@example.com"]
    assert view["last_sent_at"] is None


def test_schedule_view_missing_params_uses_defaults():
    row = ReportSchedule(
        org_id=uuid.uuid4(), report="delivery", cadence="daily", params={}, recipients=None
    )
    view = delivery_digest_svc.schedule_view(row)
    assert view["enabled"] is True
    assert view["hour"] == 8
    assert view["tz"] == "America/Chicago"
    assert view["recipients"] == []


# ==================================================================================
# save_schedule
# ==================================================================================
async def test_save_schedule_validation(org, session):
    _token, org_id = org

    with pytest.raises(ValidationFailedError):
        await delivery_digest_svc.save_schedule(
            session, org_id, enabled=True, hour=25, tz="America/Chicago", recipients=[],
            user_id=None,
        )
    with pytest.raises(ValidationFailedError):
        await delivery_digest_svc.save_schedule(
            session, org_id, enabled=True, hour=8, tz="Not/AZone", recipients=[], user_id=None
        )
    with pytest.raises(ValidationFailedError):
        await delivery_digest_svc.save_schedule(
            session, org_id, enabled=True, hour=8, tz="America/Chicago",
            recipients=["not-an-email"], user_id=None,
        )
    with pytest.raises(ValidationFailedError):
        await delivery_digest_svc.save_schedule(
            session, org_id, enabled=True, hour=8, tz="America/Chicago",
            recipients=[f"user{i}@example.com" for i in range(21)], user_id=None,
        )


async def test_save_schedule_dedupes_and_lowercases(org, session):
    _token, org_id = org
    await delivery_digest_svc.save_schedule(
        session,
        org_id,
        enabled=True,
        hour=7,
        tz="UTC",
        recipients=["A@Example.com", "a@example.com", " b@example.com ", "B@example.com"],
        user_id=None,
    )
    row = await delivery_digest_svc.get_schedule(session, org_id)
    assert row is not None
    assert row.recipients == ["a@example.com", "b@example.com"]
    assert row.params["hour"] == 7
    assert row.params["tz"] == "UTC"
    assert row.cadence == "daily"


async def test_save_schedule_updates_the_existing_row(org, session):
    _token, org_id = org
    await delivery_digest_svc.save_schedule(
        session, org_id, enabled=True, hour=7, tz="UTC", recipients=["a@example.com"],
        user_id=None,
    )
    await delivery_digest_svc.save_schedule(
        session, org_id, enabled=False, hour=9, tz="America/New_York", recipients=[],
        user_id=None,
    )
    rows = (
        (
            await session.execute(
                sa.select(ReportSchedule).where(
                    ReportSchedule.org_id == org_id,
                    ReportSchedule.report == "delivery",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    view = delivery_digest_svc.schedule_view(rows[0])
    assert view["enabled"] is False
    assert view["hour"] == 9
    assert view["tz"] == "America/New_York"


async def test_default_recipients_are_owner_and_admin(org, session):
    _token, org_id = org
    emails = await delivery_digest_svc.default_recipients(session, org_id)
    assert emails == ["digest@example.com"]


# ==================================================================================
# build
# ==================================================================================
async def test_build_sums_carriers_and_lists_numbers(org, session):
    _token, org_id = org
    day = date(2026, 6, 14)
    await _add_rollup(
        session, org_id, day, sent=10, delivered=8, failed=2, carrier="bandwidth",
        failed_spam_blocked=1,
    )
    await _add_rollup(
        session, org_id, day, sent=5, delivered=3, failed=2, carrier="telnyx",
        failed_carrier_rejected=2,
    )

    moment = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    await _seed_messages(session, org_id, count=3, status="delivered", from_e164=OUR,
                         created_at=moment)
    await _seed_messages(session, org_id, count=1, status="failed", from_e164=OUR,
                         created_at=moment)
    await _seed_messages(session, org_id, count=2, status="delivered", from_e164=OUR2,
                         created_at=moment)

    digest = await delivery_digest_svc.build(session, org_id, day)
    assert digest is not None
    assert digest["day"] == day.isoformat()
    assert digest["sent"] == 15
    assert digest["delivered"] == 11
    assert digest["failed"] == 4
    assert digest["failed_by_class"]["failed_spam_blocked"] == 1
    assert digest["failed_by_class"]["failed_carrier_rejected"] == 2
    assert digest["other_failed"] == 1
    assert digest["rate"] == pytest.approx(11 / 15)
    assert digest["pending"] == 0
    assert [row["number"] for row in digest["numbers"]] == [OUR, OUR2]
    assert digest["numbers"][0] == {"number": OUR, "sent": 4, "delivered": 3, "failed": 1}
    assert len(digest["trend"]) == 7
    assert digest["trend"][-1]["date"] == day.isoformat()
    assert digest["trend"][-1]["sent"] == 15
    assert digest["trend"][0]["sent"] == 0
    assert digest["level"] == "ok"


async def test_build_returns_none_without_volume(org, session):
    _token, org_id = org
    assert await delivery_digest_svc.build(session, org_id, date(2026, 6, 14)) is None


async def test_build_other_failed_never_negative(org, session):
    _token, org_id = org
    day = date(2026, 6, 14)
    # `failed` smaller than the class columns would be a data bug; other_failed clamps to 0.
    await _add_rollup(
        session, org_id, day, sent=6, delivered=4, failed=2, carrier="telnyx",
        failed_spam_blocked=5,
    )
    digest = await delivery_digest_svc.build(session, org_id, day)
    assert digest is not None
    assert digest["other_failed"] == 0


# ==================================================================================
# render
# ==================================================================================
def _digest(**overrides) -> dict:
    day = date(2026, 6, 14)
    base = {
        "day": day.isoformat(),
        "sent": 100,
        "delivered": 90,
        "failed": 10,
        "failed_by_class": {
            "failed_spam_blocked": 4,
            "failed_carrier_rejected": 0,
            "failed_invalid_destination": 0,
            "failed_opted_out": 0,
        },
        "other_failed": 6,
        "rate": 0.9,
        "pending": 0,
        "numbers": [{"number": "+12145550100", "sent": 40, "delivered": 38, "failed": 2}],
        "trend": [
            {
                "date": (day - timedelta(days=6 - i)).isoformat(),
                "sent": 10,
                "delivered": 9,
                "failed": 1,
            }
            for i in range(7)
        ],
        "level": "warn",
    }
    base.update(overrides)
    return base


def test_render_subject_and_no_carrier_names():
    day = date(2026, 6, 14)
    subject, body = delivery_digest_svc.render(
        _digest(), app_name="Ringlite", day=day, base_url="https://app.ringlite.io"
    )
    assert subject == "Ringlite: texts on Sun Jun 14 \u2014 100 sent, 90% delivered"

    lowered = body.lower()
    for name in ("telnyx", "bandwidth", "signalwire", "twilio", "plivo"):
        assert name not in lowered

    assert "Why texts failed" in body
    assert "Blocked as spam: 4" in body
    assert "Other: 6" in body
    assert "(214) 555-0100" in body
    assert "Last 7 days" in body
    assert "Delivery needs attention: see Analytics" in body
    assert "https://app.ringlite.io/settings/messaging" in body
    assert "to change or turn off this email" in body


def test_render_subject_handles_missing_rate():
    subject, _body = delivery_digest_svc.render(
        _digest(rate=None), app_name="Ringlite", day=date(2026, 6, 14), base_url=""
    )
    assert subject == "Ringlite: texts on Sun Jun 14 \u2014 100 sent, n/a delivered"


def test_render_only_nonzero_failure_lines():
    digest = _digest(
        failed_by_class={
            "failed_spam_blocked": 0,
            "failed_carrier_rejected": 0,
            "failed_invalid_destination": 0,
            "failed_opted_out": 3,
        },
        other_failed=0,
        level="ok",
    )
    _subject, body = delivery_digest_svc.render(
        digest, app_name="Ringlite", day=date(2026, 6, 14), base_url=""
    )
    assert "Recipient opted out: 3" in body
    assert "Blocked as spam" not in body
    assert "Rejected by the carrier" not in body
    assert "Invalid or unreachable number" not in body
    assert "Other:" not in body
    assert "Delivery needs attention" not in body


# ==================================================================================
# digest_tick
# ==================================================================================
async def test_digest_tick_skips_before_the_send_hour(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, date(2026, 6, 14), sent=5, delivered=4, failed=1)

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EARLY)
    assert counts["sent"] == 0
    assert counts["skipped"] == 1
    assert mailer.outbox == []


async def test_digest_tick_sends_once_per_local_day(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, date(2026, 6, 14), sent=5, delivered=4, failed=1)

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 1
    assert len(mailer.outbox) == 1
    assert "digest@example.com" in mailer.outbox[0]["To"]

    # The marker committed BEFORE the send stops a second email on the same local day.
    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 0
    assert counts["skipped"] == 1
    assert len(mailer.outbox) == 1


async def test_digest_tick_sends_again_on_the_next_local_day(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, date(2026, 6, 14), sent=5, delivered=4, failed=1)
    assert (
        await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    )["sent"] == 1

    await _add_rollup(session, org_id, date(2026, 6, 15), sent=7, delivered=6, failed=1)
    counts = await delivery_digest_svc.digest_tick(
        session, settings, now=datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)
    )
    assert counts["sent"] == 1
    assert len(mailer.outbox) == 2


async def test_digest_tick_disabled_schedule_sends_nothing(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, date(2026, 6, 14), sent=5, delivered=5, failed=0)
    set_org_context(session, org_id)
    session.add(
        ReportSchedule(
            id=uuid.uuid4(),
            org_id=org_id,
            report="delivery",
            params={"enabled": False, "hour": 8, "tz": "America/Chicago"},
            cadence="daily",
            recipients=[],
        )
    )
    await session.commit()

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 0
    assert counts["skipped"] == 1
    assert mailer.outbox == []


async def test_digest_tick_explicit_recipients_override_defaults(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, date(2026, 6, 14), sent=5, delivered=4, failed=1)
    set_org_context(session, org_id)
    session.add(
        ReportSchedule(
            id=uuid.uuid4(),
            org_id=org_id,
            report="delivery",
            params={"enabled": True, "hour": 8, "tz": "America/Chicago"},
            cadence="daily",
            recipients=["digest-ops@example.com"],
        )
    )
    await session.commit()

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 1
    assert "digest-ops@example.com" in mailer.outbox[0]["To"]
    assert "digest@example.com" not in mailer.outbox[0]["To"]


async def test_digest_tick_zero_volume_still_marks_last_sent(org, session, settings):
    _token, org_id = org
    # In the candidate window but not on the digest's day, so build() finds nothing.
    await _add_rollup(session, org_id, date(2026, 6, 15), sent=3, delivered=3, failed=0)

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["empty"] == 1
    assert counts["sent"] == 0
    assert mailer.outbox == []

    row = await delivery_digest_svc.get_schedule(session, org_id)
    assert row is not None
    assert row.last_sent_at is not None


# ==================================================================================
# ops_digest_tick
# ==================================================================================
async def test_ops_digest_tick_once_per_local_day(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "ops-digest@example.com", "Ops Digest", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await make_platform_operator("ops-digest@example.com", "admin")

    day = date(2026, 6, 14)
    await _add_rollup(
        session, org_id, day, sent=30, delivered=20, failed=10, carrier="telnyx"
    )
    await _seed_message_event(
        session,
        org_id,
        carrier="telnyx",
        event_type="message-delivered",
        event_time=CHICAGO_EVENING - timedelta(hours=1),
    )

    mailer.outbox.clear()
    assert await delivery_digest_svc.ops_digest_tick(session, settings, now=CHICAGO_EVENING) is True
    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert "ops-digest@example.com" in message["To"]

    body = _plain_body(message)
    # The ops audience is us: carrier names are expected here.
    assert "telnyx" in body
    assert "bandwidth" in body
    assert "Ops Digest" in body
    assert "NO RECEIPTS 24h" in body

    # The platform setting marker stops a second pass the same Chicago day.
    sent_again = await delivery_digest_svc.ops_digest_tick(session, settings, now=CHICAGO_EVENING)
    assert sent_again is False
    assert len(mailer.outbox) == 1


async def test_ops_digest_tick_before_the_hour_is_a_noop(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "ops-early@example.com", "Ops Early", OUR
    )
    await make_platform_operator("ops-early@example.com", "admin")
    mailer.outbox.clear()
    assert await delivery_digest_svc.ops_digest_tick(session, settings, now=CHICAGO_EARLY) is False
    assert mailer.outbox == []


# ==================================================================================
# Routes
# ==================================================================================
async def test_delivery_digest_get_defaults(client):
    token, org_row, _number = await make_org_with_number(
        client, "digest-get@example.com", "Org Get", OUR
    )
    r = await client.get(
        "/api/v1/analytics/delivery-digest", headers=auth_headers(token, org_row["id"])
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is True
    assert data["hour"] == 8
    assert data["tz"] == "America/Chicago"
    assert data["recipients"] == []
    assert data["default_recipients"] == ["digest-get@example.com"]
    assert data["last_sent_at"] is None


async def test_delivery_digest_put_then_get_round_trip(client):
    token, org_row, _number = await make_org_with_number(
        client, "digest-put@example.com", "Org Put", OUR
    )
    headers = auth_headers(token, org_row["id"])

    r = await client.put(
        "/api/v1/analytics/delivery-digest",
        headers=headers,
        json={
            "enabled": True,
            "hour": 7,
            "tz": "America/New_York",
            "recipients": ["Boss@Example.com", "boss@example.com", "ops@example.com"],
        },
    )
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["hour"] == 7
    assert saved["tz"] == "America/New_York"
    assert saved["recipients"] == ["boss@example.com", "ops@example.com"]

    r = await client.get("/api/v1/analytics/delivery-digest", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["recipients"] == ["boss@example.com", "ops@example.com"]
    assert r.json()["hour"] == 7


async def test_delivery_digest_put_rejects_a_bad_hour(client):
    token, org_row, _number = await make_org_with_number(
        client, "digest-badhour@example.com", "Org BadHour", OUR
    )
    r = await client.put(
        "/api/v1/analytics/delivery-digest",
        headers=auth_headers(token, org_row["id"]),
        json={"enabled": True, "hour": 25, "tz": "America/Chicago", "recipients": []},
    )
    assert r.status_code == 422, r.text


async def test_delivery_digest_put_requires_settings_write(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "digest-owner@example.com", "Org Perm", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    member_token = await register_and_login(client, "digest-member@example.com")

    member = (
        await session.execute(
            sa.select(User)
            .where(User.email == "digest-member@example.com")
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    set_org_context(session, org_id)
    role = Role(
        id=uuid.uuid4(),
        org_id=org_id,
        name="digest_reader",
        permissions=["reports:read"],
        is_system=False,
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=member.id, role_id=role.id)
    )
    await session.commit()

    r = await client.put(
        "/api/v1/analytics/delivery-digest",
        headers=auth_headers(member_token, org_row["id"]),
        json={"enabled": True, "hour": 8, "tz": "America/Chicago", "recipients": []},
    )
    assert r.status_code == 403, r.text


async def test_delivery_digest_test_send_goes_only_to_the_caller(client, session):
    token, org_row, _number = await make_org_with_number(
        client, "digest-test@example.com", "Org Test", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    now = datetime.now(timezone.utc)
    await _seed_messages(session, org_id, count=3, status="delivered", created_at=now)
    await messaging_health_svc.rollup_day(session, org_id, now.date())
    await session.commit()

    mailer.outbox.clear()
    r = await client.post(
        "/api/v1/analytics/delivery-digest/test",
        headers=auth_headers(token, org_row["id"]),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sent"] is True
    assert data["day"] == now.date().isoformat()
    assert len(mailer.outbox) == 1
    assert "digest-test@example.com" in mailer.outbox[0]["To"]


async def test_delivery_digest_test_send_without_data(client):
    token, org_row, _number = await make_org_with_number(
        client, "digest-nodata@example.com", "Org NoData", OUR
    )
    mailer.outbox.clear()
    r = await client.post(
        "/api/v1/analytics/delivery-digest/test",
        headers=auth_headers(token, org_row["id"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": False, "day": None}
    assert mailer.outbox == []
