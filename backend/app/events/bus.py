"""In-process, org-scoped pub/sub for realtime events (ring notifications, call status).

Single-process by design: every event published here fans out only to subscribers
connected to THIS worker. Redis pub/sub is the drop-in replacement with the SAME
subscribe/publish interface once workers scale past one process - keeping that interface
narrow (subscribe(org_id) -> queue, publish(org_id, event)) is the entire point of this
class existing as a seam rather than inlining asyncio.Queue everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog

logger = structlog.get_logger("events.bus")

#: Bounded so a stalled consumer cannot grow memory without limit. On overflow the OLDEST
#: queued event is dropped (never the newest) - a client catching up should see the
#: freshest state, not stale history it will immediately supersede.
QUEUE_MAXSIZE = 256


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue]] = {}

    @asynccontextmanager
    async def subscribe(self, org_id: uuid.UUID) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(org_id, set()).add(queue)
        try:
            yield queue
        finally:
            subs = self._subscribers.get(org_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(org_id, None)

    def publish(self, org_id: uuid.UUID, event: dict) -> None:
        """Sync and non-blocking on purpose: this is called from the webhook path, which
        must never wait on a slow (or dead) websocket consumer.
        """
        for queue in list(self._subscribers.get(org_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("event_bus_queue_overflow", org_id=str(org_id))
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
