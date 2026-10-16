"""SignalWire balance guard: entered balance minus SignalWire's own charges; one email at
$6, pause at $5.25 (calls, texts, inbound calls), a new balance lifts the pause."""

from datetime import datetime, timezone

import pytest

from app.providers.domain import OutboundMessage
from app.services import break_glass, mailer, signalwire_guard

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[list[str], str]] = []

    async def fake_send(settings, to, subject, body, *args, **kwargs):  # noqa: ANN001
        out.append((list(to), subject))

    async def fake_admins(session):  # noqa: ANN001
        return ["ops@example.com", "BENETHAN011@gmail.com"]

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(break_glass, "admin_emails", fake_admins)
    signalwire_guard._clear_cache()
    return out


async def _tick(session, settings, monkeypatch, spent):  # noqa: ANN001
    monkeypatch.setattr(signalwire_guard, "_last_run", None)

    async def fetch(_settings, _since):  # noqa: ANN001
        return spent

    return await signalwire_guard.tick(session, settings, fetch=fetch)


async def test_no_balance_entered_does_nothing(session, settings, sent, monkeypatch):
    assert await _tick(session, settings, monkeypatch, 5_000_000) == {}
    assert sent == []
    assert await signalwire_guard.is_paused(session) is False


async def test_warn_once_then_pause_once(session, settings, sent, monkeypatch):
    await signalwire_guard.set_balance(session, 11_990_130)
    await _tick(session, settings, monkeypatch, 5_000_000)  # $6.99 left
    assert sent == []

    await _tick(session, settings, monkeypatch, 5_990_130)  # exactly $6.00
    assert len(sent) == 1 and "about to run out" in sent[0][1]
    # Owner address first, admins added once (case-insensitive dedupe).
    assert sent[0][0] == ["benethan011@gmail.com", "ops@example.com"]
    await _tick(session, settings, monkeypatch, 6_200_000)  # $5.79: no second warning
    assert len(sent) == 1
    assert await signalwire_guard.is_paused(session) is False

    await _tick(session, settings, monkeypatch, 6_740_130)  # exactly $5.25
    assert len(sent) == 2 and "STOPPED" in sent[1][1]
    assert await signalwire_guard.is_paused(session) is True
    await _tick(session, settings, monkeypatch, 7_000_000)
    assert len(sent) == 2

    state = await signalwire_guard.get_state(session)
    assert state["estimate_micros"] == 11_990_130 - 7_000_000


async def test_failed_lookup_changes_nothing(session, settings, sent, monkeypatch):
    await signalwire_guard.set_balance(session, 5_500_000)
    await _tick(session, settings, monkeypatch, None)
    assert sent == [] and await signalwire_guard.is_paused(session) is False


async def test_new_balance_lifts_pause_and_rearms(session, settings, sent, monkeypatch):
    await signalwire_guard.set_balance(session, 6_000_000)
    await _tick(session, settings, monkeypatch, 1_000_000)
    assert await signalwire_guard.is_paused(session) is True

    # A balance still at/below the stop level keeps the pause.
    await signalwire_guard.set_balance(session, 5_000_000)
    assert await signalwire_guard.is_paused(session) is True

    state = await signalwire_guard.set_balance(session, 25_000_000)
    assert state["paused"] is False and state["warn_sent_at"] is None
    await _tick(session, settings, monkeypatch, 19_500_000)  # $5.50 -> warns again
    assert "about to run out" in sent[-1][1]


async def test_large_drop_pauses_without_separate_warning(session, settings, sent, monkeypatch):
    await signalwire_guard.set_balance(session, 10_000_000)
    await _tick(session, settings, monkeypatch, 9_000_000)
    assert len(sent) == 1 and "STOPPED" in sent[0][1]


