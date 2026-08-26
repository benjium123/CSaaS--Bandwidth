"""Carrier webhook ingestion.

No JWT and no X-Org-Id: the carrier's HTTP Basic credentials are the only gate. Everything
this endpoint does is DB work — no external I/O of any kind — which is what keeps the ack
far inside Bandwidth's timeout without needing a queue.
"""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.models import CallLeg, OrgNumber
from app.providers.bandwidth import webhooks as bw_webhooks
from app.providers.voice import Hangup, Pause, Speak, StartRecording, VoiceCommand
from app.services import calls as calls_svc
from app.services import messaging as svc
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import verify_webhook as livekit_verify_webhook

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
log = structlog.get_logger("webhooks")

CARRIER = "bandwidth"

#: The only automatic inbound-call behaviour this phase has: no configured IVR/routing
#: exists yet, so an inbound call gets told so and hung up. P6 replaces this ONE constant
#: with real per-org behaviour - nothing else in the voice webhook path needs to change.
DEFAULT_INBOUND_COMMANDS = [
    Speak(text="This number is not yet configured for inbound calls."),
    Hangup(),
]


class _VoiceRetrySignal(Exception):
    """F9b: internal-only signal that ONE event needs the carrier to redeliver later - it
    is deliberately NOT caught by the per-event exception shield (F4) in
    `_handle_voice_webhook`, so it always surfaces as the 500 that triggers a carrier
    retry, the voice mirror of messaging's Outcome.RETRY (see bandwidth_messaging above,
    webhooks.py:76-82). Nothing is persisted when this is raised, so the retry sees a clean
    slate rather than a stale dead-letter."""


