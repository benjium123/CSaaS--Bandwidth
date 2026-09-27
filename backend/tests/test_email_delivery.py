from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import email_delivery, mailer
from tests.conftest import make_settings
from tests.test_ops_console import ops, ops_settings  # noqa: F401 - fixtures

DELIVERED = {
    "id": "msg-delivered",
    "status": "completed",
    "events": [
        {"type": "queued"},
        {"type": "sending"},
        {"type": "sent"},
        {"type": "delivered", "payload": {"smtp_code": "250"}},
    ],
    "recipient_statuses": {"delivered": 1},
}

FAILED = {
    "id": "145408f0-...",
    "status": "completed",
    "events": [
        {"type": "queued"},
        {"type": "sending"},
        {"type": "injection_timeout", "payload": {"recipient": "a@b.c"}},
        {
            "type": "failed",
            "payload": {
                "ambiguous": ["a@b.c"],
                "ambiguous_timeout": True,
                "failed": [],
                "succeeded": [],
            },
        },
    ],
    "recipient_statuses": {"injection_timeout": 1},
}


@pytest.fixture(autouse=True)
def _reset_email_delivery(monkeypatch):
    email_delivery._last_alert_at = None
    email_delivery._tasks.clear()
    monkeypatch.setattr(email_delivery, "CHECK_DELAYS_SECONDS", (0, 0, 0))


def _prod_settings(**overrides):
    return make_settings().model_copy(update={"app_env": "production", **overrides})


async def test_classify_delivered():
    assert email_delivery.classify(DELIVERED) == ("delivered", [])


async def test_classify_failed():
    verdict, reasons = email_delivery.classify(FAILED)
    assert verdict == "failed"
    assert reasons == ["failed", "injection_timeout"]


async def test_classify_pending():
    assert email_delivery.classify({"events": [{"type": "queued"}], "recipient_statuses": {}}) == (
        "pending",
        [],
    )
    assert email_delivery.classify({"recipient_statuses": {"sent": 1}}) == ("pending", [])


async def test_classify_garbage_input():
    assert email_delivery.classify({"events": None, "recipient_statuses": None}) == ("pending", [])
    assert email_delivery.classify({"events": "not-a-list", "recipient_statuses": {"sent": 1}}) == (
        "pending",
        [],
    )


async def test_mask():
    assert email_delivery.mask("304534 is your Ringlite code") == "###### is your Ringlite code"
    assert email_delivery.mask("Code 12") == "Code 12"


async def test_follow_up_delivered_first_check(monkeypatch):
    settings = make_settings()

    async def fake_fetch(settings, message_id):
        return DELIVERED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)
    calls = []

    async def fake_retry(*args, **kwargs):
        calls.append("retry")

    async def fake_alert(*args, **kwargs):
        calls.append("alert")

    monkeypatch.setattr(email_delivery, "_retry", fake_retry)
    monkeypatch.setattr(email_delivery, "_alert_operators", fake_alert)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 1)

    assert calls == []


async def test_follow_up_pending_then_delivered(monkeypatch):
    settings = make_settings()
    responses = [
        {"events": [], "recipient_statuses": {}},
        {"events": [], "recipient_statuses": {"sent": 1}},
        DELIVERED,
    ]
    fetch_count = 0

    async def fake_fetch(settings, message_id):
        nonlocal fetch_count
        response = responses[fetch_count]
        fetch_count += 1
        return response

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)
    retry_calls = []

    async def fake_retry(*args, **kwargs):
        retry_calls.append(args)

    monkeypatch.setattr(email_delivery, "_retry", fake_retry)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 1)

    assert fetch_count == 3
    assert retry_calls == []


