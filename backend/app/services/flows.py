"""P12 flow CRUD: the DR-4 validation gate, DR-3 versioning/pinning, number binding, and
the DR-10 business-hours evaluation helper the carrier/room executors call to answer the
engine's ``EvaluateHours`` action.

Versioning (DR-3): a ``CallFlow`` row's ``definition`` is immutable once created - editing
always inserts a NEW row with ``version = max(existing) + 1`` and ``status="draft"``.
Activating a version flips it to ``status="active"`` and archives whatever OTHER version of
the same name was previously active, so at most one version per (org, name) is ever "active"
at a time. A call pins whichever row id it started with in ``calls.extra["flow"]``
(routing_exec.py) - editing or even activating a different version afterwards can never
change a call already in flight.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.callflow import BusinessHours, CallFlow, CallQueue, RingGroupDef
from app.models.messaging import OrgNumber
from app.services import flow_engine

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --------------------------------------------------------------------------------------
# Validation gate (DR-4)
# --------------------------------------------------------------------------------------
async def validate_and_raise(session: AsyncSession, org_id: uuid.UUID, definition: dict) -> None:
    """Structural (flow_engine.validate_flow: no DB access, cannot know whether an id
    resolves) THEN cross-object (B3: does ring_group_id / queue_id / business_hours_id
    actually name a row in THIS org?) - both stages name the offending node id exactly,
    and both must pass before a definition is ever saved. Runtime must never discover a
    dangling cross-object reference the way it can a genuinely malformed node."""
    errors = flow_engine.validate_flow(definition)
    if not errors:
        errors = await _validate_cross_refs(session, org_id, definition)
    if errors:
        raise ValidationFailedError("Invalid flow definition: " + "; ".join(errors))


async def _validate_cross_refs(
    session: AsyncSession, org_id: uuid.UUID, definition: dict
) -> list[str]:
    nodes = definition.get("nodes")
    if not isinstance(nodes, dict):
        return []

    errors: list[str] = []
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")

        if ntype == "ring_group":
            ref = node.get("ring_group_id")
            if isinstance(ref, str) and not await _exists(session, RingGroupDef, org_id, ref):
                errors.append(f"node '{node_id}' references unknown ring_group '{ref}'")
        elif ntype == "queue":
            ref = node.get("queue_id")
            if isinstance(ref, str) and not await _exists(session, CallQueue, org_id, ref):
                errors.append(f"node '{node_id}' references unknown queue '{ref}'")
        elif ntype == "hours":
            ref = node.get("business_hours_id")
            if isinstance(ref, str) and not await _exists(session, BusinessHours, org_id, ref):
                errors.append(f"node '{node_id}' references unknown business_hours '{ref}'")
        # "transfer" is deliberately NOT checked here: its `to` field is a phone number,
        # not an object reference, and flow_engine.validate_flow already validates its
        # shape (item 11 ruling).

    return errors


async def _exists(session: AsyncSession, model, org_id: uuid.UUID, raw_id: str) -> bool:  # noqa: ANN001
    try:
        obj_id = uuid.UUID(raw_id)
    except ValueError:
        return False
    stmt = sa.select(model.id).where(model.id == obj_id, model.org_id == org_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# --------------------------------------------------------------------------------------
# Flow CRUD + versioning
# --------------------------------------------------------------------------------------
async def list_flows(session: AsyncSession) -> list[CallFlow]:
    stmt = sa.select(CallFlow).order_by(CallFlow.name, CallFlow.version.desc())
    return list((await session.execute(stmt)).scalars().all())


async def list_versions(session: AsyncSession, name: str) -> list[CallFlow]:
    stmt = sa.select(CallFlow).where(CallFlow.name == name).order_by(CallFlow.version.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_flow(session: AsyncSession, flow_id: uuid.UUID) -> CallFlow:
    row = await session.get(CallFlow, flow_id)
    if row is None:
        raise NotFoundError("Flow not found")
    return row


async def create_flow(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    definition: dict,
    created_by: uuid.UUID | None = None,
) -> CallFlow:
    await validate_and_raise(session, org_id, definition)
    row = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name.strip(),
        version=1,
        status="draft",
        definition=definition,
        created_by=created_by,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Flow {name!r} version 1 already exists") from exc
    return row


async def create_version(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    flow_id: uuid.UUID,
    definition: dict,
    created_by: uuid.UUID | None = None,
) -> CallFlow:
    """DR-3: editing = a new immutable row. `flow_id` may name any existing version of the
    flow - the new row always lands one past the HIGHEST version that name has ever had."""
    existing = await session.get(CallFlow, flow_id)
    if existing is None:
        raise NotFoundError("Flow not found")
    await validate_and_raise(session, org_id, definition)

    max_version = (
        await session.execute(
            sa.select(sa.func.max(CallFlow.version)).where(CallFlow.name == existing.name)
        )
    ).scalar_one()
    row = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name=existing.name,
        version=int(max_version or 0) + 1,
        status="draft",
        definition=definition,
        created_by=created_by,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A version race occurred - retry") from exc
    return row


async def activate_flow(session: AsyncSession, org_id: uuid.UUID, flow_id: uuid.UUID) -> CallFlow:
    target = await session.get(CallFlow, flow_id)
    if target is None:
        raise NotFoundError("Flow not found")

    others = (
        (
            await session.execute(
                sa.select(CallFlow).where(
                    CallFlow.name == target.name,
                    CallFlow.status == "active",
                    CallFlow.id != target.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        other.status = "archived"

    target.status = "active"
    await session.commit()
    return target


async def bind_number(
    session: AsyncSession, org_id: uuid.UUID, number_id: uuid.UUID, flow_id: uuid.UUID | None
) -> OrgNumber:
    """DR-3: binding is `org_numbers.call_flow_id`, nullable (NULL keeps today's default
    behaviour). Only an ACTIVE version may be bound - a call pins whatever version is bound
    at ring time, so binding a draft/archived row would pin something never meant to run."""
    number = await session.get(OrgNumber, number_id)
    if number is None:
        raise NotFoundError("Number not found")

    if flow_id is not None:
        flow = await session.get(CallFlow, flow_id)
        if flow is None:
            raise NotFoundError("Flow not found")
        if flow.status != "active":
            raise ValidationFailedError("Only an active flow version can be bound to a number")

    number.call_flow_id = flow_id
    await session.commit()
    return number


# --------------------------------------------------------------------------------------
# Ring groups / queues (thin CRUD - runtime state lives in services/routing_exec.py)
# --------------------------------------------------------------------------------------
async def list_ring_groups(session: AsyncSession) -> list[RingGroupDef]:
    stmt = sa.select(RingGroupDef).order_by(RingGroupDef.name)
    return list((await session.execute(stmt)).scalars().all())


async def create_ring_group(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    strategy: str,
    member_user_ids: list[str],
    ring_timeout_seconds: int,
) -> RingGroupDef:
    row = RingGroupDef(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name.strip(),
        strategy=strategy,
        member_user_ids=member_user_ids,
        ring_timeout_seconds=ring_timeout_seconds,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Ring group {name!r} already exists") from exc
    return row


async def list_queues(session: AsyncSession) -> list[CallQueue]:
    stmt = sa.select(CallQueue).order_by(CallQueue.name)
    return list((await session.execute(stmt)).scalars().all())


async def create_queue(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    hold_audio_url: str | None,
    max_wait_seconds: int,
    overflow: str,
    ring_group_id: uuid.UUID | None,
) -> CallQueue:
    if ring_group_id is not None and await session.get(RingGroupDef, ring_group_id) is None:
        raise NotFoundError("Ring group not found")
    row = CallQueue(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name.strip(),
        hold_audio_url=hold_audio_url,
        max_wait_seconds=max_wait_seconds,
        overflow=overflow,
        ring_group_id=ring_group_id,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Queue {name!r} already exists") from exc
    return row


# --------------------------------------------------------------------------------------
# Business hours (DR-10)
# --------------------------------------------------------------------------------------
async def list_business_hours(session: AsyncSession) -> list[BusinessHours]:
    stmt = sa.select(BusinessHours).order_by(BusinessHours.name)
    return list((await session.execute(stmt)).scalars().all())


async def create_business_hours(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    timezone_name: str,
    schedule: dict,
    holidays: list[str],
) -> BusinessHours:
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValidationFailedError(f"Unknown IANA timezone {timezone_name!r}") from exc
    row = BusinessHours(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name.strip() or "default",
        timezone=timezone_name,
        schedule=schedule,
        holidays=list(holidays),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Business hours {name!r} already exists") from exc
    return row


def _parse_hhmm(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        hh, _, mm = value.partition(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def evaluate_hours(business_hours: BusinessHours, moment: datetime) -> str:
    """DR-10: "open" | "closed" | "holiday", evaluated in the business's OWN IANA
    timezone (never the caller's - this is the org's opening hours, not recipient-local
    quiet hours). Uses zoneinfo at the given instant, exactly like compliance/quiet_hours,
    so DST transitions come from the tz database rather than manual offset arithmetic.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(business_hours.timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        tz = ZoneInfo("UTC")

    local = moment.astimezone(tz)
    if local.date().isoformat() in (business_hours.holidays or []):
        return "holiday"

    weekday_key = _WEEKDAY_KEYS[local.weekday()]
    windows = (business_hours.schedule or {}).get(weekday_key) or []
    local_time = local.time()
    for window in windows:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            continue
        start = _parse_hhmm(window[0])
        end = _parse_hhmm(window[1])
        if start is None or end is None:
            continue
        if start <= local_time < end:
            return "open"
    return "closed"
