"""P41: daily messaging rollup, workspace health, breach warnings, ops view.

Source rows are seeded directly (same style as tests/test_reputation.py) and read back
through app.services.messaging_health and the two read APIs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    ConsentEvent,
    Message,
    MessageEvent,
    MessageThread,
    Notification,
    OrgMembership,
    OrgMessagingDaily,
    Role,
    User,
)
from app.providers.domain import DeliveryReceipt
from app.services import messaging as messaging_svc
from app.services import messaging_health as messaging_health_svc
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, auth_headers, make_org_with_number

OUR = "+12145550100"
OUR2 = "+12145550101"
NOW = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
OPS = {"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN}


async def _thread(session, org_id: uuid.UUID, contact_e164: str) -> MessageThread:
    set_org_context(session, org_id)
    existing = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == OUR,
                MessageThread.contact_e164 == contact_e164,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    thread = MessageThread(id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=contact_e164)
    session.add(thread)
    await session.flush()
    return thread


async def _seed_messages(
    session,
    org_id: uuid.UUID,
    *,
    count: int = 1,
    direction: str = "outbound",
    status: str = "delivered",
    carrier: str = "bandwidth",
    failure_class: str | None = None,
    error_code: str | None = None,
    created_at: datetime | None = None,
) -> list[Message]:
    """N otherwise-identical messages, stamped INSIDE the rollup day (half-open bounds)."""
    set_org_context(session, org_id)
    moment = created_at if created_at is not None else NOW - timedelta(minutes=1)
    messages: list[Message] = []
    for i in range(count):
        contact = f"+1972555{9000 + i:04d}"
        thread = await _thread(session, org_id, contact)
        msg = Message(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread.id,
            direction=direction,
            status=status,
            from_e164=OUR,
            to_e164=contact,
            body="x",
            media=[],
            carrier=carrier,
            error_code=error_code,
            failure_class=failure_class if direction == "outbound" else None,
        )
        session.add(msg)
        await session.flush()
        msg.created_at = moment
        messages.append(msg)
    await session.commit()
    return messages


async def _seed_consent(
    session,
    org_id: uuid.UUID,
    *,
    event: str,
    message_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> ConsentEvent:
    set_org_context(session, org_id)
    row = ConsentEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        contact_e164=OUR,
        channel="sms",
        event=event,
        source="test",
        keyword_matched=None,
        message_id=message_id,
        actor_user_id=None,
        details={},
    )
    session.add(row)
    await session.flush()
    row.created_at = created_at if created_at is not None else NOW - timedelta(minutes=1)
    await session.commit()
    return row


@pytest.fixture
async def org(client, session):
    token, org_row, _number = await make_org_with_number(
        client, "p41health@example.com", "Org Health", OUR
    )
    return token, uuid.UUID(org_row["id"])


# ==================================================================================
# The DLR path writes the bucket
# ==================================================================================
def test_apply_dlr_sets_failure_class_on_failure():
    message = Message(
        id=uuid.uuid4(),
        direction="outbound",
        status="sent",
        from_e164=OUR,
        to_e164="+19725559000",
        body="x",
        media=[],
        carrier="telnyx",
    )
    messaging_svc._apply_dlr_to_message(
        message, DeliveryReceipt(provider_message_id="pm-1", event_type="message-failed",
                                error_code="40002")
    )
    assert message.status == "failed"
    assert message.failure_class == "spam_blocked"


def test_apply_dlr_leaves_failure_class_null_on_delivered():
    message = Message(
        id=uuid.uuid4(),
        direction="outbound",
        status="sent",
        from_e164=OUR,
        to_e164="+19725559000",
        body="x",
        media=[],
        carrier="telnyx",
    )
    messaging_svc._apply_dlr_to_message(
        message, DeliveryReceipt(provider_message_id="pm-1", event_type="message-delivered")
    )
    assert message.status == "delivered"
    assert message.failure_class is None


# ==================================================================================
# rollup_day
# ==================================================================================
async def test_rollup_day_idempotent_and_corrects_late_receipts(org, session):
    _token, org_id = org
    day = NOW.date()

    await _seed_messages(session, org_id, count=2, status="delivered", created_at=NOW)
    failed_bandwidth = (
        await _seed_messages(session, org_id, count=1, status="failed", created_at=NOW)
    )[0]
    failed_telnyx = (
        await _seed_messages(
            session,
            org_id,
            count=1,
            status="failed",
            carrier="telnyx",
            failure_class="spam_blocked",
            created_at=NOW,
        )
    )[0]
    await _seed_messages(
        session, org_id, count=1, direction="inbound", status="received", created_at=NOW
    )
    await _seed_consent(
        session, org_id, event="opt_out", message_id=failed_bandwidth.id, created_at=NOW
    )
    await _seed_consent(
        session, org_id, event="help_request", message_id=failed_telnyx.id, created_at=NOW
    )

    touched = await messaging_health_svc.rollup_day(session, org_id, day)
    await session.commit()
    assert touched == 2

    by_carrier = await _rows_by_carrier(session, org_id, day)
    assert by_carrier["bandwidth"].sent == 3
    assert by_carrier["bandwidth"].delivered == 2
    assert by_carrier["bandwidth"].failed == 1
    assert by_carrier["bandwidth"].inbound == 1
    assert by_carrier["bandwidth"].opt_outs == 1
    assert by_carrier["telnyx"].sent == 1
    assert by_carrier["telnyx"].failed == 1
    assert by_carrier["telnyx"].failed_spam_blocked == 1
    assert by_carrier["telnyx"].help_requests == 1

    # Running the same day again must collide on the unique key, not double the counts.
    assert await messaging_health_svc.rollup_day(session, org_id, day) == 2
    await session.commit()
    again = await _rows_by_carrier(session, org_id, day)
    assert len(again) == 2
    assert again["bandwidth"].sent == 3
    assert again["bandwidth"].failed == 1
    assert again["telnyx"].failed == 1

    # A late DLR flips one failed row to delivered; the next rollup corrects the day.
    set_org_context(session, org_id)
    failed_bandwidth = await session.get(Message, failed_bandwidth.id)
    failed_bandwidth.status = "delivered"
    await session.commit()

    await messaging_health_svc.rollup_day(session, org_id, day)
    await session.commit()
    corrected = (await _rows_by_carrier(session, org_id, day))["bandwidth"]
    assert corrected.delivered == 3
    assert corrected.failed == 0


async def _rows_by_carrier(session, org_id: uuid.UUID, day) -> dict[str, OrgMessagingDaily]:
    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(OrgMessagingDaily).where(
                    OrgMessagingDaily.org_id == org_id,
                    OrgMessagingDaily.period_date == day,
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.carrier: row for row in rows}


async def test_failure_classes_land_in_their_columns(org, session):
    _token, org_id = org
    day = NOW.date()

    for failure_class, count in [
        ("spam_blocked", 1),
        ("carrier_rejected", 2),
        ("invalid_destination", 3),
        ("opted_out", 4),
    ]:
        await _seed_messages(
            session,
            org_id,
            count=count,
            status="failed",
            carrier="telnyx",
            failure_class=failure_class,
            created_at=NOW,
        )
    # NULL and the literal "unknown" are counted in `failed` and nowhere else.
    await _seed_messages(
        session, org_id, count=1, status="failed", carrier="telnyx", created_at=NOW
    )
    await _seed_messages(
        session,
        org_id,
        count=1,
        status="failed",
        carrier="telnyx",
        failure_class="unknown",
        created_at=NOW,
    )

    await messaging_health_svc.rollup_day(session, org_id, day)
    await session.commit()

    row = (await _rows_by_carrier(session, org_id, day))["telnyx"]
    assert row.failed == 12
    assert row.failed_spam_blocked == 1
    assert row.failed_carrier_rejected == 2
    assert row.failed_invalid_destination == 3
    assert row.failed_opted_out == 4

    summary = await messaging_health_svc.health(session, org_id, now=NOW)
    assert summary["failed_by_class"]["unknown"] == 2


# ==================================================================================
# health()
# ==================================================================================
async def test_health_no_data_then_ok_below_the_volume_floor(org, session):
    _token, org_id = org

    summary = await messaging_health_svc.health(session, org_id, now=NOW)
    assert summary["level"] == "no_data"
    assert summary["volume"] == 0
    assert summary["delivery_rate"] is None
    assert summary["spam_block_rate"] is None
    assert summary["opt_out_rate"] is None
    assert summary["reasons"] == []

    # 50% delivery, but only two texts: far below MIN_VOLUME, so nothing can breach.
    await _seed_messages(session, org_id, count=1, status="delivered", created_at=NOW)
    await _seed_messages(session, org_id, count=1, status="failed", created_at=NOW)
    await messaging_health_svc.rollup_day(session, org_id, NOW.date())
    await session.commit()

    summary = await messaging_health_svc.health(session, org_id, now=NOW)
    assert summary["volume"] == 2
    assert summary["delivery_rate"] == 0.5
    assert summary["level"] == "ok"
    assert summary["reasons"] == []
    assert summary["window_start"] == (NOW.date() - timedelta(days=6))
    assert summary["window_end"] == NOW.date()
    assert summary["thresholds"]["min_volume"] == 100.0


async def test_health_delivery_warn_then_critical(org, session):
    _token, org_id = org

    await _seed_messages(session, org_id, count=170, status="delivered", created_at=NOW)
    await _seed_messages(session, org_id, count=30, status="failed", created_at=NOW)
    await messaging_health_svc.rollup_day(session, org_id, NOW.date())
    await session.commit()

    summary = await messaging_health_svc.health(session, org_id, now=NOW)
    assert summary["level"] == "warn"
    assert "85.0%" in summary["reasons"][0]
    assert "200 texts" in summary["reasons"][0]

    # 300 delivered / 400 terminal = 75%, under the critical floor.
    await _seed_messages(session, org_id, count=130, status="delivered", created_at=NOW)
    await _seed_messages(session, org_id, count=70, status="failed", created_at=NOW)
    await messaging_health_svc.rollup_day(session, org_id, NOW.date())
    await session.commit()

    critical = await messaging_health_svc.health(session, org_id, now=NOW)
    assert critical["volume"] == 400
    assert critical["level"] == "critical"
    assert "75.0%" in critical["reasons"][0]


async def test_health_spam_block_threshold(org, session):
    _token, org_id = org

    await _seed_messages(session, org_id, count=190, status="delivered", created_at=NOW)
    await _seed_messages(
        session,
        org_id,
        count=10,
        status="failed",
        carrier="telnyx",
        failure_class="spam_blocked",
        created_at=NOW,
    )
    await messaging_health_svc.rollup_day(session, org_id, NOW.date())
    await session.commit()

    summary = await messaging_health_svc.health(session, org_id, now=NOW)
    assert summary["spam_block_rate"] == 0.05
    assert summary["level"] == "critical"
    assert any("Spam blocks 5.0% (10 texts)" in reason for reason in summary["reasons"])


async def test_health_opt_out_threshold(org, session):
    _token, org_id = org

    await _seed_messages(session, org_id, count=200, status="delivered", created_at=NOW)
    for _ in range(7):
        await _seed_consent(session, org_id, event="opt_out", created_at=NOW)
    await messaging_health_svc.rollup_day(session, org_id, NOW.date())
    await session.commit()

    summary = await messaging_health_svc.health(session, org_id, now=NOW)
    # 7 opt-outs over 200 delivered = 3.5%: past the warn line, under the critical one.
    assert summary["opt_out_rate"] == 0.035
    assert summary["level"] == "warn"
    assert any("Opt-outs 3.5%" in reason for reason in summary["reasons"])


# ==================================================================================
# notify_breaches()
# ==================================================================================
async def _ensure_user(session, email: str) -> User:
    existing = (
        await session.execute(sa.select(User).where(User.email == email))
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


async def test_notify_breaches_targets_owner_admin_and_dedupes(client, session):
    _token, org_row, _number = await make_org_with_number(
        client, "p41notify@example.com", "Org Notify", OUR
    )
    org_id = uuid.UUID(org_row["id"])

    await _seed_messages(session, org_id, count=170, status="delivered", created_at=NOW)
    await _seed_messages(session, org_id, count=30, status="failed", created_at=NOW)
    await messaging_health_svc.rollup_day(session, org_id, NOW.date())
    await session.commit()

    owner_user = await _ensure_user(session, "p41notify@example.com")
    admin_user = await _ensure_user(session, "p41notify_admin@example.com")
    member_user = await _ensure_user(session, "p41notify_member@example.com")
    await _ensure_membership(
        session, org_id, owner_user.id, (await _ensure_role(session, org_id, "owner")).id
    )
    await _ensure_membership(
        session, org_id, admin_user.id, (await _ensure_role(session, org_id, "admin")).id
    )
    await _ensure_membership(
        session, org_id, member_user.id, (await _ensure_role(session, org_id, "agent")).id
    )
    await session.commit()

    assert await messaging_health_svc.notify_breaches(session, now=NOW) == 2

    set_org_context(session, org_id)
    notifications = (
        (
            await session.execute(
                sa.select(Notification).where(
                    Notification.org_id == org_id,
                    Notification.kind == "messaging_health",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(notifications) == 2
    assert {n.user_id for n in notifications} == {owner_user.id, admin_user.id}
    assert all(n.body.startswith("Warning: ") for n in notifications)

    # The hourly tick runs again the same day: the dedupe key is what stops a second bell.
    assert await messaging_health_svc.notify_breaches(session, now=NOW) == 0


# ==================================================================================
# platform_rows() / receipts_check()
# ==================================================================================
async def test_platform_rows_sorted_worst_first(client, session):
    _t1, org_row1, _n1 = await make_org_with_number(client, "p41a@example.com", "Plat One", OUR)
    _t2, org_row2, _n2 = await make_org_with_number(client, "p41b@example.com", "Plat Two", OUR2)
    org1 = uuid.UUID(org_row1["id"])
    org2 = uuid.UUID(org_row2["id"])

    await _seed_messages(session, org1, count=150, status="delivered", created_at=NOW)
    await _seed_messages(session, org1, count=50, status="failed", created_at=NOW)
    await messaging_health_svc.rollup_day(session, org1, NOW.date())
    await session.commit()

    # Org 2 has a rollup row but no traffic at all: "no data", not 0%.
    set_org_context(session, org2)
    session.add(OrgMessagingDaily(id=uuid.uuid4(), org_id=org2, period_date=NOW.date(),
                                  carrier="bandwidth"))
    await session.commit()

    rows = await messaging_health_svc.platform_rows(session, now=NOW)
    assert [r["org_id"] for r in rows] == [org1, org2]
    assert rows[0]["level"] == "critical"
    assert rows[0]["org_name"] == "Plat One"
    assert rows[0]["first_breached_at"] is None
    assert rows[1]["level"] == "no_data"
    assert rows[1]["delivery_rate"] is None


async def test_receipts_check_reports_per_carrier(org, session):
    _token, org_id = org
    now = datetime.now(timezone.utc)

    bandwidth_msg = (
        await _seed_messages(session, org_id, count=1, created_at=now - timedelta(hours=2))
    )[0]
    telnyx_msg = (
        await _seed_messages(
            session, org_id, count=1, carrier="telnyx", created_at=now - timedelta(hours=2)
        )
    )[0]

    set_org_context(session, org_id)
    session.add(
        MessageEvent(
            id=uuid.uuid4(),
            message_id=bandwidth_msg.id,
            carrier="bandwidth",
            provider_message_id="pm-bandwidth-1",
            event_type="message-delivered",
            payload={},
            event_time=now - timedelta(hours=1),
        )
    )
    session.add(
        MessageEvent(
            id=uuid.uuid4(),
            message_id=telnyx_msg.id,
            carrier="telnyx",
            provider_message_id="pm-telnyx-1",
            event_type="message-failed",
            payload={},
            event_time=now - timedelta(hours=25),
        )
    )
    await session.commit()

    by_carrier = {row["carrier"]: row for row in await messaging_health_svc.receipts_check(session)}
    assert set(by_carrier) == {"bandwidth", "telnyx", "twilio", "plivo", "signalwire"}
    assert by_carrier["bandwidth"]["receipts_24h"] == 1
    assert by_carrier["bandwidth"]["last_receipt_at"] is not None
    # A receipt older than a day still proves the URL works; it just is not recent.
    assert by_carrier["telnyx"]["receipts_24h"] == 0
    assert by_carrier["telnyx"]["last_receipt_at"] is not None
    assert by_carrier["twilio"]["receipts_24h"] == 0
    assert by_carrier["twilio"]["last_receipt_at"] is None


# ==================================================================================
# Routes
# ==================================================================================
async def test_analytics_health_route_shape(client, session):
    token, org_row, _number = await make_org_with_number(
        client, "p41route@example.com", "Org Route", OUR
    )
    org_id = uuid.UUID(org_row["id"])
    now = datetime.now(timezone.utc)

    await _seed_messages(
        session, org_id, count=2, status="delivered", created_at=now - timedelta(minutes=10)
    )
    await messaging_health_svc.rollup_day(session, org_id, now.date())
    await session.commit()

    r = await client.get("/api/v1/analytics/health", headers=auth_headers(token, org_row["id"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data) == {
        "window_start",
        "window_end",
        "volume",
        "delivery_rate",
        "spam_block_rate",
        "opt_out_rate",
        "failed_by_class",
        "level",
        "reasons",
        "thresholds",
    }
    assert data["level"] == "ok"
    assert data["volume"] == 2
    assert set(data["failed_by_class"]) == {
        "spam_blocked",
        "carrier_rejected",
        "invalid_destination",
        "opted_out",
        "unknown",
    }
    assert set(data["thresholds"]) == {
        "delivery_warn",
        "delivery_critical",
        "spam_warn",
        "spam_critical",
        "opt_out_warn",
        "opt_out_critical",
        "min_volume",
    }


async def test_platform_messaging_health_route_is_ops_gated(client):
    r = await client.get("/api/v1/platform/messaging/health")
    assert r.status_code == 403

    r = await client.get("/api/v1/platform/messaging/health", headers=OPS)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["rows"], list)


async def test_platform_receipts_check_route_is_ops_gated(client):
    r = await client.get("/api/v1/platform/messaging/receipts-check")
    assert r.status_code == 403

    r = await client.get("/api/v1/platform/messaging/receipts-check", headers=OPS)
    assert r.status_code == 200, r.text
    assert [row["carrier"] for row in r.json()] == [
        "bandwidth",
        "telnyx",
        "twilio",
        "plivo",
        "signalwire",
    ]