async def test_follow_up_pending_unconfirmed(monkeypatch):
    settings = make_settings()
    fetch_count = 0

    async def fake_fetch(settings, message_id):
        nonlocal fetch_count
        fetch_count += 1
        return {"events": [{"type": "queued"}], "recipient_statuses": {}}

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)
    retry_calls = []
    alert_calls = []

    async def fake_retry(*args, **kwargs):
        retry_calls.append(args)

    async def fake_alert(*args, **kwargs):
        alert_calls.append(args)

    monkeypatch.setattr(email_delivery, "_retry", fake_retry)
    monkeypatch.setattr(email_delivery, "_alert_operators", fake_alert)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 1)

    # Still pending after every check: retried once like a failure, not dropped.
    assert fetch_count == 3
    assert len(retry_calls) == 1
    assert alert_calls == []

    # The retry also never confirms: operators are alerted.
    await email_delivery._follow_up(settings, "msg2", ["a@b.c"], "s", "b", 2)
    assert len(retry_calls) == 1
    assert len(alert_calls) == 1 and alert_calls[0][3] == ["unconfirmed"]


async def test_follow_up_failed_attempt1_telnyx(monkeypatch):
    settings = make_settings()
    monkeypatch.setattr(
        settings,
        "resend_api_key",
        SimpleNamespace(get_secret_value=lambda: ""),
    )

    async def fake_fetch(settings, message_id):
        return FAILED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    telnyx_calls = []

    async def fake_telnyx(settings, recipients, subject, body):
        telnyx_calls.append((recipients, subject, body))
        return True, "msg-retry"

    monkeypatch.setattr(mailer, "_telnyx_post", fake_telnyx)

    track_calls = []

    def fake_track(settings, message_id, recipients, subject, body, *, attempt=1):
        track_calls.append((message_id, attempt, recipients, subject, body))

    monkeypatch.setattr(email_delivery, "track", fake_track)

    alert_calls = []

    async def fake_alert(*args, **kwargs):
        alert_calls.append(args)

    monkeypatch.setattr(email_delivery, "_alert_operators", fake_alert)

    await email_delivery._follow_up(settings, "msg-orig", ["a@b.c"], "s", "b", 1)

    assert len(telnyx_calls) == 1
    assert telnyx_calls[0] == (["a@b.c"], "s", "b")
    assert track_calls == [("msg-retry", 2, ["a@b.c"], "s", "b")]
    assert alert_calls == []


async def test_follow_up_failed_attempt1_resend(monkeypatch):
    settings = make_settings()
    monkeypatch.setattr(
        settings,
        "resend_api_key",
        SimpleNamespace(get_secret_value=lambda: "resend-key"),
    )

    async def fake_fetch(settings, message_id):
        return FAILED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    resend_calls = []

    async def fake_resend(settings, recipients, subject, body):
        resend_calls.append((recipients, subject, body))
        return True

    monkeypatch.setattr(mailer, "_resend_post", fake_resend)

    telnyx_calls = []

    async def fake_telnyx(settings, recipients, subject, body):
        telnyx_calls.append((recipients, subject, body))
        return True, "msg-retry"

    monkeypatch.setattr(mailer, "_telnyx_post", fake_telnyx)

    await email_delivery._follow_up(settings, "msg-orig", ["a@b.c"], "s", "b", 1)

    assert resend_calls == [(["a@b.c"], "s", "b")]
    assert telnyx_calls == []


async def test_follow_up_failed_attempt2_alerts(monkeypatch):
    settings = make_settings()

    async def fake_fetch(settings, message_id):
        return FAILED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    operator_emails = ["admin@ringlite.io", "op@ringlite.io"]

    async def fake_operator_emails():
        return operator_emails

    monkeypatch.setattr(email_delivery, "_operator_emails", fake_operator_emails)

    send_calls = []

    async def fake_send(settings, to, subject, body, *, follow_up=True):
        send_calls.append((to, subject, body, follow_up))
        return True

    monkeypatch.setattr(mailer, "send", fake_send)

    await email_delivery._follow_up(
        settings,
        "msg",
        ["customer@example.com"],
        "304534 is your Ringlite code",
        "Your code is 304534",
        2,
    )

    assert len(send_calls) == 1
    to, alert_subject, body, follow_up = send_calls[0]
    assert to == operator_emails
    assert alert_subject == "Ringlite: an email failed to deliver"
    assert follow_up is False
    assert "######" in body
    assert "example.com" in body
    assert "304534" not in body
    assert "customer@example.com" not in body


