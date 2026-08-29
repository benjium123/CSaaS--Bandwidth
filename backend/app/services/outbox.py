"""Durable platform-event outbox (P13 DR-4).

`record_platform_event` writes the row into the CALLER'S session, so the event commits
or rolls back with the domain change it describes. The webhook deliverer (sweeper tick,
P13 implementer) fans committed events out to subscribed endpoints — this module knows
nothing about delivery.

The in-process EventBus stays what it is: UI freshness. It drops on overflow and dies
with the process; customer webhooks must not.
"""

from __future__ import annotations

import uuid

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import PLATFORM_EVENT_TYPES, PlatformEvent
from app.models.voice import TERMINAL_CALL_STATUSES, Call


def record_platform_event(
    session: AsyncSession, org_id: uuid.UUID, event_type: str, payload: dict
) -> PlatformEvent:
    """Append one outbox row in the caller's transaction. Payload must be JSON-safe."""
    if event_type not in PLATFORM_EVENT_TYPES:
        raise ValueError(f"unknown platform event type '{event_type}'")
    row = PlatformEvent(id=uuid.uuid4(), org_id=org_id, event_type=event_type, payload=payload)
    session.add(row)
    return row


@event.listens_for(Session, "before_flush")
def _record_call_completed(session: Session, flush_context, instances) -> None:  # noqa: ANN001
    """Emit `call.completed` exactly once, at the flush where a Call turns terminal.

    A single hook here covers every transition path (carrier webhook state machine,
    LiveKit room events, dial-outcome application) instead of six call sites. The
    monotonic guard in services/calls.py makes the non-terminal -> terminal edge fire
    at most once per call, so no dedupe bookkeeping is needed.
    """
    for obj in session.dirty:
        if not isinstance(obj, Call) or not session.is_modified(obj):
            continue
        history = inspect(obj).attrs.status.history
        if not history.has_changes():
            continue
        old = history.deleted[0] if history.deleted else None
        if obj.status in TERMINAL_CALL_STATUSES and old not in TERMINAL_CALL_STATUSES:
            session.add(
                PlatformEvent(
                    id=uuid.uuid4(),
                    org_id=obj.org_id,
                    event_type="call.completed",
                    payload={
                        "call_id": str(obj.id),
                        "status": obj.status,
                        "direction": obj.direction,
                        "from": obj.our_e164 if obj.direction == "outbound" else obj.contact_e164,
                        "to": obj.contact_e164 if obj.direction == "outbound" else obj.our_e164,
                        "duration_seconds": obj.duration_seconds,
                    },
                )
            )