@router.post("/bandwidth/messaging")
async def bandwidth_messaging(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings = request.app.state.settings
    raw = await request.body()

    if not bw_webhooks.verify(
        request.headers,
        settings.bandwidth_webhook_username,
        settings.bandwidth_webhook_password.get_secret_value(),
    ):
        # Bandwidth retrying a 401 for 24h is their problem. Accepting unauthenticated
        # events would be ours.
        response.status_code = 401
        return {"error": {"code": "unauthenticated", "message": "Invalid webhook credentials"}}

    body_text = raw.decode("utf-8", errors="replace")
    try:
        events = bw_webhooks.parse(raw)
    except ValueError as exc:
        # Retrying malformed input cannot fix it: record and answer 200 so the carrier stops.
        await svc.dead_letter(session, CARRIER, "malformed", body_text)
        log.warning("webhook_malformed", reason=str(exc))
        return {"status": "dead_lettered"}

    outcomes = []
    for event in events:
        outcomes.append(
            await svc.ingest_event(
                session, CARRIER, event, body_text, getattr(request.app.state, "carrier", None)
            )
        )

    if svc.Outcome.RETRY in outcomes:
        # Deliberate 500: at least one DLR referenced a message we have not committed yet.
        # Bandwidth's own 24h retry heals the race. Safe to replay the whole batch — every
        # already-done event dedupes to a no-op on the unique constraint.
        response.status_code = 500
        return {"status": "retry"}

    return {"status": "ok", "events": len(outcomes)}


@router.post("/{carrier_name}/messaging")
async def carrier_messaging(
    carrier_name: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Ingestion for every carrier other than Bandwidth.

    Each adapter verifies with **its own** scheme - Bandwidth Basic auth, Telnyx Ed25519,
    SignalWire's Twilio-style HMAC over the URL. There is deliberately no shared verifier:
    a common one would have to accept the weakest scheme for everybody, and "the signature
    passed" would stop meaning the same thing per carrier.

    Bandwidth keeps its own explicit route above; this handles the rest, so the path an
    operator registers with a carrier always names the carrier.
    """
    registry = getattr(request.app.state, "carriers", None)
    carrier = registry.get(carrier_name) if registry else None
    if carrier is None:
        # 404, not 401: the carrier genuinely is not configured here, and telling it to
        # retry for 24h against a route that will never exist helps nobody.
        response.status_code = 404
        return {"error": {"code": "carrier_not_configured", "message": carrier_name}}

    raw = await request.body()
    if not carrier.verify_webhook(request.headers, raw):
        response.status_code = 401
        return {"error": {"code": "unauthenticated", "message": "Invalid webhook signature"}}

    body_text = raw.decode("utf-8", errors="replace")
    try:
        events = carrier.parse_webhook(raw)
    except ValueError as exc:
        await svc.dead_letter(session, carrier_name, "malformed", body_text)
        log.warning("webhook_malformed", carrier=carrier_name, reason=str(exc))
        return {"status": "dead_lettered"}

    outcomes = []
    for event in events:
        outcomes.append(
            await svc.ingest_event(session, carrier_name, event, body_text, carrier)
        )

    if svc.Outcome.RETRY in outcomes:
        response.status_code = 500
        return {"status": "retry"}
    return {"status": "ok", "events": len(outcomes)}


# ==================================================================================
# P5 additions - voice
# ==================================================================================
async def _resolve_voice_org(session: AsyncSession, event) -> tuple[object, object]:  # noqa: ANN001
    """(org_id, leg) for one VoiceEvent, resolved WITHOUT tenant context (there is none
    yet - this lookup is what establishes it, mirroring _ingest_inbound's number lookup).

    Two paths, same as the calls-service docstring: an existing leg's own org, or (for a
    call this org has never seen) an OrgNumber match on the event's `to` OR `from` (F1a/F9a):
    a transfer B-leg and an outbound leg caught mid-race (the leg's own provider_call_id
    commit racing the webhook) both carry OUR number in `from`, not `to`.
    """
    leg = None
    if event.provider_call_id:
        leg = (
            await session.execute(
                sa.select(CallLeg)
                .where(CallLeg.provider_call_id == event.provider_call_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
    if leg is not None:
        return leg.org_id, leg

    for number in (event.to, event.from_):
        if not number:
            continue
        org_number = (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.e164 == number)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if org_number is not None:
            return org_number.org_id, None
    return None, None


async def _to_is_org_number(session: AsyncSession, org_id, to: str) -> bool:  # noqa: ANN001
    """F1c guard: create_inbound_call must only ever fire for a call_initiated whose `to`
    is genuinely one of THIS org's numbers - not merely because the org was reachable via
    the `from` fallback above (a transfer B-leg's/outbound race's `to` is the OUTSIDE
    party, never ours)."""
    if not to:
        return False
    return (
        await session.execute(
            sa.select(OrgNumber.id)
            .where(OrgNumber.org_id == org_id, OrgNumber.e164 == to)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none() is not None


def _outbound_answer_commands(call, *, needs_pause: bool) -> list[VoiceCommand]:  # noqa: ANN001
    """F3+F5: the ONE place that decides what happens when an OUTBOUND call answers. P6
    (rooms) / P7 (media) take over answer handling entirely and replace this single
    function - nothing else in the voice webhook path needs to change when they do.

    `needs_pause` is true for a carrier that delivers commands inline in the webhook
    response (Bandwidth): an empty `<Response/>` there hangs the line up immediately, so
    something must hold it open as a stopgap until P6/P7 exist. Telnyx delivers commands
    out-of-band via execute_commands and has no such response-body concept, so it never
    needs the Pause.
    """
    commands: list[VoiceCommand] = []
    if call.extra.get("record"):
        commands.append(StartRecording())
    if needs_pause:
        commands.append(Pause(seconds=3600))
    return commands


def _voice_bxml_response(carrier, commands: list) -> Response:  # noqa: ANN001
    rendered = carrier.render_commands(commands)
    if rendered is None:
        # Telnyx: commands go out-of-band via execute_commands, never in the response body.
        return JSONResponse(status_code=200, content={"status": "ok"})
    return Response(content=rendered, media_type="application/xml")


async def _handle_voice_webhook(
    carrier_name: str, request: Request, session: AsyncSession
) -> Response:
    registry = getattr(request.app.state, "carriers", None)
    carrier = registry.get(carrier_name) if registry else None
    if carrier is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "carrier_not_configured", "message": carrier_name}},
        )

    raw = await request.body()
    if not carrier.verify_voice_webhook(request.headers, raw):
        # Bandwidth retrying a 401 for 24h is their problem; accepting an unauthenticated
        # voice event would be ours.
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthenticated", "message": "Invalid webhook signature"}},
        )

    body_text = raw.decode("utf-8", errors="replace")
    try:
        events = carrier.parse_voice_webhook(raw)
    except ValueError as exc:
        log.warning("voice_webhook_malformed", carrier=carrier_name, reason=str(exc))
        events = []

    store = getattr(request.app.state, "media_store", None)
    commands: list = []
    retry_needed = False
    for event in events:
        try:
            org_id, leg = await _resolve_voice_org(session, event)
            if org_id is None:
                # F12 ruling: cannot even establish which org this belongs to - dead-letter
                # it, same as an inbound message to an unknown number. Dead-lettering COUNTS
                # as stored; a redelivery landing here again during the carrier's retry
                # window dead-lettering a second time is accepted, not treated as loss.
                await svc.dead_letter(session, carrier_name, "unknown_voice_call", body_text)
                continue

            set_org_context(session, org_id)

            if leg is None:
                # F1b: a transfer B-leg (or an outbound leg whose provider_call_id commit
                # lost the race with its own webhook) - adopt the pending leg BEFORE ever
                # considering this a brand new inbound call.
                leg = await calls_svc.adopt_transfer_leg(session, org_id, event)

            if leg is None and event.event_type == "call_initiated":
                if not event.provider_call_id:
                    # F13: refuse to create a Call keyed on nothing.
                    await svc.dead_letter(
                        session, carrier_name, "empty_provider_call_id", body_text
                    )
                    continue
                if await _to_is_org_number(session, org_id, event.to):
                    # F1c: create_inbound_call remains only for a GENUINELY new inbound
                    # call - `to` must be one of THIS org's own numbers, not merely
                    # reachable via the `from` fallback above.
                    await calls_svc.create_inbound_call(session, org_id, event, carrier_name)
                else:
                    await svc.dead_letter(
                        session, carrier_name, "unmatched_voice_initiate", body_text
                    )
                    continue
            elif leg is None:
                # F9b: org is known but no leg exists for a non-initiate event, and no
                # pending transfer leg matched either - signal a retry rather than ledger
                # an event that can never be attributed. See _VoiceRetrySignal above.
                raise _VoiceRetrySignal

            call, _leg, _changed = await calls_svc.apply_voice_event(
                session, carrier_name, event, org_id, carrier=carrier, store=store
            )

            if (
                event.event_type == "call_initiated"
                and call is not None
                and call.direction == "inbound"
            ):
                # Idempotent by design (D6): a duplicate delivery of the same call_initiated
                # gets the exact same BXML back, whether or not the row itself was new.
                commands = list(DEFAULT_INBOUND_COMMANDS)
            elif (
                event.event_type == "call_answered"
                and call is not None
                and call.direction == "outbound"
            ):
                # F3+F5: an outbound call answering is the one place a `record` flag turns
                # into a StartRecording, and (Bandwidth only) the one place the line must be
                # deliberately held open past the answer webhook.
                inline_commands = carrier.render_commands([]) is not None
                outbound_commands = _outbound_answer_commands(call, needs_pause=inline_commands)
                if inline_commands:
                    commands = outbound_commands
                elif outbound_commands:
                    await carrier.execute_commands(event.provider_call_id, outbound_commands)
        except _VoiceRetrySignal:
            retry_needed = True
            continue
        except Exception:
            # F4: one bad event must never 500 the whole ack - EXCEPT the deliberate RETRY
            # signal above, which this shield intentionally does not catch.
            log.exception(
                "voice_webhook_event_failed",
                carrier=carrier_name,
                event_type=getattr(event, "event_type", None),
            )
            continue

    if retry_needed:
        return Response(status_code=500, content=b"retry")
    return _voice_bxml_response(carrier, commands)


@router.post("/bandwidth/voice/answer")
async def bandwidth_voice_answer(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    return await _handle_voice_webhook("bandwidth", request, session)


@router.post("/bandwidth/voice/disconnect")
async def bandwidth_voice_disconnect(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    return await _handle_voice_webhook("bandwidth", request, session)


@router.post("/bandwidth/voice/amd")
async def bandwidth_voice_amd(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    return await _handle_voice_webhook("bandwidth", request, session)


@router.post("/{carrier_name}/voice")
async def carrier_voice(
    carrier_name: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Every carrier's voice ingestion other than Bandwidth's three named subpaths above -
    each verifies with its own scheme, same reasoning as carrier_messaging."""
    return await _handle_voice_webhook(carrier_name, request, session)


# ==================================================================================
# P6 addition - LiveKit (media plane, not a carrier: see voice_plane/service.py header)
# ==================================================================================
@router.post("/livekit")
async def livekit_webhook(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    settings = request.app.state.settings
    if getattr(request.app.state, "livekit", None) is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "carrier_not_configured", "message": "livekit"}},
        )

    raw = await request.body()
    event = livekit_verify_webhook(
        request.headers,
        raw,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
    )
    if event is None:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthenticated", "message": "Invalid webhook signature"}},
        )

    bus = request.app.state.event_bus
    try:
        await voice_service.handle_livekit_event(session, bus, event)
    except Exception:
        # F4's LiveKit sibling: one bad event must never fail the ack - LiveKit has no
        # documented retry contract to lean on here, so swallowing (not 500ing) is the
        # safer default rather than inviting an infinite redelivery loop.
        log.exception("livekit_webhook_event_failed", event_type=event.get("event"))

    return JSONResponse(status_code=200, content={"status": "ok"})