async def test_follow_up_alert_cooldown(monkeypatch):
    settings = make_settings()

    async def fake_fetch(settings, message_id):
        return FAILED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    async def fake_operator_emails():
        return ["admin@ringlite.io"]

    monkeypatch.setattr(email_delivery, "_operator_emails", fake_operator_emails)

    send_calls = []

    async def fake_send(settings, to, subject, body, *, follow_up=True):
        send_calls.append((to, subject, body, follow_up))
        return True

    monkeypatch.setattr(mailer, "send", fake_send)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 2)
    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 2)

    assert len(send_calls) == 1


async def test_follow_up_no_operators_no_send(monkeypatch):
    settings = make_settings()

    async def fake_fetch(settings, message_id):
        return FAILED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    async def fake_operator_emails():
        return []

    monkeypatch.setattr(email_delivery, "_operator_emails", fake_operator_emails)

    send_calls = []

    async def fake_send(settings, to, subject, body, *, follow_up=True):
        send_calls.append((to, subject, body, follow_up))
        return True

    monkeypatch.setattr(mailer, "send", fake_send)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 2)

    assert send_calls == []


async def test_follow_up_fetch_none_unconfirmed(monkeypatch):
    settings = make_settings()

    async def fake_fetch(settings, message_id):
        return None

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)
    calls = []

    async def fake_retry(*args, **kwargs):
        calls.append("retry")

    async def fake_alert(*args, **kwargs):
        calls.append("alert")

    monkeypatch.setattr(email_delivery, "_retry", fake_retry)
    monkeypatch.setattr(email_delivery, "_alert_operators", fake_alert)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 1)

    assert calls == []


async def test_track_noop_in_test():
    settings = make_settings()
    email_delivery.track(settings, "msg-1", ["a@b.c"], "s", "b")
    assert email_delivery._tasks == set()


async def test_track_production_schedules_and_removes(monkeypatch):
    settings = _prod_settings()

    async def fake_fetch(settings, message_id):
        return DELIVERED

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    email_delivery.track(settings, "msg-1", ["a@b.c"], "s", "b")
    assert len(email_delivery._tasks) == 1

    for _ in range(20):
        await asyncio.sleep(0)
        if not email_delivery._tasks:
            break

    assert email_delivery._tasks == set()


async def test_follow_up_crash_does_not_raise(monkeypatch):
    settings = make_settings()

    async def fake_fetch(settings, message_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(email_delivery, "fetch_message", fake_fetch)

    await email_delivery._follow_up(settings, "msg", ["a@b.c"], "s", "b", 1)


async def test_mailer_send_tracks_follow_up(monkeypatch):
    settings = _prod_settings(telnyx_email_from="mail@ringlite.io")

    async def fake_telnyx_post(settings, recipients, subject, body):
        return True, "msg-1"

    monkeypatch.setattr(mailer, "_telnyx_post", fake_telnyx_post)

    track_calls = []

    def fake_track(settings, message_id, recipients, subject, body, *, attempt=1):
        track_calls.append((message_id, recipients, subject, body, attempt))

    monkeypatch.setattr(email_delivery, "track", fake_track)

    result = await mailer.send(settings, ["a@b.c"], "s", "b")
    assert result is True
    assert track_calls == [("msg-1", ["a@b.c"], "s", "b", 1)]

    track_calls.clear()
    result = await mailer.send(settings, ["a@b.c"], "s", "b", follow_up=False)
    assert result is True
    assert track_calls == []


async def test_operator_emails_reads_active_admins_from_the_database(ops, session):  # noqa: F811
    """The real query (not a fake): active admin operators only."""
    from tests.test_ops_console import _operator

    await _operator(ops, session, email="admin-ops@example.com", role="admin")
    await _operator(ops, session, email="reviewer-ops@example.com", role="reviewer")
    assert await email_delivery._operator_emails() == ["admin-ops@example.com"]
