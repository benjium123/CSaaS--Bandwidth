"""The Telnyx balance alert emails ops admins once per drop under $25."""

import pytest

from app.services import break_glass, mailer, telnyx_balance_alert


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[list[str], str]] = []

    async def fake_send(settings, to, subject, body, *args, **kwargs):  # noqa: ANN001
        out.append((list(to), subject))

    async def fake_admins(session):  # noqa: ANN001
        return ["ops@example.com"]

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(break_glass, "admin_emails", fake_admins)
    monkeypatch.setattr(telnyx_balance_alert, "_alerted", False)
    return out


async def _tick(session, settings, monkeypatch, found):  # noqa: ANN001
    monkeypatch.setattr(telnyx_balance_alert, "_last_run", None)

    async def fetch(_settings):  # noqa: ANN001
        return found

    return await telnyx_balance_alert.tick(session, settings, fetch=fetch)


async def test_alerts_once_per_drop_under_the_floor(session, settings, sent, monkeypatch):
    assert await _tick(session, settings, monkeypatch, (5_000, 5_000)) == {
        "telnyx_available_cents": 5_000
    }
    assert sent == []

    out = await _tick(session, settings, monkeypatch, (2_499, 2_499))
    assert out["telnyx_balance_alerted"] == 1
    assert sent == [(["ops@example.com"], sent[0][1])]
    assert "$24.99" in sent[0][1]

    # Still low: no second email.
    await _tick(session, settings, monkeypatch, (1_000, 1_000))
    assert len(sent) == 1

    # Recovers, then drops again: one more email.
    await _tick(session, settings, monkeypatch, (3_000, 3_000))
    await _tick(session, settings, monkeypatch, (900, 900))
    assert len(sent) == 2


async def test_a_failed_lookup_never_alerts(session, settings, sent, monkeypatch):
    assert await _tick(session, settings, monkeypatch, None) == {}
    assert sent == []


async def test_checks_at_most_every_15_minutes(session, settings, sent, monkeypatch):
    calls = []

    async def fetch(_settings):  # noqa: ANN001
        calls.append(1)
        return (100, 100)

    monkeypatch.setattr(telnyx_balance_alert, "_last_run", None)
    await telnyx_balance_alert.tick(session, settings, fetch=fetch)
    await telnyx_balance_alert.tick(session, settings, fetch=fetch)
    assert len(calls) == 1
