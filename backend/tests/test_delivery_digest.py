"""D2: the delivery digest - opt-in customer email, the ops digest, and breach emails.

The customer digest starts OFF and only an operator switch turns it on; the ops digest is
the main report and carries a BOTTOM LINE verdict. Breach warnings email owners/admins
once per level per week (services/messaging_health.notify_breaches).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ValidationFailedError
from app.models import (
    AuditLogEntry,
    Message,
    MessageEvent,
    MessageThread,
    Notification,
    OrgMembership,
    OrgMessagingDaily,
    PlatformSetting,
    ReportSchedule,
    Role,
    User,
)
from app.services import delivery_digest as delivery_digest_svc
from app.services import mailer
from app.services import messaging_health as messaging_health_svc
from tests.conftest import auth_headers, make_org_with_number, register_and_login
from tests.test_ops_console import _operator

OUR = "+12145550100"
OUR2 = "+12145550101"
#: The frozen clock test_p41 uses (13:00 America/Chicago on 2026-06-15).
NOW = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
DAY = date(2026, 6, 14)
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
# Seeding helpers (same style as tests/test_p41_messaging_health.py)
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
    moment = created_at if created_at is not None else NOW
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


async def _enable_digest(
    session,
    org_id: uuid.UUID,
    *,
    hour: int = 8,
    tz: str = "America/Chicago",
    recipients: list[str] | None = None,
) -> None:
    set_org_context(session, org_id)
    session.add(
        ReportSchedule(
            id=uuid.uuid4(),
            org_id=org_id,
            report="delivery",
            cadence="daily",
            params={"enabled": True, "hour": hour, "tz": tz},
            recipients=recipients or [],
        )
    )
    await session.commit()


async def _ensure_user(session, email: str) -> User:
    existing = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(id=uuid.uuid4(), email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    return user


async def _ensure_role(session, org_id: uuid.UUID, name: str) -> Role:
    set_org_context(session, org_id)
    existing = (
        await session.execute(sa.select(Role).where(Role.org_id == org_id, Role.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    role = Role(id=uuid.uuid4(), org_id=org_id, name=name, permissions=[], is_system=False)
    session.add(role)
    await session.flush()
    return role


async def _ensure_membership(
    session, org_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    set_org_context(session, org_id)
    existing = (
        await session.execute(
            sa.select(OrgMembership).where(
                OrgMembership.org_id == org_id,
                OrgMembership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user_id, role_id=role_id)
        )
    else:
        existing.role_id = role_id
    await session.flush()


async def _ensure_owner(session, org_id: uuid.UUID, email: str) -> User:
    user = await _ensure_user(session, email)
    await _ensure_membership(
        session, org_id, user.id, (await _ensure_role(session, org_id, "owner")).id
    )
    await session.commit()
    return user


async def _stamp_notifications(session, org_id: uuid.UUID, moment: datetime) -> None:
    """The bell rows a pass just wrote carry the wall clock; move them onto the clock the
    test is simulating so the weekly-mail window can be exercised."""
    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(Notification).where(Notification.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.created_at = moment
    await session.commit()


# ==================================================================================
# schedule_view
# ==================================================================================
def test_schedule_view_defaults_to_off_without_a_row():
    view = delivery_digest_svc.schedule_view(None)
    assert view == {
        "enabled": False,
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


def test_schedule_view_missing_params_is_off():
    row = ReportSchedule(
        org_id=uuid.uuid4(), report="delivery", cadence="daily", params={}, recipients=None
    )
    view = delivery_digest_svc.schedule_view(row)
    assert view["enabled"] is False
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
    await _add_rollup(
        session, org_id, DAY, sent=10, delivered=8, failed=2, carrier="bandwidth",
        failed_spam_blocked=1,
    )
    await _add_rollup(
        session, org_id, DAY, sent=5, delivered=3, failed=2, carrier="telnyx",
        failed_carrier_rejected=2,
    )

    moment = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    await _seed_messages(session, org_id, count=3, status="delivered", from_e164=OUR,
                         created_at=moment)
    await _seed_messages(session, org_id, count=1, status="failed", from_e164=OUR,
                         created_at=moment)
    await _seed_messages(session, org_id, count=2, status="delivered", from_e164=OUR2,
                         created_at=moment)

    digest = await delivery_digest_svc.build(session, org_id, DAY)
    assert digest is not None
    assert digest["day"] == DAY.isoformat()
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
    assert digest["trend"][-1]["date"] == DAY.isoformat()
    assert digest["trend"][-1]["sent"] == 15
    assert digest["trend"][0]["sent"] == 0
    assert digest["level"] == "ok"


async def test_build_returns_none_without_volume(org, session):
    _token, org_id = org
    assert await delivery_digest_svc.build(session, org_id, DAY) is None


async def test_build_other_failed_never_negative(org, session):
    _token, org_id = org
    # `failed` smaller than the class columns would be a data bug; other_failed clamps to 0.
    await _add_rollup(
        session, org_id, DAY, sent=6, delivered=4, failed=2, carrier="telnyx",
        failed_spam_blocked=5,
    )
    digest = await delivery_digest_svc.build(session, org_id, DAY)
    assert digest is not None
    assert digest["other_failed"] == 0


# ==================================================================================
# render
# ==================================================================================
def _digest(**overrides) -> dict:
    base = {
        "day": DAY.isoformat(),
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
                "date": (DAY - timedelta(days=6 - i)).isoformat(),
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
    subject, body = delivery_digest_svc.render(
        _digest(), app_name="Ringlite", day=DAY, base_url="https://app.ringlite.io"
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
        _digest(rate=None), app_name="Ringlite", day=DAY, base_url=""
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
        digest, app_name="Ringlite", day=DAY, base_url=""
    )
    assert "Recipient opted out: 3" in body
    assert "Blocked as spam" not in body
    assert "Rejected by the carrier" not in body
    assert "Invalid or unreachable number" not in body
    assert "Other:" not in body
    assert "Delivery needs attention" not in body


# ==================================================================================
# digest_tick (opt-in)
# ==================================================================================
async def test_digest_tick_sends_nothing_without_a_schedule_row(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, DAY, sent=5, delivered=4, failed=1)

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 0
    assert counts["skipped"] == 1
    assert mailer.outbox == []
    assert await delivery_digest_svc.get_schedule(session, org_id) is None


async def test_operator_enables_then_the_next_tick_sends(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "optin@example.com", "Org OptIn", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(session, org_id, DAY, sent=5, delivered=4, failed=1)
    ops_token = await _operator(client, session, "ops-optin@example.com", "admin")
    mailer.outbox.clear()

    assert (
        await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    )["sent"] == 0
    assert mailer.outbox == []

    r = await client.put(
        f"/api/v1/platform/orgs/{org_id}/delivery-digest",
        json={"enabled": True},
        headers=auth_headers(ops_token),
    )
    assert r.status_code == 200, r.text

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 1
    assert len(mailer.outbox) == 1
    assert "optin@example.com" in mailer.outbox[0]["To"]


async def test_digest_tick_sends_once_per_local_day(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, DAY, sent=5, delivered=4, failed=1)
    await _enable_digest(session, org_id)

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 1
    assert len(mailer.outbox) == 1
    assert "digest@example.com" in mailer.outbox[0]["To"]

    # The marker committed BEFORE the send stops a second email on the same local day.
    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 0
    assert counts["skipped"] == 1
    assert len(mailer.outbox) == 1


async def test_digest_tick_waits_for_the_send_hour(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, DAY, sent=5, delivered=4, failed=1)
    await _enable_digest(session, org_id, hour=20)

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EARLY)
    assert counts["sent"] == 0
    assert counts["skipped"] == 1
    assert mailer.outbox == []


async def test_digest_tick_sends_again_on_the_next_local_day(org, session, settings):
    _token, org_id = org
    await _add_rollup(session, org_id, DAY, sent=5, delivered=4, failed=1)
    await _enable_digest(session, org_id)
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
    await _add_rollup(session, org_id, DAY, sent=5, delivered=5, failed=0)
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
    await _add_rollup(session, org_id, DAY, sent=5, delivered=4, failed=1)
    await _enable_digest(session, org_id, recipients=["digest-ops@example.com"])

    counts = await delivery_digest_svc.digest_tick(session, settings, now=CHICAGO_EVENING)
    assert counts["sent"] == 1
    assert "digest-ops@example.com" in mailer.outbox[0]["To"]
    assert "digest@example.com" not in mailer.outbox[0]["To"]


async def test_digest_tick_zero_volume_still_marks_last_sent(org, session, settings):
    _token, org_id = org
    await _enable_digest(session, org_id)
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
# ops digest - the BOTTOM LINE
# ==================================================================================
async def test_ops_digest_bottom_line_all_clear(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "clear@example.com", "Clear Co", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(session, org_id, DAY, sent=30, delivered=30, failed=0)
    # bandwidth sent AND reported back, so nothing is silent.
    await _seed_message_event(
        session,
        org_id,
        carrier="bandwidth",
        event_type="message-delivered",
        event_time=datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc),
    )

    subject, body = await delivery_digest_svc.build_ops_digest(
        session, DAY, now=CHICAGO_EVENING
    )
    assert "all clear" in subject
    assert body.splitlines()[0] == (
        "BOTTOM LINE: All clear. 30 texts, 100% delivered across 1 workspaces."
    )
    assert "Customer daily emails ON" in body
    assert "none" in body.splitlines()[-1]


async def test_ops_digest_bottom_line_action_needed(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "failing@example.com", "Failing Co", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    # 70% delivered on 30 texts: critical and above the volume floor.
    await _add_rollup(
        session, org_id, DAY, sent=30, delivered=21, failed=9, carrier="bandwidth",
        failed_spam_blocked=9,
    )
    # bandwidth sent yesterday and reported nothing -> silent.
    await _seed_messages(
        session, org_id, count=3, carrier="bandwidth",
        created_at=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
    )
    # telnyx sent and DID report back -> not silent.
    await _seed_message_event(
        session,
        org_id,
        carrier="telnyx",
        event_type="message-delivered",
        event_time=datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc),
    )

    subject, body = await delivery_digest_svc.build_ops_digest(
        session, DAY, now=CHICAGO_EVENING
    )
    assert subject.endswith("ACTION NEEDED (2)")
    assert "BOTTOM LINE: Action needed." in body
    assert (
        "- Failing Co: 70% delivered (9 of 30 failed), mostly Blocked as spam. "
        "Customer warned: no." in body
    )
    assert "- bandwidth: no delivery receipts in 24 h while sending" in body
    # A carrier with no traffic yesterday is quiet, not broken.
    assert "- twilio:" not in body
    assert "- plivo:" not in body
    assert "- signalwire:" not in body


async def test_ops_digest_bottom_line_says_customer_warned(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "warned@example.com", "Warned Co", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(session, org_id, DAY, sent=30, delivered=21, failed=9)
    owner = await _ensure_owner(session, org_id, "warned@example.com")
    set_org_context(session, org_id)
    note = Notification(
        id=uuid.uuid4(),
        org_id=org_id,
        user_id=owner.id,
        kind="messaging_health",
        body="Warning: Delivery rate 85.0% over the last 7 days.",
    )
    session.add(note)
    await session.flush()
    note.created_at = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    await session.commit()

    _subject, body = await delivery_digest_svc.build_ops_digest(
        session, DAY, now=CHICAGO_EVENING
    )
    assert "- Warned Co: 70% delivered (9 of 30 failed)" in body
    assert "Customer warned: yes." in body


async def test_ops_digest_tick_sends_once_per_local_day(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "ops-owner@example.com", "Ops Digest", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(
        session, org_id, DAY, sent=30, delivered=20, failed=10, carrier="bandwidth",
        failed_spam_blocked=10,
    )
    await _seed_messages(
        session, org_id, count=3, carrier="bandwidth",
        created_at=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
    )
    ops_token = await _operator(client, session, "ops-digest@example.com", "admin")
    mailer.outbox.clear()

    assert ops_token  # the operator token is what the daily report goes to
    assert (
        await delivery_digest_svc.ops_digest_tick(session, settings, now=CHICAGO_EVENING)
        is True
    )
    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert "ops-digest@example.com" in message["To"]
    assert "ACTION NEEDED" in message["Subject"]
    body = _plain_body(message)
    assert "BOTTOM LINE: Action needed." in body
    assert "Ops Digest" in body
    assert "bandwidth" in body

    # The platform setting marker stops a second pass the same Chicago day.
    sent_again = await delivery_digest_svc.ops_digest_tick(
        session, settings, now=CHICAGO_EVENING
    )
    assert sent_again is False
    assert len(mailer.outbox) == 1


async def test_ops_digest_tick_before_the_hour_is_a_noop(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "ops-early@example.com", "Ops Early", OUR
    )
    await _operator(client, session, "ops-early-op@example.com", "admin")
    mailer.outbox.clear()
    assert await delivery_digest_svc.ops_digest_tick(session, settings, now=CHICAGO_EARLY) is False
    assert mailer.outbox == []


# ==================================================================================
# Customer warning emails (messaging_health.notify_breaches)
# ==================================================================================
async def test_breach_email_goes_out_once_per_level(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "breach@example.com", "Org Breach", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(session, org_id, NOW.date(), sent=200, delivered=170, failed=30)
    await _ensure_owner(session, org_id, "breach@example.com")
    mailer.outbox.clear()

    created = await messaging_health_svc.notify_breaches(session, now=NOW, settings=settings)
    assert created >= 1
    assert len(mailer.outbox) == 1
    assert "breach@example.com" in mailer.outbox[0]["To"]
    assert "delivery warning" in mailer.outbox[0]["Subject"]
    assert "Delivery rate 85.0%" in _plain_body(mailer.outbox[0])
    assert "What to do:" in _plain_body(mailer.outbox[0])

    # The next day writes a NEW bell row for the same level - but no second email in the
    # same week.
    await _stamp_notifications(session, org_id, NOW)
    mailer.outbox.clear()
    created = await messaging_health_svc.notify_breaches(
        session, now=NOW + timedelta(days=1), settings=settings
    )
    assert created >= 1
    assert mailer.outbox == []


async def test_breach_email_escalates_from_warning_to_critical(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "escalate@example.com", "Org Escalate", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(session, org_id, NOW.date(), sent=200, delivered=150, failed=50)
    owner = await _ensure_owner(session, org_id, "escalate@example.com")
    # Yesterday's WARNING bell: still inside the weekly window for that level...
    set_org_context(session, org_id)
    prior = Notification(
        id=uuid.uuid4(),
        org_id=org_id,
        user_id=owner.id,
        kind="messaging_health",
        body="Warning: delivery rate dropped",
    )
    session.add(prior)
    await session.flush()
    prior.created_at = NOW - timedelta(days=1)
    await session.commit()
    mailer.outbox.clear()

    # ... but nothing CRITICAL has gone out, so the escalation emails immediately.
    created = await messaging_health_svc.notify_breaches(session, now=NOW, settings=settings)
    assert created >= 1
    assert len(mailer.outbox) == 1
    assert "texts are failing on your workspace" in mailer.outbox[0]["Subject"]
    assert "Delivery rate 75.0%" in _plain_body(mailer.outbox[0])


async def test_breach_email_is_skipped_without_settings(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "nosettings@example.com", "Org NoSettings", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _add_rollup(session, org_id, NOW.date(), sent=200, delivered=170, failed=30)
    await _ensure_owner(session, org_id, "nosettings@example.com")
    mailer.outbox.clear()

    # The pre-existing call shape: bells yes, mail no.
    created = await messaging_health_svc.notify_breaches(session, now=NOW)
    assert created >= 1
    assert mailer.outbox == []


async def test_rollup_tick_passes_settings_through_to_the_breach_email(client, session, settings):
    _token, org_row, _number = await make_org_with_number(
        client, "rollup@example.com", "Org Rollup", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    await _seed_messages(session, org_id, count=170, status="delivered", created_at=NOW)
    await _seed_messages(session, org_id, count=30, status="failed", created_at=NOW)
    await _ensure_owner(session, org_id, "rollup@example.com")
    mailer.outbox.clear()

    counts = await messaging_health_svc.rollup_tick(session, now=NOW, settings=settings)
    assert counts["notifications"] >= 1
    assert len(mailer.outbox) == 1
    assert "rollup@example.com" in mailer.outbox[0]["To"]


# ==================================================================================
# Routes
# ==================================================================================
async def test_customer_delivery_digest_routes_are_gone(client):
    r = await client.get("/api/v1/analytics/delivery-digest")
    assert r.status_code in (404, 405), r.status_code
    r = await client.put("/api/v1/analytics/delivery-digest", json={"enabled": True})
    assert r.status_code in (404, 405), r.status_code


async def test_platform_digests_list_shows_every_org_in_the_health_table(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "plat-digest@example.com", "Plat Digest", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    today = datetime.now(timezone.utc).date()
    await _add_rollup(session, org_id, today, sent=5, delivered=5, failed=0)
    ops_token = await _operator(client, session, "ops-ro@example.com", "read_only")

    r = await client.get("/api/v1/platform/messaging/digests", headers=auth_headers(ops_token))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [row["org_id"] for row in rows] == [str(org_id)]
    row = rows[0]
    assert row["org_name"] == "Plat Digest"
    assert row["enabled"] is False
    assert row["hour"] == 8
    assert row["tz"] == "America/Chicago"
    assert row["recipients"] == []
    assert row["default_recipients"] == ["plat-digest@example.com"]
    assert row["last_sent_at"] is None

    client.cookies.clear()  # the operator login above left a session cookie on the client
    r = await client.get("/api/v1/platform/messaging/digests")
    assert r.status_code in (401, 403), r.status_code


async def test_platform_digest_put_requires_ops_admin(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "put-guard@example.com", "Put Guard", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    customer_token = await register_and_login(client, "not-an-operator@example.com")

    r = await client.put(
        f"/api/v1/platform/orgs/{org_id}/delivery-digest",
        json={"enabled": True},
        headers=auth_headers(customer_token),
    )
    assert r.status_code in (401, 403), r.text

    r = await client.put(
        f"/api/v1/platform/orgs/{org_id}/delivery-digest", json={"enabled": True}
    )
    assert r.status_code in (401, 403), r.text


async def test_platform_digest_put_enables_and_writes_audit(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "put-org@example.com", "Put Org", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    ops_token = await _operator(client, session, "ops-admin@example.com", "admin")

    r = await client.put(
        f"/api/v1/platform/orgs/{org_id}/delivery-digest",
        json={
            "enabled": True,
            "hour": 7,
            "tz": "America/New_York",
            "recipients": ["Boss@Example.com", "boss@example.com", "ops@example.com"],
        },
        headers=auth_headers(ops_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["hour"] == 7
    assert body["tz"] == "America/New_York"
    assert body["recipients"] == ["boss@example.com", "ops@example.com"]
    assert body["org_name"] == "Put Org"

    await session.rollback()
    set_org_context(session, org_id)
    entries = (
        (
            await session.execute(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.action == "platform.delivery_digest_updated"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].detail["enabled"] is True
    assert entries[0].detail["hour"] == 7

    # An unknown org is a 404, not a new schedule row.
    r = await client.put(
        f"/api/v1/platform/orgs/{uuid.uuid4()}/delivery-digest",
        json={"enabled": True},
        headers=auth_headers(ops_token),
    )
    assert r.status_code == 404, r.text


async def test_platform_digest_test_send_goes_only_to_the_operator(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "send-only@example.com", "Send Only", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    now = datetime.now(timezone.utc)
    await _seed_messages(session, org_id, count=3, status="delivered", created_at=now)
    await messaging_health_svc.rollup_day(session, org_id, now.date())
    await session.commit()
    ops_token = await _operator(client, session, "ops-send@example.com", "read_only")
    mailer.outbox.clear()

    r = await client.post(
        f"/api/v1/platform/orgs/{org_id}/delivery-digest/test",
        headers=auth_headers(ops_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert r.json()["day"] == now.date().isoformat()
    assert len(mailer.outbox) == 1
    assert "ops-send@example.com" in mailer.outbox[0]["To"]


async def test_platform_digest_test_send_without_data(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "nodata@example.com", "No Data", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    ops_token = await _operator(client, session, "ops-nodata@example.com", "read_only")
    mailer.outbox.clear()

    r = await client.post(
        f"/api/v1/platform/orgs/{org_id}/delivery-digest/test",
        headers=auth_headers(ops_token),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": False, "day": None}
    assert mailer.outbox == []


async def test_platform_ops_digest_test_does_not_set_the_marker(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "ops-test@example.com", "Ops Test", OUR
    )
    ops_token = await _operator(client, session, "ops-opsdigest@example.com", "read_only")
    mailer.outbox.clear()

    r = await client.post(
        "/api/v1/platform/messaging/ops-digest/test", headers=auth_headers(ops_token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert len(mailer.outbox) == 1
    assert "ops-opsdigest@example.com" in mailer.outbox[0]["To"]

    # The daily marker is untouched: the real report is still owed for the day.
    await session.rollback()
    marker = (
        await session.execute(
            sa.select(PlatformSetting)
            .where(PlatformSetting.key == delivery_digest_svc.OPS_SETTING_KEY)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    assert marker is None
