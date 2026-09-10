"""Handing a live room call to an assistant.

This module is the ONLY place that puts an assistant worker into a room. It is reached
from exactly three places - an AI calling campaign's dial, the "Call me" test call, and an
inbound call on a number whose flow answers with an assistant - and it does nothing at all
for any other call, which is what keeps the human ring path untouched.
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import AgentProfile, Call, CallFlow, CallLeg, OrgNumber
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services import agent as agent_svc

log = structlog.get_logger("assistant_dispatch")

AI_AGENT_NAME_DEFAULT = "ai-agent"


def agent_name(settings) -> str:
    """`settings.ai_agent_name` when the deployment set one, else the default."""
    return getattr(settings, "ai_agent_name", AI_AGENT_NAME_DEFAULT)


async def dispatch_into_room(session: AsyncSession, api, settings, call: Call) -> bool:
    """Put the assistant worker in this call's room. Never raises - a dispatch failure
    must not fail the dial or the webhook that triggered it. Returns whether a dispatch
    was actually asked for."""
    if api is None:
        return False
    if ((call.extra or {}).get("via")) != "livekit":
        return False
    room = (call.extra or {}).get("room")
    if not room:
        return False
    if call.status in TERMINAL_CALL_STATUSES:
        return False

    marker = ((call.extra or {}).get("assistant") or {})
    if marker.get("dispatched") is True:
        return False

    token = agent_svc.mint_call_worker_token(
        settings, call_id=call.id, org_id=call.org_id
    )
    if not token:
        log.warning("assistant_dispatch_no_token", call_id=str(call.id))
        return False

    # The token travels in the dispatch metadata, never in our database and never in an
    # API response.
    metadata = json.dumps(
        {
            "call_id": str(call.id),
            "org_id": str(call.org_id),
            "worker_token": token,
        }
    )

    try:
        await api.create_agent_dispatch(
            room=room, agent_name=agent_name(settings), metadata=metadata
        )
        call.extra = {
            **(call.extra or {}),
            "assistant": {**marker, "dispatched": True},
        }
        await session.commit()
    except Exception:
        log.exception("assistant_dispatch_failed", call_id=str(call.id))
        return False

    return True


async def mark_for_assistant(
    session: AsyncSession, call: Call, *, profile_id, is_test: bool = False
) -> None:
    """Stamp the dispatch marker on a call row before the worker can ask who it is."""
    marker = ((call.extra or {}).get("assistant") or {})
    call.extra = {
        **(call.extra or {}),
        "assistant": {**marker, "profile_id": str(profile_id), "is_test": is_test},
    }


async def _flow_assistant_profile(session: AsyncSession, call: Call) -> AgentProfile | None:
    """Step 2 of resolve_call_profile only, used by the inbound hook to make sure a
    DEFAULT profile never starts answering every inbound call."""
    if call.direction != "inbound" or not call.our_e164:
        return None

    number = (
        await session.execute(
            sa.select(OrgNumber).where(
                OrgNumber.org_id == call.org_id,
                OrgNumber.e164 == call.our_e164,
            )
        )
    ).scalar_one_or_none()
    if number is None or number.call_flow_id is None:
        return None

    flow = await session.get(CallFlow, number.call_flow_id)
    if flow is None:
        return None

    from app.services import flows as flows_svc

    profile_ids = flows_svc.assistant_profile_ids(flow.definition or {})
    if not profile_ids:
        return None
    try:
        profile_id = uuid.UUID(str(profile_ids[0]))
    except (ValueError, TypeError):
        return None

    return (
        await session.execute(
            sa.select(AgentProfile).where(
                AgentProfile.org_id == call.org_id,
                AgentProfile.id == profile_id,
            )
        )
    ).scalar_one_or_none()


async def on_livekit_event(session: AsyncSession, api, settings, event: dict) -> None:
    """Inbound hook. After the LiveKit webhook has been applied, hand a brand-new inbound
    room call to an assistant when - and only when - the number it arrived on is answered
    by one. Never raises."""
    try:
        if event.get("event") != "participant_joined":
            return
        participant = event.get("participant") or {}
        attributes = participant.get("attributes") or {}
        sip_call_id = attributes.get("sip.callID")
        if not sip_call_id:
            return

        leg = (
            await session.execute(
                sa.select(CallLeg)
                .where(CallLeg.provider_call_id == sip_call_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if leg is None:
            return
        # Bind the tenant context from the LEG before loading the Call: session.get goes
        # through the tenant listener, and this hook runs on its own after the webhook's
        # own handler, so there is nothing to inherit a bound context from.
        set_org_context(session, leg.org_id)
        call = await session.get(Call, leg.call_id)
        if call is None:
            return

        if call.direction != "inbound":
            return
        if ((call.extra or {}).get("via")) != "livekit":
            return

        profile = await _flow_assistant_profile(session, call)
        if profile is None:
            return

        await mark_for_assistant(session, call, profile_id=profile.id)
        await dispatch_into_room(session, api, settings, call)
    except Exception:
        log.exception("assistant_dispatch_hook_failed", event_type=event.get("event"))
