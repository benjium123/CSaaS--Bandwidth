"""Regression tests for BUGFIX_LEDGER_2026-09.md Area 2 (SMS/MMS pipeline), + 4.1
(registration gate) and the message.received frontend-support event."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.compliance import gate as compliance_gate
from app.compliance import keywords
from app.compliance import quiet_hours as qh
from app.compliance import service as compliance_svc
from app.db.base import set_org_context
from app.errors import ComplianceBlockedError, StickySenderUnavailableError
from app.models import (
    MediaAsset,
    Message,
    MessageEvent,
    MessageThread,
    Org,
    OrgNumber,
    PlatformEvent,
)
from app.providers.domain import DeliveryReceipt, InboundMessage, SendResult, UnknownEvent
from app.providers.plivo import webhooks as plivo_webhooks
from app.providers.registry import CarrierRegistry
from app.providers.twilio import webhooks as twilio_webhooks
from app.services import media as media_svc
from app.services import messaging as messaging_svc
from app.services.sender import select_sender
from tests.conftest import FakeCarrier

OUR = "+12145550100"
OUR_B = "+12145550101"
THEIRS = "+19725550101"
THEIRS_B = "+19725550102"


def _thread(org_id: uuid.UUID, our_e164: str, contact_e164: str) -> MessageThread:
    return MessageThread(
        id=uuid.uuid4(),
        org_id=org_id,
        our_e164=our_e164,
        contact_e164=contact_e164,
        last_message_at=datetime.now(timezone.utc),
    )


class PlivoParseCarrier(FakeCarrier):
    def parse_webhook(self, raw_body):
        return plivo_webhooks.parse(raw_body)


# ----------------------------------------------------------------------------------
# 2.1: Twilio SmsStatus=received is inbound, never a DLR
# ----------------------------------------------------------------------------------
async def test_twilio_received_is_inbound_not_dlr():
    body = (
        b"MessageSid=SM123&SmsStatus=received&Body=STOP"
        b"&From=%2B19725550101&To=%2B12145550100"
    )
    events = twilio_webhooks.parse(body)
    assert len(events) == 1
    assert isinstance(events[0], InboundMessage)
    assert events[0].text == "STOP"


# ----------------------------------------------------------------------------------
# 2.2: outbound MMS media is persisted at send time and survives a quiet-hours hold
# ----------------------------------------------------------------------------------
async def test_outbound_mms_media_persisted_and_used_on_release(monkeypatch, session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 MMS Org", slug="area2-mms-org"))
    await session.flush()
    fake = FakeCarrier(name="bandwidth")
    hold_until = datetime(2026, 6, 15, 17, 0)

    set_org_context(session, org_id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth",
            is_active=True, status="active",
        )
    )
    await session.commit()

    async def deferred_gate(*args, **kwargs):
        return compliance_gate.ComplianceVerdict(False, "quiet_hours", defer_until=hold_until)

    async def allowed_gate(*args, **kwargs):
        return compliance_gate.ComplianceVerdict(True)

    async def allowed_registration(*args, **kwargs):
        return True, ""

    monkeypatch.setattr(messaging_svc.gate, "check_outbound", deferred_gate)
    monkeypatch.setattr(messaging_svc.registration, "check_number_may_send", allowed_registration)

    message = await messaging_svc.send_message(
        session, org_id, fake,
        to_e164=THEIRS, from_e164=OUR, body="MMS hello",
        media_urls=["http://example.com/media/one"],
    )
    assert message.status == "queued"
    assert message.hold_until == hold_until
    # 2.2: the row itself carries the media URL NOW, before dispatch ever ran.
    assert message.media == ["http://example.com/media/one"]

    monkeypatch.setattr(messaging_svc.gate, "check_outbound", allowed_gate)
    released = await messaging_svc.release_held_messages(
        session, fake, now=hold_until + timedelta(minutes=1)
    )
    assert released == 1
    assert fake.sent[-1].media == ("http://example.com/media/one",)


# ----------------------------------------------------------------------------------
# 2.3: release_held_messages dispatches via message.carrier, not the primary
# ----------------------------------------------------------------------------------
async def test_release_held_messages_uses_message_carrier(monkeypatch, session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Release Org", slug="area2-release-org"))
    await session.flush()
    bandwidth = FakeCarrier(name="bandwidth")
    plivo = FakeCarrier(name="plivo")
    registry = CarrierRegistry({"bandwidth": bandwidth, "plivo": plivo}, primary="bandwidth")
    moment = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)

    set_org_context(session, org_id)
    thread_bw = _thread(org_id, OUR, THEIRS)
    thread_plivo = _thread(org_id, OUR_B, THEIRS_B)
    session.add_all([thread_bw, thread_plivo])
    await session.flush()
    session.add_all(
        [
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_bw.id, direction="outbound",
                status="queued", from_e164=OUR, to_e164=THEIRS, body="bw", media=[],
                carrier="bandwidth", hold_until=moment,
            ),
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_plivo.id, direction="outbound",
                status="queued", from_e164=OUR_B, to_e164=THEIRS_B, body="plivo", media=[],
                carrier="plivo", hold_until=moment,
            ),
        ]
    )
    await session.commit()

    async def allowed_gate(*args, **kwargs):
        return compliance_gate.ComplianceVerdict(True)

    monkeypatch.setattr(messaging_svc.gate, "check_outbound", allowed_gate)

    released = await messaging_svc.release_held_messages(
        session, bandwidth, now=moment + timedelta(minutes=1), registry=registry
    )
    assert released == 2
    assert {m.to for m in bandwidth.sent} == {THEIRS}
    assert {m.to for m in plivo.sent} == {THEIRS_B}


# ----------------------------------------------------------------------------------
# D4: release_held_messages must prime CURRENT_ORG_ID for the row's own org before
# consulting the registry - otherwise registry.get(message.carrier) on a
# CarrierRegistryProxy always resolves the GLOBAL (env) carrier, never a DB-only org's
# own provider account, even when both happen to share the same carrier name.
# ----------------------------------------------------------------------------------
async def test_d4_release_held_messages_resolves_the_orgs_db_backed_carrier(
    monkeypatch, session
):
    import time

    from app.providers import registry_org

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 D4 Org", slug="area2-d4-org"))
    await session.flush()

    env_bandwidth = FakeCarrier(name="bandwidth")
    org_bandwidth = FakeCarrier(name="bandwidth")
    global_registry = CarrierRegistry({"bandwidth": env_bandwidth}, primary="bandwidth")
    proxy = registry_org.CarrierRegistryProxy(global_registry)

    # Pre-seed this org's DB-backed registry cache directly (bypassing real credential
    # decryption/construction, which is not what's under test here) - is_primed() reads
    # exactly this entry, and CarrierRegistryProxy._resolve() only ever returns it when
    # CURRENT_ORG_ID names this org.
    version = registry_org.current_version(org_id)
    registry_org._ORG_REGISTRY_CACHE[(org_id, version)] = (
        CarrierRegistry({"bandwidth": org_bandwidth}, primary="bandwidth"),
        {"bandwidth": org_bandwidth},
        time.monotonic(),
    )
    try:
        moment = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        set_org_context(session, org_id)
        thread = _thread(org_id, OUR, THEIRS)
        session.add(thread)
        await session.flush()
        session.add(
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
                status="queued", from_e164=OUR, to_e164=THEIRS, body="hi", media=[],
                carrier="bandwidth", hold_until=moment,
            )
        )
        await session.commit()

        async def allowed_gate(*args, **kwargs):
            return compliance_gate.ComplianceVerdict(True)

        monkeypatch.setattr(messaging_svc.gate, "check_outbound", allowed_gate)

        released = await messaging_svc.release_held_messages(
            session, env_bandwidth, now=moment + timedelta(minutes=1), registry=proxy
        )
        assert released == 1
        assert len(org_bandwidth.sent) == 1
        assert env_bandwidth.sent == []
    finally:
        registry_org._ORG_REGISTRY_CACHE.pop((org_id, version), None)


# ----------------------------------------------------------------------------------
# 2.4: inverted quiet-hours window is rejected, never a perpetual hold
# ----------------------------------------------------------------------------------
async def test_inverted_quiet_hours_rejected():
    with pytest.raises(ValueError):
        qh.evaluate("+19725550101", window_start="21:00", window_end="08:00")


async def test_patch_settings_rejects_inverted_window(client):
    from tests.conftest import auth_headers, make_org_with_number

    token, org, _ = await make_org_with_number(client, "area2-window@example.com", "Org W", OUR)
    r = await client.patch(
        "/api/v1/compliance/settings",
        json={"window_start": "21:00", "window_end": "08:00"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422, r.text


# ----------------------------------------------------------------------------------
# 2.5: a failover win repoints the thread to the winning from_e164
# ----------------------------------------------------------------------------------
async def test_failover_repoints_thread_to_winning_number(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Failover Org", slug="area2-failover-org"))
    await session.flush()
    primary_from = OUR
    secondary_from = OUR_B
    error = SimpleNamespace(retryable=True, category="auth", carrier_code="1", detail="bad token")
    sc_primary = FakeCarrier(name="bandwidth", scripted=[SendResult("rejected", None, error)])
    sc_secondary = FakeCarrier(name="twilio")
    registry = CarrierRegistry({"bandwidth": sc_primary, "twilio": sc_secondary}, primary="bandwidth")

    class Route:
        def __init__(self, from_e164, carrier_name, reason):
            self.from_e164 = from_e164
            self.carrier_name = carrier_name
            self.reason = reason

    class Plan:
        def all_routes(self):
            return [
                Route(primary_from, "bandwidth", "primary"),
                Route(secondary_from, "twilio", "failover"),
            ]

    set_org_context(session, org_id)
    old_thread = _thread(org_id, primary_from, THEIRS)
    session.add(old_thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=old_thread.id, direction="outbound",
        status="queued", from_e164=primary_from, to_e164=THEIRS, body="failover", media=[],
        carrier="bandwidth",
    )
    session.add(message)
    await session.commit()

    updated = await messaging_svc.dispatch_with_failover(
        session, org_id, registry, Plan(), message, []
    )
    assert updated.status == "accepted"
    assert updated.from_e164 == secondary_from
    new_thread = await session.get(MessageThread, updated.thread_id)
    assert new_thread.our_e164 == secondary_from
    assert new_thread.id != old_thread.id


# ----------------------------------------------------------------------------------
# 2.6: sender considers only status=="active" numbers; refused sticky raises for replies
# ----------------------------------------------------------------------------------
async def test_select_sender_ignores_refused_number_and_raises_for_reply(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Sender Org", slug="area2-sender-org"))
    await session.flush()
    set_org_context(session, org_id)
    session.add_all(
        [
            OrgNumber(
                id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth",
                is_active=True, status="active",
            ),
            OrgNumber(
                id=uuid.uuid4(), org_id=org_id, e164=OUR_B, carrier="bandwidth",
                is_active=True, status="released",
            ),
        ]
    )
    await session.commit()

    sticky = _thread(org_id, OUR_B, THEIRS)
    session.add(sticky)
    await session.commit()

    with pytest.raises(StickySenderUnavailableError):
        await select_sender(session, org_id, THEIRS, allow_reassign=False)


# ----------------------------------------------------------------------------------
# 2.7: bulk sends skip the 24h active-conversation quiet-hours carve-out
# ----------------------------------------------------------------------------------
async def test_bulk_sends_do_not_use_active_conversation_carveout(monkeypatch, session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Bulk Org", slug="area2-bulk-org"))
    await session.flush()
    set_org_context(session, org_id)
    settings = await compliance_svc.get_settings(session, org_id)
    settings.quiet_hours_enforced = True
    settings.window_start = "08:00"
    settings.window_end = "21:00"
    late = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
    await session.commit()

    thread = _thread(org_id, OUR, THEIRS)
    session.add(thread)
    await session.flush()
    session.add(
        Message(
            id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
            status="received", from_e164=THEIRS, to_e164=OUR, body="hello", media=[],
            carrier="bandwidth", created_at=late.replace(tzinfo=None) - timedelta(hours=1),
        )
    )
    await session.commit()

    monkeypatch.setattr(qh, "_now", lambda: late)
    draft = compliance_gate.OutboundDraft(to_e164=THEIRS, from_e164=OUR, body="reply")

    regular = await compliance_gate.check_outbound(session, org_id, draft, bulk=False)
    assert regular.allowed is True
    assert regular.reason == "active_conversation"

    bulk = await compliance_gate.check_outbound(session, org_id, draft, bulk=True)
    assert bulk.allowed is False
    assert bulk.defer_until is not None


async def test_check_outbound_spy_seam_still_works_with_plain_three_args(session):
    """Guards the P1/P2 seam contract: a plain 3-positional-arg stand-in for
    check_outbound must keep working when bulk/exemption are both at their defaults -
    the new `bulk` kwarg must only be added to the call when it is actually truthy."""
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Seam Org", slug="area2-seam-org"))
    await session.flush()
    set_org_context(session, org_id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth",
            is_active=True, status="active",
        )
    )
    await session.commit()

    calls = []

    async def spy(session_, org_id_, draft):
        calls.append(draft)
        return compliance_gate.ComplianceVerdict(True)

    fake = FakeCarrier(name="bandwidth")
    import app.services.messaging as messaging_mod

    original_check_outbound = messaging_mod.gate.check_outbound
    messaging_mod.gate.check_outbound = spy
    try:
        await messaging_svc.send_message(
            session, org_id, fake, to_e164=THEIRS, from_e164=OUR, body="hi",
        )
    finally:
        messaging_mod.gate.check_outbound = original_check_outbound

    assert len(calls) == 1


# ----------------------------------------------------------------------------------
# 2.9: reprocess_pending re-parses via the OWNING carrier adapter
# ----------------------------------------------------------------------------------
async def test_reprocess_pending_uses_plivo_parser(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Plivo Org", slug="area2-plivo-org"))
    await session.flush()
    set_org_context(session, org_id)
    thread = _thread(org_id, OUR, THEIRS)
    session.add(thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
        status="accepted", from_e164=OUR, to_e164=THEIRS, body="plivo", media=[],
        carrier="plivo", provider_message_id="plivo-uuid",
    )
    session.add(message)
    await session.flush()
    row = MessageEvent(
        id=uuid.uuid4(), org_id=org_id, message_id=message.id, carrier="plivo",
        provider_message_id="plivo-uuid", event_type="message-delivered",
        payload={"MessageUUID": "plivo-uuid", "Status": "delivered", "ErrorCode": "900"},
    )
    session.add(row)
    await session.commit()

    registry = CarrierRegistry({"plivo": PlivoParseCarrier(name="plivo")}, primary="plivo")
    count = await messaging_svc.reprocess_pending(session, registry=registry)
    assert count == 1
    await session.refresh(message)
    await session.refresh(row)
    assert message.status == "delivered"
    assert row.processed_at is not None


async def test_reprocess_pending_without_registry_marks_missing_adapter(session):
    """No registry at all (e.g. no carrier configured) must not crash or silently drop
    the row - it stays pending with a diagnosable error, not misread as Bandwidth."""
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 NoReg Org", slug="area2-noreg-org"))
    await session.flush()
    set_org_context(session, org_id)
    thread = _thread(org_id, OUR, THEIRS)
    session.add(thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
        status="accepted", from_e164=OUR, to_e164=THEIRS, body="x", media=[],
        carrier="plivo", provider_message_id="plivo-uuid-2",
    )
    session.add(message)
    await session.flush()
    row = MessageEvent(
        id=uuid.uuid4(), org_id=org_id, message_id=message.id, carrier="plivo",
        provider_message_id="plivo-uuid-2", event_type="message-delivered",
        payload={"MessageUUID": "plivo-uuid-2", "Status": "delivered"},
    )
    session.add(row)
    await session.commit()

    count = await messaging_svc.reprocess_pending(session)
    assert count == 0
    await session.refresh(row)
    assert row.processed_at is None
    assert row.processing_error == "missing_carrier_adapter"


# ----------------------------------------------------------------------------------
# 2.10: Plivo unknown DLR statuses emit UnknownEvent; known DLRs carry ErrorCode
# ----------------------------------------------------------------------------------
async def test_plivo_unknown_status_emits_unknown_event_and_passes_error_code():
    events = plivo_webhooks.parse(b"MessageUUID=plivo-uuid&Status=weird&ErrorCode=950")
    assert len(events) == 1
    assert isinstance(events[0], UnknownEvent)
    assert events[0].raw["ErrorCode"] == "950"

    delivered = plivo_webhooks.parse(b"MessageUUID=plivo-uuid-2&Status=delivered&ErrorCode=900")
    assert isinstance(delivered[0], DeliveryReceipt)
    assert delivered[0].error_code == "900"


# ----------------------------------------------------------------------------------
# 2.11: recover_stale_queued resends once, fails on a second stale sighting
# ----------------------------------------------------------------------------------
async def test_recover_stale_queued_resends_once_then_fails(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Stale Org", slug="area2-stale-org"))
    await session.flush()
    fake = FakeCarrier(name="bandwidth")
    registry = CarrierRegistry({"bandwidth": fake}, primary="bandwidth")
    stale_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=11)

    set_org_context(session, org_id)
    thread_one = _thread(org_id, OUR, THEIRS)
    thread_two = _thread(org_id, OUR_B, THEIRS_B)
    session.add_all([thread_one, thread_two])
    await session.flush()
    first = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread_one.id, direction="outbound",
        status="queued", from_e164=OUR, to_e164=THEIRS, body="first", media=[],
        carrier="bandwidth", created_at=stale_time,
    )
    second = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread_two.id, direction="outbound",
        status="queued", from_e164=OUR_B, to_e164=THEIRS_B, body="second", media=[],
        carrier="bandwidth", created_at=stale_time, error_code="stale_recovery_attempted",
    )
    session.add_all([first, second])
    await session.commit()

    recovered = await messaging_svc.recover_stale_queued(
        session, registry=registry, now=datetime.now(timezone.utc)
    )
    assert recovered == 2
    await session.refresh(first)
    await session.refresh(second)
    assert first.status == "accepted"
    assert second.status == "failed"
    assert fake.sent[0].to == THEIRS


async def test_recover_stale_queued_ignores_fresh_queued_messages(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Fresh Org", slug="area2-fresh-org"))
    await session.flush()
    fake = FakeCarrier(name="bandwidth")
    registry = CarrierRegistry({"bandwidth": fake}, primary="bandwidth")

    set_org_context(session, org_id)
    thread = _thread(org_id, OUR, THEIRS)
    session.add(thread)
    await session.flush()
    fresh = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
        status="queued", from_e164=OUR, to_e164=THEIRS, body="fresh", media=[],
        carrier="bandwidth", created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(fresh)
    await session.commit()

    recovered = await messaging_svc.recover_stale_queued(
        session, registry=registry, now=datetime.now(timezone.utc)
    )
    assert recovered == 0
    await session.refresh(fresh)
    assert fresh.status == "queued"


# ----------------------------------------------------------------------------------
# 2.13: inbound MMS gets expires_at from settings.media_retention_days
# ----------------------------------------------------------------------------------
async def test_inbound_mms_media_expires_when_retention_configured(session):
    class FakeStore:
        async def put(self, key, data, content_type):
            self.key = key

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        async def aiter_bytes(self):
            yield b"abc"

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def stream(self, method, url, auth=None):
            return FakeStreamCtx()

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 MMS Expiry Org", slug="area2-mms-expiry-org"))
    await session.flush()
    set_org_context(session, org_id)
    asset = MediaAsset(
        id=uuid.uuid4(), org_id=org_id, direction="inbound",
        source_url="http://example.com/media", status="pending",
    )
    session.add(asset)
    await session.commit()
    moment = datetime.now(timezone.utc)

    settings = SimpleNamespace(media_retention_days=30)
    ok = await media_svc._fetch_one(
        session, FakeStore(), FakeClient(), None, asset, moment, settings
    )
    assert ok is True
    assert asset.status == "stored"
    assert asset.expires_at == moment + timedelta(days=30)


async def test_inbound_mms_media_never_expires_when_retention_is_zero(session):
    """settings.media_retention_days == 0 (the config default) means "never expire" -
    matching the existing convention services/media.py already uses for the outbound
    store_upload path. No settings passed at all behaves the same way."""
    class FakeStore:
        async def put(self, key, data, content_type):
            self.key = key

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        async def aiter_bytes(self):
            yield b"abc"

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def stream(self, method, url, auth=None):
            return FakeStreamCtx()

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 MMS NoExpiry Org", slug="area2-mms-noexpiry-org"))
    await session.flush()
    set_org_context(session, org_id)
    asset = MediaAsset(
        id=uuid.uuid4(), org_id=org_id, direction="inbound",
        source_url="http://example.com/media2", status="pending",
    )
    session.add(asset)
    await session.commit()
    moment = datetime.now(timezone.utc)

    ok = await media_svc._fetch_one(session, FakeStore(), FakeClient(), None, asset, moment)
    assert ok is True
    assert asset.status == "stored"
    assert asset.expires_at is None


# ----------------------------------------------------------------------------------
# 2.14: a bare "yes" only confirms a prior opt-out - never a universal opt-in
# ----------------------------------------------------------------------------------
async def test_bare_yes_is_not_opt_in_without_standing_opt_out():
    assert keywords.classify_keyword("Yes") is None
    start = keywords.classify_keyword("START")
    assert start is not None and start.kind == "opt_in"
    cancel = keywords.classify_keyword("cancel")
    assert cancel is not None and cancel.kind == "opt_out"


async def test_handle_inbound_keyword_yes_ignored_without_standing_optout(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Yes Org", slug="area2-yes-org"))
    await session.flush()
    set_org_context(session, org_id)
    thread = _thread(org_id, OUR, THEIRS)
    session.add(thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
        status="received", from_e164=THEIRS, to_e164=OUR, body="Yes", media=[],
        carrier="bandwidth",
    )
    session.add(message)
    await session.commit()

    await compliance_svc.handle_inbound_keyword(session, org_id, message)

    # No standing opt-out existed, so "Yes" must not have been recorded as an opt-in.
    assert await compliance_svc.is_opted_out(session, THEIRS) is False
    latest = await compliance_svc.latest_consent(session, THEIRS)
    assert latest is None


async def test_handle_inbound_keyword_yes_confirms_standing_optout(session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Yes Confirm Org", slug="area2-yes-confirm-org"))
    await session.flush()
    set_org_context(session, org_id)

    await compliance_svc.record_consent(
        session, org_id, contact_e164=THEIRS, event="opt_out", source="keyword",
        keyword_matched="stop",
    )
    await session.commit()
    assert await compliance_svc.is_opted_out(session, THEIRS) is True

    thread = _thread(org_id, OUR, THEIRS)
    session.add(thread)
    await session.flush()
    message = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound",
        status="received", from_e164=THEIRS, to_e164=OUR, body="yes", media=[],
        carrier="bandwidth",
    )
    session.add(message)
    await session.commit()

    await compliance_svc.handle_inbound_keyword(session, org_id, message)

    assert await compliance_svc.is_opted_out(session, THEIRS) is False
    latest = await compliance_svc.latest_consent(session, THEIRS)
    assert latest is not None and latest.event == "opt_in"


# ----------------------------------------------------------------------------------
# 4.1: send_message applies the registration gate whenever no plan is supplied
# ----------------------------------------------------------------------------------
async def test_send_message_registration_gate_when_no_plan(monkeypatch, session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Reg Org", slug="area2-reg-org"))
    await session.flush()
    fake = FakeCarrier(name="bandwidth")
    set_org_context(session, org_id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth",
            is_active=True, status="active",
        )
    )
    await session.commit()

    async def allowed_gate(*args, **kwargs):
        return compliance_gate.ComplianceVerdict(True)

    async def refused_registration(*args, **kwargs):
        return False, f"{OUR} is not registered"

    monkeypatch.setattr(messaging_svc.gate, "check_outbound", allowed_gate)
    monkeypatch.setattr(messaging_svc.registration, "check_number_may_send", refused_registration)

    with pytest.raises(ComplianceBlockedError):
        await messaging_svc.send_message(
            session, org_id, fake, to_e164=THEIRS, from_e164=OUR, body="blocked",
        )


async def test_send_message_skips_registration_gate_when_plan_supplied(monkeypatch, session):
    """When a plan IS supplied, routing.plan_route already filtered to
    registration-eligible numbers - send_message must not re-check (and must not 503 on
    a missing OrgNumber row lookup that a plan-based caller may not need)."""
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Plan Org", slug="area2-plan-org"))
    await session.flush()
    fake = FakeCarrier(name="bandwidth")
    set_org_context(session, org_id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth",
            is_active=True, status="active",
        )
    )
    await session.commit()

    registration_called = False

    async def should_not_be_called(*args, **kwargs):
        nonlocal registration_called
        registration_called = True
        return False, "should never be reached"

    async def allowed_gate(*args, **kwargs):
        return compliance_gate.ComplianceVerdict(True)

    monkeypatch.setattr(messaging_svc.gate, "check_outbound", allowed_gate)
    monkeypatch.setattr(messaging_svc.registration, "check_number_may_send", should_not_be_called)

    class Route:
        def __init__(self, from_e164, carrier_name):
            self.from_e164 = from_e164
            self.carrier_name = carrier_name
            self.reason = "primary"

    class Plan:
        def all_routes(self):
            return [Route(OUR, "bandwidth")]

    registry = CarrierRegistry({"bandwidth": fake}, primary="bandwidth")
    message = await messaging_svc.send_message(
        session, org_id, fake, to_e164=THEIRS, from_e164=OUR, body="via plan",
        registry=registry, plan=Plan(),
    )
    assert message.status == "accepted"
    assert registration_called is False


# ----------------------------------------------------------------------------------
# FRONTEND-SUPPORT: message.received on the real-time bus + durable outbox keys
# ----------------------------------------------------------------------------------
async def test_inbound_emits_received_event_with_frontend_keys(monkeypatch, session):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Area2 Frontend Org", slug="area2-frontend-org"))
    await session.flush()
    set_org_context(session, org_id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(), org_id=org_id, e164=OUR, carrier="bandwidth",
            is_active=True, status="active",
        )
    )
    await session.commit()

    async def noop_inbound(*args, **kwargs):
        return None

    async def org_cannot_reply(*args, **kwargs):
        return False

    monkeypatch.setattr(messaging_svc.gate, "on_inbound", noop_inbound)
    monkeypatch.setattr("app.services.sms_agent.org_could_reply", org_cannot_reply)

    published: list[tuple] = []

    class FakeBus:
        def publish(self, org_id_, event):
            published.append((org_id_, event))

    session.info["event_bus"] = FakeBus()

    event = InboundMessage(
        provider_message_id="in-1", from_=THEIRS, to=OUR, our_number=OUR, text="hi",
        media=(), segment_count=None, raw={},
    )
    outcome = await messaging_svc.ingest_event(
        session, "bandwidth", event, b"raw", carrier=FakeCarrier(name="bandwidth")
    )
    assert outcome == messaging_svc.Outcome.DONE

    # Durable outbox row (external webhook subscribers) carries the new keys too.
    row = (
        await session.execute(
            sa.select(PlatformEvent).where(PlatformEvent.event_type == "message.received")
        )
    ).scalar_one()
    assert row.payload["our_e164"] == OUR
    assert row.payload["contact_e164"] == THEIRS

    # Real-time org bus (routes/softphone.py's console WS) got the SAME shape.
    assert len(published) == 1
    pub_org_id, pub_event = published[0]
    assert pub_org_id == org_id
    assert pub_event["type"] == "message.received"
    assert pub_event["our_e164"] == OUR
    assert pub_event["contact_e164"] == THEIRS
    assert "thread_id" in pub_event and "message_id" in pub_event


async def test_message_received_is_gated_as_a_thread_id_event_in_softphone():
    from app.api.routes.softphone import _THREAD_ID_EVENTS

    assert "message.received" in _THREAD_ID_EVENTS
