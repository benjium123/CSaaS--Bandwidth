"""Telnyx balance alerts: one email per level per drop ($25 warn, $10 urgent, $0 stopped),
at most one reminder a day, one "back to normal", and the state survives a restart."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import PlatformSetting
from app.services import break_glass, mailer, telnyx_balance_alert

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    out: list[str] = []

    async def fake_send(settings, to, subject, body, *args, **kwargs):  # noqa: ANN001
        out.append(subject)

    async def fake_admins(session):  # noqa: ANN001
        return ["ops@example.com"]

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(break_glass, "admin_emails", fake_admins)
    return out


async def _tick(session, settings, monkeypatch, cents, *, at=T0):  # noqa: ANN001
    monkeypatch.setattr(telnyx_balance_alert, "_last_run", None)
    monkeypatch.setattr(telnyx_balance_alert, "_now", lambda: at)

    async def fetch(_settings):  # noqa: ANN001
        return None if cents is None else (cents, cents)

    return await telnyx_balance_alert.tick(session, settings, fetch=fetch)


def test_levels():
    level = telnyx_balance_alert.level_for
    assert [level(c) for c in (2_500, 2_499, 1_000, 999, 1, 0, -50)] == [
        "ok", "warn", "warn", "urgent", "urgent", "stopped", "stopped"
    ]


async def test_each_level_emails_once_then_recovers(session, settings, sent, monkeypatch):
    await _tick(session, settings, monkeypatch, 5_000)
    assert sent == []

    await _tick(session, settings, monkeypatch, 2_000)  # under $25
    await _tick(session, settings, monkeypatch, 1_900, at=T0 + timedelta(hours=1))
    assert len(sent) == 1 and sent[0].startswith("Telnyx balance low")

    await _tick(session, settings, monkeypatch, 900, at=T0 + timedelta(hours=2))  # under $10
    assert len(sent) == 2 and sent[1].startswith("URGENT")

    await _tick(session, settings, monkeypatch, 0, at=T0 + timedelta(hours=3))  # empty
    assert len(sent) == 3 and "empty" in sent[2]

    await _tick(session, settings, monkeypatch, 3_000, at=T0 + timedelta(hours=4))  # recovered
    assert len(sent) == 4 and "back to normal" in sent[3]
    await _tick(session, settings, monkeypatch, 3_000, at=T0 + timedelta(hours=5))
    assert len(sent) == 4

    # Re-armed: the next drop alerts again.
    await _tick(session, settings, monkeypatch, 2_000, at=T0 + timedelta(hours=6))
    assert len(sent) == 5 and sent[4].startswith("Telnyx balance low")


async def test_one_reminder_a_day_while_still_low(session, settings, sent, monkeypatch):
    await _tick(session, settings, monkeypatch, 2_000)
    await _tick(session, settings, monkeypatch, 2_000, at=T0 + timedelta(hours=23))
    assert len(sent) == 1
    await _tick(session, settings, monkeypatch, 2_000, at=T0 + timedelta(hours=24))
    assert len(sent) == 2 and sent[1].startswith("Reminder: Telnyx balance low")
    await _tick(session, settings, monkeypatch, 2_000, at=T0 + timedelta(hours=30))
    assert len(sent) == 2


async def test_state_survives_a_restart(session, settings, sent, monkeypatch):
    await _tick(session, settings, monkeypatch, 2_000)
    row = await session.get(PlatformSetting, telnyx_balance_alert.STATE_KEY)
    assert row.value["level"] == "warn"
    # A new process (deploy) has no memory; the stored level stops a second email.
    session.expire_all()
    await _tick(session, settings, monkeypatch, 2_000, at=T0 + timedelta(minutes=30))
    assert len(sent) == 1


async def test_a_failed_lookup_never_alerts(session, settings, sent, monkeypatch):
    assert await _tick(session, settings, monkeypatch, None) == {}
    assert sent == []


async def test_checks_at_most_every_15_minutes(session, settings, sent, monkeypatch):
    calls = []

    async def fetch(_settings):  # noqa: ANN001
        calls.append(1)
        return (5_000, 5_000)

    monkeypatch.setattr(telnyx_balance_alert, "_last_run", None)
    await telnyx_balance_alert.tick(session, settings, fetch=fetch)
    await telnyx_balance_alert.tick(session, settings, fetch=fetch)
    assert len(calls) == 1