def test_renewals_between():
    created = datetime(2026, 9, 19, 1, 30, tzinfo=timezone.utc)
    r = signalwire_guard.renewals_between
    assert r(created, T0, datetime(2026, 10, 18, tzinfo=timezone.utc)) == 0
    assert r(created, T0, datetime(2026, 10, 20, tzinfo=timezone.utc)) == 1
    assert r(created, T0, datetime(2026, 12, 20, tzinfo=timezone.utc)) == 3
    # Bought after the anchor: its purchase day is not a renewal.
    assert r(datetime(2026, 10, 17, tzinfo=timezone.utc), T0,
             datetime(2026, 11, 1, tzinfo=timezone.utc)) == 0
    # Bought on the 31st renews on short months' last day.
    jan31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert r(jan31, datetime(2026, 2, 1, tzinfo=timezone.utc),
             datetime(2026, 3, 1, tzinfo=timezone.utc)) == 1


async def test_spend_sums_voice_messaging_and_rentals(settings, monkeypatch):
    import httpx

    settings.signalwire_space_url = "example.signalwire.com"
    settings.signalwire_project_id = "proj"
    from pydantic import SecretStr

    settings.signalwire_api_token = SecretStr("tok")
    monkeypatch.setattr(signalwire_guard, "_now",
                        lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/voice/logs":
            if "page_token" in str(request.url):
                return httpx.Response(200, json={"data": [
                    {"created_at": "2026-10-16T13:00:00Z", "charge": 0.054}], "links": {}})
            return httpx.Response(200, json={"data": [
                {"created_at": "2026-10-16T12:30:00Z", "charge": 0.144},
                {"created_at": "2026-10-16T11:00:00Z", "charge": 9.0},  # before the anchor
            ], "links": {"next": "https://example.signalwire.com/api/voice/logs?page_token=x"}})
        if path == "/api/messaging/logs":
            return httpx.Response(200, json={"data": [
                {"created_at": "2026-10-16T12:10:00Z", "charge": 0.0, "charge_details": [
                    {"description": "Outbound SMS", "charge": 0.00415},
                    {"description": "SMS Outbound Carrier Fee", "charge": 0.003}]}],
                "links": {}})
        if path.endswith("/IncomingPhoneNumbers.json"):
            return httpx.Response(200, json={"incoming_phone_numbers": [
                {"date_created": "Sat, 19 Sep 2026 01:30:41 +0000"},
                {"date_created": "Wed, 16 Sep 2026 07:46:38 +0000"},  # renewed Oct 16 07:46, before
            ]})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    spent = await signalwire_guard.fetch_spend_micros(settings, T0)
    assert spent == 144_000 + 54_000 + 7_150 + 1_000_000


async def test_paused_signalwire_refuses_texts(session, monkeypatch):
    from app.providers.signalwire.adapter import SignalWireMessagingCarrier

    async def paused():
        return True

    monkeypatch.setattr(signalwire_guard, "paused_cached", paused)
    carrier = SignalWireMessagingCarrier.__new__(SignalWireMessagingCarrier)
    result = await carrier.send_message(OutboundMessage(to="+15550000001", from_="+15550000002",
                                                        text="hi", tag="t", media=()))
    assert result.status == "rejected" and result.error.carrier_code == "carrier_paused"
    assert result.error.retryable is False


async def test_paused_inbound_call_is_rejected_unanswered(client, monkeypatch):
    async def paused():
        return True

    monkeypatch.setattr(signalwire_guard, "paused_cached", paused)
    resp = await client.post(
        "/api/v1/webhooks/signalwire/voice",
        content="CallSid=CA1&Direction=inbound&CallStatus=ringing&From=%2B15550000001&To=%2B15550000002",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200 and "<Reject/>" in resp.text


async def test_unpaused_inbound_call_is_not_rejected(client, monkeypatch):
    async def not_paused():
        return False

    monkeypatch.setattr(signalwire_guard, "paused_cached", not_paused)
    resp = await client.post(
        "/api/v1/webhooks/signalwire/voice",
        content="CallSid=CA1&Direction=inbound&CallStatus=ringing&From=%2B15550000001&To=%2B15550000002",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert "<Reject/>" not in resp.text
