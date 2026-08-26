"""Loopback carrier — DEV/DEMO ONLY.

A frontend phase with no carrier account could only ever demo a 503. This fake carrier
accepts sends and then drives **the real ingestion service** with constructed events:
delivery receipts, then an echo inbound reply. The state machine, idempotency ledger,
thread upsert, contact linkage, unread derivation and polling are all exercised for real —
only the PSTN is simulated.

Guardrails live in ``Settings``: boot REFUSES this when APP_ENV=production, and refuses it
alongside BANDWIDTH_ENABLED (ambiguous carrier).

Tests construct it with ``auto=False`` and call ``await drain()`` — deterministic, no sleeps.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

import structlog

from app.providers.domain import (
    CarrierCapabilities,
    CarrierEvent,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    SendResult,
)

log = structlog.get_logger("carrier.loopback")

CARRIER_NAME = "loopback"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LoopbackCarrier:
    name = CARRIER_NAME

    capabilities = CarrierCapabilities(
        supports_cancel=False,
        supports_scheduled_send=False,
        sync_delivery_status=False,
        max_media_bytes=3_750_000,
        group_mms_toll_free=False,
    )

    def __init__(
        self,
        *,
        auto: bool = True,
        echo: bool = True,
        dlr_delay: float = 0.4,
        reply_delay: float = 1.5,
    ) -> None:
        self.auto = auto
        self.echo = echo
        self.dlr_delay = dlr_delay
        self.reply_delay = reply_delay
        self._pending: list[tuple[str, OutboundMessage]] = []
        self._tasks: set[asyncio.Task] = set()

    async def send_message(self, msg: OutboundMessage) -> SendResult:
        provider_id = f"loopback-{uuid.uuid4()}"
        self._pending.append((provider_id, msg))
        if self.auto:
            task = asyncio.create_task(self._simulate(provider_id, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self._pending.pop()
        return SendResult("accepted", provider_id, None)

    # -- simulation ---------------------------------------------------------------
    def _events_for(self, provider_id: str, msg: OutboundMessage) -> list[CarrierEvent]:
        events: list[CarrierEvent] = [
            DeliveryReceipt(
                provider_message_id=provider_id,
                event_type="message-sending",
                segment_count=1,
                event_time=_now(),
                raw={"type": "message-sending", "simulated": True},
            ),
            DeliveryReceipt(
                provider_message_id=provider_id,
                event_type="message-delivered",
                segment_count=1,
                event_time=_now(),
                raw={"type": "message-delivered", "simulated": True},
            ),
        ]
        if self.echo:
            events.append(
                InboundMessage(
                    provider_message_id=f"loopback-in-{uuid.uuid4()}",
                    from_=msg.to,
                    to=msg.from_,
                    our_number=msg.from_,
                    text=f"echo: {msg.text}",
                    segment_count=1,
                    event_time=_now(),
                    raw={"type": "message-received", "simulated": True},
                )
            )
        return events

    async def _dispatch(self, events: list[CarrierEvent]) -> None:
        """Feed events through the REAL ingestion service on a fresh session."""
        from app.db.session import get_sessionmaker
        from app.services import messaging as svc

        for event in events:
            async with get_sessionmaker()() as session:
                await svc.ingest_event(session, CARRIER_NAME, event, "{}", self)

    async def _simulate(self, provider_id: str, msg: OutboundMessage) -> None:
        try:
            events = self._events_for(provider_id, msg)
            await asyncio.sleep(self.dlr_delay)
            await self._dispatch(events[:2])
            if len(events) > 2:
                await asyncio.sleep(self.reply_delay)
                await self._dispatch(events[2:])
        except Exception:  # never raise into the send path
            log.exception("loopback_simulation_failed", provider_message_id=provider_id)

    async def drain(self) -> None:
        """Deterministically run every queued simulation. For tests (auto=False)."""
        while self._pending:
            provider_id, msg = self._pending.pop(0)
            await self._dispatch(self._events_for(provider_id, msg))

    def media_auth(self, url: str) -> tuple[str, str] | None:
        return None

    # -- webhook surface is inert: nothing external ever posts to us ---------------
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        return False

    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]:
        return []
