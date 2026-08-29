"""Phase 14 DR-7: derived, trailing-7-day per-number reputation monitoring.

No third-party reputation API - everything here is seeded directly into `messages` and
read back through `app.services.reputation`, the sweeper tick, and the read API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import AuditLogEntry, Message, MessageThread
from app.services import reputation as reputation_svc
from tests.conftest import auth_headers, make_org_with_number

OUR = "+12145550100"
NOW = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)


async def _thread(session, org_id: uuid.UUID, our_e164: str, contact_e164: str) -> MessageThread:
    set_org_context(session, org_id)
    existing = (
        await session.execute(
            sa.select(MessageThread).where(
                MessageThread.our_e164 == our_e164, MessageThread.contact_e164 == contact_e164
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=our_e164, contact_e164=contact_e164
    )
    session.add(thread)
    await session.flush()
    return thread


async def _seed(
    session,
    org_id: uuid.UUID,
    *,
    from_e164: str = OUR,
    carrier: str = "bandwidth",
    delivered: int = 0,
    failed: int = 0,
    rejected: int = 0,
    spam_codes: list[str] | None = None,
    created_at: datetime | None = None,
) -> None:
    """Insert N outbound messages in the given statuses, all otherwise identical. Each
    `spam_codes` entry is inserted as an extra `failed` row carrying that error code (on
    top of the plain `failed`/`rejected` counts above), so a test can ask for both a
    volume/rate shape AND a specific spam-class code in one call."""
    set_org_context(session, org_id)
    # Strictly INSIDE the window by default - compute_number_stats' upper bound is
    # exclusive (`created_at < now`, half-open like usage.py's day bounds), so a message
    # stamped at exactly `now` would be silently excluded.
    moment = created_at if created_at is not None else NOW - timedelta(minutes=1)
    contact_base = 9000
    rows: list[tuple[str, str | None]] = (
        [("delivered", None)] * delivered
        + [("failed", None)] * failed
        + [("rejected", None)] * rejected
        + [("failed", code) for code in (spam_codes or [])]
    )
    for i, (status, error_code) in enumerate(rows):
        contact = f"+1972555{contact_base + i:04d}"
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
            error_code=error_code,
        )
        session.add(msg)
        await session.flush()
        msg.created_at = moment
    await session.commit()


@pytest.fixture
async def org(client, session):
    token, org_row, _number = await make_org_with_number(client, "rep1@example.com", "Org Rep", OUR)
    return token, uuid.UUID(org_row["id"])


# ==================================================================================
# compute_number_stats: rates
# ==================================================================================
async def test_delivery_rate_is_delivered_over_carrier_terminal_rows(org, session):
    _token, org_id = org
    await _seed(session, org_id, delivered=45, failed=15, rejected=10)

    stats = await reputation_svc.compute_number_stats(session, org_id, now=NOW)
    row = next(s for s in stats if s.e164 == OUR)

    # denominator is delivered+failed=60, NOT +rejected - rejected never reached the
    # carrier, mixing it in would understate a healthy number's real performance.
    assert row.delivered == 45
    assert row.failed == 15
    assert row.rejected == 10
    assert row.volume == 70
    assert row.delivery_rate == pytest.approx(45 / 60)


async def test_no_carrier_terminal_rows_gives_none_not_zero(org, session):
    _token, org_id = org
    await _seed(session, org_id, rejected=5)

    stats = await reputation_svc.compute_number_stats(session, org_id, now=NOW)
    row = next(s for s in stats if s.e164 == OUR)
    assert row.delivery_rate is None, "no data must never render as 0% delivery"


async def test_numbers_with_zero_traffic_are_still_reported(org, session):
    _token, org_id = org
    set_org_context(session, org_id)
    stats = await reputation_svc.compute_number_stats(session, org_id, now=NOW)
    row = next(s for s in stats if s.e164 == OUR)
    assert row.volume == 0
    assert row.delivery_rate is None
    assert row.spam_class_error_count == 0


async def test_traffic_outside_the_window_is_excluded(org, session):
    _token, org_id = org
    stale = NOW - timedelta(days=reputation_svc.WINDOW_DAYS, hours=1)
    await _seed(session, org_id, delivered=1, failed=1, created_at=stale)

    stats = await reputation_svc.compute_number_stats(session, org_id, now=NOW)
    row = next(s for s in stats if s.e164 == OUR)
    assert row.volume == 0


# ==================================================================================
# Spam-class codes (Bandwidth only - see OPEN_ISSUES D29 for why Telnyx is empty)
# ==================================================================================
async def test_bandwidth_spam_class_code_is_counted():
    assert "4770" in reputation_svc.BANDWIDTH_SPAM_CLASS_CODES
    assert "4750" in reputation_svc.BANDWIDTH_SPAM_CLASS_CODES


async def test_telnyx_spam_class_codes_are_deliberately_empty():
    """OPEN_ISSUES D29: the codes that looked plausible (40002/40003) collide with
    providers/telnyx/errors.py's own _INVALID_REQUEST set, and there is no live Telnyx
    traffic yet (B1) to validate a real list against - populate this once there is."""
    assert reputation_svc.TELNYX_SPAM_CLASS_CODES == frozenset()


async def test_spam_class_error_count_is_carrier_specific(org, session):
    _token, org_id = org
    # A code that IS spam-class for bandwidth is not blindly counted for a telnyx number.
    await _seed(session, org_id, carrier="bandwidth", spam_codes=["4770"])
    stats = await reputation_svc.compute_number_stats(session, org_id, now=NOW)
    row = next(s for s in stats if s.e164 == OUR)
    assert row.spam_class_error_count == 1


# ==================================================================================
# Alerting: threshold + idempotency
# ==================================================================================
async def test_low_delivery_over_min_volume_writes_one_alert(org, session):
    _token, org_id = org
    # volume=60 >= MIN_VOLUME_FOR_DELIVERY_ALERT(50), delivery_rate=45/60=0.75 < 0.85
    await _seed(session, org_id, delivered=45, failed=15)

    alerts = await reputation_svc.check_reputation(session, org_id, now=NOW)
    assert alerts == 1

    rows = (
        await session.execute(
            sa.select(AuditLogEntry).where(
                AuditLogEntry.org_id == org_id,
                AuditLogEntry.action == reputation_svc.ALERT_ACTION,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].target_id == OUR
    assert rows[0].detail["delivery_rate"] == pytest.approx(0.75)


async def test_low_delivery_under_min_volume_does_not_alert(org, session):
    _token, org_id = org
    # Same 75% rate, but volume=6 < MIN_VOLUME_FOR_DELIVERY_ALERT(50).
    await _seed(session, org_id, delivered=4, failed=2)

    alerts = await reputation_svc.check_reputation(session, org_id, now=NOW)
    assert alerts == 0


async def test_volume_gate_uses_carrier_terminal_count_not_total_volume(org, session):
    """Opus review: `rejected` sends never reached the carrier - padding with them must
    not let an org cross the 50-send floor on a tiny real delivery sample."""
    _token, org_id = org
    # delivered+failed = 6 (well under 50); rejected=100 pads total volume way past 50.
    await _seed(session, org_id, delivered=4, failed=2, rejected=100)

    alerts = await reputation_svc.check_reputation(session, org_id, now=NOW)
    assert alerts == 0, "rejected sends must not count toward the volume floor"


async def test_healthy_delivery_over_min_volume_does_not_alert(org, session):
    _token, org_id = org
    await _seed(session, org_id, delivered=95, failed=5)  # 95% >= 85%

    alerts = await reputation_svc.check_reputation(session, org_id, now=NOW)
    assert alerts == 0


async def test_any_spam_class_error_alerts_regardless_of_volume(org, session):
    _token, org_id = org
    await _seed(session, org_id, spam_codes=["4770"])  # volume=1, well under the floor

    alerts = await reputation_svc.check_reputation(session, org_id, now=NOW)
    assert alerts == 1


async def test_alert_is_idempotent_per_number_per_utc_day(org, session):
    _token, org_id = org
    await _seed(session, org_id, delivered=45, failed=15)

    first = await reputation_svc.check_reputation(session, org_id, now=NOW)
    second = await reputation_svc.check_reputation(session, org_id, now=NOW + timedelta(hours=2))
    assert first == 1
    assert second == 0, "same UTC day - must not write a second alert row"

    rows = (
        await session.execute(
            sa.select(AuditLogEntry).where(
                AuditLogEntry.org_id == org_id,
                AuditLogEntry.action == reputation_svc.ALERT_ACTION,
            )
        )
    ).scalars().all()
    assert len(rows) == 1

    next_day = await reputation_svc.check_reputation(session, org_id, now=NOW + timedelta(days=1))
    assert next_day == 1, "a NEW UTC day may alert again"


# ==================================================================================
# Sweeper tick: per-org commit
# ==================================================================================
async def test_reputation_tick_covers_every_org_and_commits_per_org(client, session):
    token_a, org_a, _ = await make_org_with_number(client, "repA@example.com", "Org A", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "repB@example.com", "Org B", "+12145550101"
    )
    org_a_id = uuid.UUID(org_a["id"])
    org_b_id = uuid.UUID(org_b["id"])

    await _seed(session, org_a_id, from_e164=OUR, delivered=45, failed=15)
    await _seed(session, org_b_id, from_e164="+12145550101", spam_codes=["4770"])

    counts = await reputation_svc.reputation_tick(session, now=NOW)
    assert counts["orgs"] >= 2
    assert counts["alerts"] >= 2

    rows = (
        await session.execute(
            sa.select(AuditLogEntry.org_id)
            .where(AuditLogEntry.action == reputation_svc.ALERT_ACTION)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    assert org_a_id in rows
    assert org_b_id in rows


# ==================================================================================
# Read API
# ==================================================================================
async def test_reputation_endpoint_returns_derived_stats(client, session, org):
    token, org_id = org
    # The read endpoint has no `now` override (it always anchors on the real clock, unlike
    # the service-level tests above which drive a frozen `now`) - seed with real "now" so
    # the seeded rows fall inside its trailing-7-day window.
    await _seed(session, org_id, delivered=45, failed=15, created_at=datetime.now(timezone.utc))

    r = await client.get("/api/v1/numbers/reputation", headers=auth_headers(token, str(org_id)))
    assert r.status_code == 200, r.text
    body = r.json()
    row = next(x for x in body if x["e164"] == OUR)
    assert row["delivered"] == 45
    assert row["failed"] == 15
    assert row["delivery_rate"] == pytest.approx(0.75)
    assert row["carrier"] == "bandwidth"


async def test_reputation_endpoint_is_org_scoped(client, session):
    token_a, org_a, _ = await make_org_with_number(client, "repC@example.com", "Org C", OUR)
    token_b, org_b, _ = await make_org_with_number(
        client, "repD@example.com", "Org D", "+12145550102"
    )

    await _seed(session, uuid.UUID(org_a["id"]), from_e164=OUR, delivered=10)

    r = await client.get(
        "/api/v1/numbers/reputation", headers=auth_headers(token_b, org_b["id"])
    )
    assert r.status_code == 200
    body = r.json()
    assert all(row["e164"] != OUR for row in body), "org B must never see org A's number"
