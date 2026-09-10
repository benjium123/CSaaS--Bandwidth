"""Idempotent, zero-configuration defaults for a newly created org.

Seeds exactly the fixed set of artifacts a brand-new org needs so it is usable
immediately. There is deliberately no marker column: idempotency is based on the
existence of each individual artifact.

Deliberate non-actions, so future readers do not mistake them for omissions:

  * "recording off" - there is no org-level recording toggle in this schema; recording
    is per-call/per-flow and is off unless a flow asks for it. Nothing to seed.
  * "AI off" - AgentProfile.sms_enabled defaults to False and no profile is created at
    all, so the AI surface is off by construction. Nothing to seed.
  * Opt-out keywords are a code-level CTIA catalogue (app/compliance/keywords.py), not
    per-org rows. Nothing to seed.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallFlow, ComplianceSettings, MessageTemplate, RingGroupDef
from app.models.compliance import FEDERAL_WINDOW_END, FEDERAL_WINDOW_START
from app.models.routing import RoutingPolicy
from app.services import flows

_COMPLIANCE_SETTINGS = "compliance_settings"
_TEMPLATE_HELP = "template:help"
_TEMPLATE_STOP = "template:stop"
_RING_GROUP_EVERYONE = "ring_group:everyone"
_CALL_FLOW_DEFAULT = "call_flow:default"
_ROUTING_POLICY = "routing_policy"


async def seed_org_defaults(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    owner_user_id: uuid.UUID | None = None,
) -> dict:
    # Caller owns the transaction. This function never commits or rolls back.
    # Two simultaneous seeds could both pass an existence check; the unique constraints
    # below make the loser fail with IntegrityError rather than duplicate. Callers are
    # not a meaningful concurrent path, so no retry logic is added.
    created: list[str] = []
    existing: list[str] = []

    # (a) compliance_settings
    compliance = (
        await session.execute(sa.select(ComplianceSettings).limit(1))
    ).scalar_one_or_none()
    if compliance is None:
        session.add(
            ComplianceSettings(
                id=uuid.uuid4(),
                org_id=org_id,
                window_start=FEDERAL_WINDOW_START,
                window_end=FEDERAL_WINDOW_END,
                quiet_hours_enforced=True,
            )
        )
        created.append(_COMPLIANCE_SETTINGS)
    else:
        existing.append(_COMPLIANCE_SETTINGS)

    # (b) template:help
    help_template = (
        await session.execute(
            sa.select(MessageTemplate).where(MessageTemplate.name == "Help reply")
        )
    ).scalar_one_or_none()
    if help_template is None:
        session.add(
            MessageTemplate(
                id=uuid.uuid4(),
                org_id=org_id,
                name="Help reply",
                body=(
                    "{{org.name}}: for help, reply to this message or call us. "
                    "Msg & data rates may apply. Reply STOP to unsubscribe."
                ),
                media_asset_ids=[],
            )
        )
        created.append(_TEMPLATE_HELP)
    else:
        existing.append(_TEMPLATE_HELP)

    # (c) template:stop
    stop_template = (
        await session.execute(
            sa.select(MessageTemplate).where(MessageTemplate.name == "Opt-out confirmation")
        )
    ).scalar_one_or_none()
    if stop_template is None:
        session.add(
            MessageTemplate(
                id=uuid.uuid4(),
                org_id=org_id,
                name="Opt-out confirmation",
                body=(
                    "You are unsubscribed from {{org.name}} messages. "
                    "No more messages will be sent. Reply START to resubscribe."
                ),
                media_asset_ids=[],
            )
        )
        created.append(_TEMPLATE_STOP)
    else:
        existing.append(_TEMPLATE_STOP)

    # (d) ring_group:everyone - flush so the default flow below can reference its id.
    ring_group = (
        await session.execute(
            sa.select(RingGroupDef).where(RingGroupDef.name == "Everyone")
        )
    ).scalar_one_or_none()
    if ring_group is None:
        ring_group = RingGroupDef(
            id=uuid.uuid4(),
            org_id=org_id,
            name="Everyone",
            strategy="simultaneous",
            member_user_ids=[str(owner_user_id)] if owner_user_id is not None else [],
            ring_timeout_seconds=20,
        )
        session.add(ring_group)
        await session.flush()
        created.append(_RING_GROUP_EVERYONE)
    else:
        existing.append(_RING_GROUP_EVERYONE)

    # (e) call_flow:default - created active deliberately (there is nothing to review),
    # but NOT bound to any number. org_numbers.call_flow_id stays NULL, so inbound
    # behaviour is unchanged until an admin binds it.
    flow_exists = (
        await session.execute(
            sa.select(CallFlow.id).where(CallFlow.name == "Default").limit(1)
        )
    ).scalar_one_or_none()
    if flow_exists is None:
        definition = {
            "entry": "ring",
            "nodes": {
                "ring": {
                    "type": "ring_group",
                    "ring_group_id": str(ring_group.id),
                    "no_answer": "voicemail",
                },
                "voicemail": {
                    "type": "voicemail",
                    "greeting": "Sorry we missed you. Leave a message after the tone "
                    "and we'll call you back.",
                },
            },
        }
        await flows.validate_and_raise(session, org_id, definition)
        session.add(
            CallFlow(
                id=uuid.uuid4(),
                org_id=org_id,
                name="Default",
                version=1,
                status="active",
                definition=definition,
                created_by=owner_user_id,
            )
        )
        created.append(_CALL_FLOW_DEFAULT)
    else:
        existing.append(_CALL_FLOW_DEFAULT)

    # (f) routing_policy - new orgs get Smart routing on with cross-carrier failover
    # available. Existing orgs keep their current policy exactly as configured.
    policy_exists = (
        await session.execute(sa.select(RoutingPolicy).limit(1))
    ).scalar_one_or_none()
    if policy_exists is None:
        session.add(
            RoutingPolicy(
                id=uuid.uuid4(),
                org_id=org_id,
                preference=[],
                allow_intra_carrier_failover=True,
                allow_cross_carrier_failover=True,
                smart_routing=True,
            )
        )
        created.append(_ROUTING_POLICY)
    else:
        existing.append(_ROUTING_POLICY)

    return {"created": sorted(created), "existing": sorted(existing)}
