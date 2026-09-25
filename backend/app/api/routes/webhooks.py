"""Carrier webhook ingestion.

No JWT and no X-Org-Id: the carrier's HTTP Basic credentials are the only gate. Everything
this endpoint does is DB work — no external I/O of any kind — which is what keeps the ack
far inside Bandwidth's timeout without needing a queue.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated
from urllib.parse import parse_qsl

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.errors import UnauthenticatedError
from app.models import CallLeg, Org, OrgNumber
from app.models.provider_accounts import PROVIDER_NAMES, ProviderAccount
from app.providers import registry_org
from app.providers.bandwidth import webhooks as bw_webhooks
from app.providers.telnyx.voice import TelnyxVoiceCommandError
from app.providers.voice import Hangup, Pause, Speak, StartRecording, VoiceCommand
from app.services import assistant_dispatch, credits, didit_client, stripe_client
from app.services import calling_settings as calling_settings_svc
from app.services import calls as calls_svc
from app.services import credentials as credential_svc
from app.services import messaging as svc
from app.services import routing_exec as routing_exec_svc
from app.services import subscriptions as subscriptions_svc
from app.services import telephony_billing as _tb
from app.services import supervisor as supervisor_svc
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import verify_webhook as livekit_verify_webhook

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
log = structlog.get_logger("webhooks")

CARRIER = "bandwidth"

#: P17: verification fallback for a provider now configured only through an org's
#: provider_accounts row (no env credentials for it at all). Short-TTL, bounded: this
#: runs ONLY after the global env-registry path has already failed, so it is off the hot
#: path for every deployment that still authenticates purely off the environment.
_WEBHOOK_ACCOUNTS_CACHE: dict[str, tuple[float, list[ProviderAccount]]] = {}
_WEBHOOK_ACCOUNTS_TTL_SECONDS = 30.0
_WEBHOOK_ACCOUNTS_PAGE_SIZE = 50
#: 4.17: total across all pages, not a single page - a deployment with more than this
#: many active accounts for one provider needs a real per-account webhook path (P17
#: scale problem), not an unbounded scan every 30s cache miss.
_WEBHOOK_ACCOUNTS_TOTAL_CAP = 500


async def _active_accounts_for_provider(
    session: AsyncSession, provider: str
) -> list[ProviderAccount]:
    now = time.monotonic()
    cached = _WEBHOOK_ACCOUNTS_CACHE.get(provider)
    if cached is not None and now - cached[0] < _WEBHOOK_ACCOUNTS_TTL_SECONDS:
        return cached[1]

    # JUSTIFIED allow_unscoped: this runs before any org is known - verifying against
    # every org's account for this provider IS how a carrier-signed request that fails
    # env verification gets attributed to an org at all.
    #
    # 4.17: a bare LIMIT with no ORDER BY has no guaranteed, stable row selection across
    # calls/dialects - it previously could silently and permanently exclude some
    # account past the first (arbitrary) page from ever being able to verify a webhook.
    # Paginate through everything (bounded by _WEBHOOK_ACCOUNTS_TOTAL_CAP) ordered by id.
    rows: list[ProviderAccount] = []
    last_id = None
    while len(rows) < _WEBHOOK_ACCOUNTS_TOTAL_CAP:
        stmt = (
            sa.select(ProviderAccount)
            .where(
                ProviderAccount.provider == provider,
                ProviderAccount.status == "active",
            )
            .order_by(ProviderAccount.id)
            .limit(_WEBHOOK_ACCOUNTS_PAGE_SIZE)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
        if last_id is not None:
            stmt = stmt.where(ProviderAccount.id > last_id)
        page = list((await session.execute(stmt)).scalars().all())
        if not page:
            break
        rows.extend(page)
        last_id = page[-1].id
        if len(page) < _WEBHOOK_ACCOUNTS_PAGE_SIZE:
            break
    _WEBHOOK_ACCOUNTS_CACHE[provider] = (now, rows)
    return rows


async def _db_account_carrier_verifying_with_account(
    session: AsyncSession, settings, provider: str, headers, raw: bytes
):  # noqa: ANN001
    """Return (carrier, ProviderAccount) whose credentials verified this request, or None.

    The account is returned so voice ingestion can PIN the webhook to that account's org
    (item 1.3) instead of letting the payload resolve to a different org."""
    if provider not in PROVIDER_NAMES:  # attacker-controlled path segment: never cache/query
        return None
    if not credential_svc.master_key_present(settings):
        return None
    for account in await _active_accounts_for_provider(session, provider):
        candidate = await registry_org.carrier_for_account(settings, account)
        if candidate is not None and candidate.verify_webhook(headers, raw):
            return candidate, account
    return None


async def _db_account_carrier_verifying(
    session: AsyncSession, settings, provider: str, headers, raw: bytes
):  # noqa: ANN001
    """Compatibility wrapper for messaging routes that only need the carrier object.
    Never raises: a 503 from a missing master key just means no DB fallback exists,
    not that the webhook itself failed."""
    result = await _db_account_carrier_verifying_with_account(
        session, settings, provider, headers, raw
    )
    return result[0] if result is not None else None


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

    env_user = settings.bandwidth_webhook_username
    env_pass = settings.bandwidth_webhook_password.get_secret_value()
    # Empty configured creds must NEVER verify. bw_webhooks.verify() does a
    # constant-time compare of the request's Basic-auth user/pass against these -
    # unguarded, an unconfigured deployment (both blank) would accept a bare
    # `Authorization: Basic <base64(":")>` request as authentic, since "" == "" too.
    verified = bool(env_user and env_pass) and bw_webhooks.verify(
        request.headers, env_user, env_pass
    )
    db_carrier = None
    if not verified:
        db_carrier = await _db_account_carrier_verifying(
            session, settings, CARRIER, request.headers, raw
        )
        verified = db_carrier is not None

    if not verified:
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

    session.info["event_bus"] = getattr(request.app.state, "event_bus", None)
    session.info["settings"] = getattr(request.app.state, "settings", None)
    outcomes = []
    for event in events:
        outcomes.append(
            await svc.ingest_event(
                session,
                CARRIER,
                event,
                body_text,
                db_carrier or getattr(request.app.state, "carrier", None),
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
    settings = request.app.state.settings
    registry = getattr(request.app.state, "carriers", None)
    carrier = registry.get(carrier_name) if registry else None

    raw = await request.body()
    verified = carrier is not None and carrier.verify_webhook(request.headers, raw)
    if not verified:
        db_carrier = await _db_account_carrier_verifying(
            session, settings, carrier_name, request.headers, raw
        )
        if db_carrier is not None:
            carrier = db_carrier
            verified = True

    if carrier is None:
        # 404, not 401: the carrier genuinely is not configured here, and telling it to
        # retry for 24h against a route that will never exist helps nobody.
        response.status_code = 404
        return {"error": {"code": "carrier_not_configured", "message": carrier_name}}

    if not verified:
        response.status_code = 401
        return {"error": {"code": "unauthenticated", "message": "Invalid webhook signature"}}

    body_text = raw.decode("utf-8", errors="replace")
    try:
        events = carrier.parse_webhook(raw)
    except ValueError as exc:
        await svc.dead_letter(session, carrier_name, "malformed", body_text)
        log.warning("webhook_malformed", carrier=carrier_name, reason=str(exc))
        return {"status": "dead_lettered"}

    session.info["event_bus"] = getattr(request.app.state, "event_bus", None)
    session.info["settings"] = getattr(request.app.state, "settings", None)
    outcomes = []
    for event in events:
        outcomes.append(await svc.ingest_event(session, carrier_name, event, body_text, carrier))

    if svc.Outcome.RETRY in outcomes:
        response.status_code = 500
        return {"status": "retry"}
    if carrier_name == "signalwire":
        # SignalWire's Compatibility (LaML) webhooks expect an XML document back and log
        # anything else as "12100 Document parse error" against the message - measured
        # 2026-09-19: every ingested inbound text showed as "failed" on their side. An
        # empty <Response/> is "received, nothing to reply".
        return Response(content="<Response/>", media_type="application/xml")
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


def _outbound_answer_commands(call, org, *, needs_pause: bool) -> list[VoiceCommand]:  # noqa: ANN001
    """F3+F5: the ONE place that decides what happens when an OUTBOUND call answers. P6
    (rooms) / P7 (media) take over answer handling entirely and replace this single
    function - nothing else in the voice webhook path needs to change when they do.

    P29: when the org has the recording consent announcement enabled, the answering
    party hears it before any other answer command - including StartRecording - so the
    announcement precedes the recording rather than playing inside it. The inbound half
    lives in `services/routing_exec.py` (the flow engine's connect path).

    `needs_pause` is true for a carrier that delivers commands inline in the webhook
    response (Bandwidth): an empty `<Response/>` there hangs the line up immediately, so
    something must hold it open as a stopgap until P6/P7 exist. Telnyx delivers commands
    out-of-band via execute_commands and has no such response-body concept, so it never
    needs the Pause.
    """
    commands: list[VoiceCommand] = []
    # P43: a call recorded for safety monitoring ALWAYS tells the other side first, even
    # when the business turned its own announcement off (two-party consent states).
    if org is not None and (
        calling_settings_svc.announcement_enabled(org) or (call.extra or {}).get("monitor")
    ):
        commands.append(Speak(text=calling_settings_svc.announcement_text_for(org)))
    if call.extra.get("record"):
        commands.append(StartRecording())
    if needs_pause:
        commands.append(Pause(seconds=3600))
    return commands


async def _voice_bxml_response(
    carrier,  # noqa: ANN001
    commands: list,
    provider_call_id: str | None = None,
    *,
    session: AsyncSession | None = None,
    carrier_name: str = "",
    body_text: str = "",
) -> Response:
    rendered = carrier.render_commands(commands)
    if rendered is None:
        # Telnyx: commands go out-of-band via execute_commands, never in the response body.
        # 3.2: previously this branch never ran the commands at all when render_commands
        # returned None, so an inbound Telnyx call flow/default was pure silence.
        if commands and provider_call_id:
            await carrier.execute_commands(provider_call_id, commands)
        elif commands:
            # B5: no provider_call_id was ever captured for this call, so the commands
            # can never be delivered out-of-band - silently answering 200 here was a
            # fake success with no audio ever played. Dead-letter and signal failure
            # (consistent with 3.15's TelnyxVoiceCommandError -> 502 handling) instead.
            if session is not None:
                await svc.dead_letter(session, carrier_name, "commands_without_call_id", body_text)
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "code": "voice_command_failed",
                        "message": "commands without a provider call id",
                    }
                },
            )
        return JSONResponse(status_code=200, content={"status": "ok"})
    return Response(content=rendered, media_type="application/xml")


async def _handle_voice_webhook(
    carrier_name: str, request: Request, session: AsyncSession
) -> Response:
    settings = request.app.state.settings
    registry = getattr(request.app.state, "carriers", None)
    carrier = registry.get(carrier_name) if registry else None
    if carrier is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "carrier_not_configured", "message": carrier_name}},
        )

    raw = await request.body()
    db_account = None
    if not carrier.verify_voice_webhook(request.headers, raw):
        # 1.3: fall back to any org's own DB carrier credentials that verify this
        # request, PINNED to that account's org - the webhook must never be allowed to
        # ingest under whatever org the payload happens to resolve to.
        result = await _db_account_carrier_verifying_with_account(
            session, settings, carrier_name, request.headers, raw
        )
        if result is None:
            # Bandwidth retrying a 401 for 24h is their problem; accepting an
            # unauthenticated voice event would be ours.
            return JSONResponse(
                status_code=401,
                content={
                    "error": {"code": "unauthenticated", "message": "Invalid webhook signature"}
                },
            )
        carrier, db_account = result

    body_text = raw.decode("utf-8", errors="replace")
    try:
        events = carrier.parse_voice_webhook(raw)
    except ValueError as exc:
        # 1.10: malformed voice webhook must be stored/dead-lettered, never answered
        # with an empty BXML that hangs the caller up silently.
        await svc.dead_letter(session, carrier_name, "malformed_voice_webhook", body_text)
        log.warning("voice_webhook_malformed", carrier=carrier_name, reason=str(exc))
        return JSONResponse(status_code=200, content={"status": "dead_lettered"})

    store = getattr(request.app.state, "media_store", None)
    commands: list = []
    commands_provider_call_id: str | None = None
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

            if db_account is not None and org_id != db_account.org_id:
                # 1.3: the verifying account is not the payload's org. Do not ingest
                # under either org - dead-letter and ack so the carrier stops retrying.
                await svc.dead_letter(session, carrier_name, "webhook_org_mismatch", body_text)
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
                # P12: a number bound to an active call flow (org_numbers.call_flow_id) runs
                # it through the carrier executor instead of the flat default below.
                bound_flow = await routing_exec_svc.resolve_inbound_flow(session, call.our_e164)
                extra = call.extra or {}
                if not extra.get("refused") and not extra.get("credit_checked"):
                    # Billing v2 hard stop, decided ONCE per call: a redelivered
                    # call_initiated must never turn an allowed (maybe answered) call
                    # into a refused one.
                    if await _tb.inbound_call_allowed(
                        session, org_id, call.carrier or carrier_name
                    ):
                        call.extra = {**extra, "credit_checked": True}
                    else:
                        await _tb.refuse_inbound_call(session, call)
                if (call.extra or {}).get("refused"):
                    commands = [Hangup()]
                elif bound_flow is None:
                    commands = list(DEFAULT_INBOUND_COMMANDS)
                else:
                    commands = await routing_exec_svc.start_carrier_flow(
                        session, getattr(request.app.state, "event_bus", None), call, bound_flow
                    )
                commands_provider_call_id = event.provider_call_id
            elif (
                event.event_type == "dtmf_received"
                and call is not None
                and call.direction == "inbound"
            ):
                # P12: a digit webhook for a call whose flow is awaiting one advances it -
                # a no-op (commands left untouched) for every call with no active flow.
                flow_commands = await routing_exec_svc.continue_carrier_flow(
                    session, getattr(request.app.state, "event_bus", None), call, event
                )
                if flow_commands is not None:
                    commands = flow_commands
                    commands_provider_call_id = event.provider_call_id
            elif (
                event.event_type == "call_answered"
                and call is not None
                and call.direction == "outbound"
            ):
                # F3+F5: an outbound call answering is the one place a `record` flag turns
                # into a StartRecording, and (Bandwidth only) the one place the line must be
                # deliberately held open past the answer webhook.
                inline_commands = carrier.render_commands([]) is not None
                org_row = await session.get(Org, org_id)
                outbound_commands = _outbound_answer_commands(
                    call, org_row, needs_pause=inline_commands
                )
                if inline_commands:
                    commands = outbound_commands
                elif outbound_commands:
                    await carrier.execute_commands(event.provider_call_id, outbound_commands)
        except TelnyxVoiceCommandError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": {"code": "voice_command_failed", "message": str(exc)}},
            )
        except _VoiceRetrySignal:
            retry_needed = True
            continue
        except Exception:
            # F4: one bad event must never 500 the whole ack - EXCEPT the deliberate RETRY
            # signal above, which this shield intentionally does not catch.
            await session.rollback()
            log.exception(
                "voice_webhook_event_failed",
                carrier=carrier_name,
                event_type=getattr(event, "event_type", None),
            )
            continue

    if retry_needed:
        return Response(status_code=500, content=b"retry")
    try:
        return await _voice_bxml_response(
            carrier,
            commands,
            commands_provider_call_id,
            session=session,
            carrier_name=carrier_name,
            body_text=body_text,
        )
    except TelnyxVoiceCommandError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "voice_command_failed", "message": str(exc)}},
        )


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


async def _signalwire_placed_call(to: str, from_: str) -> bool:
    """Is there an outbound call from `from_` to `to` that our app placed in the last five
    minutes and that has not ended? One indexed lookup, own short session."""
    from datetime import datetime, timedelta, timezone

    from app.db.session import get_sessionmaker
    from app.models import Call

    async with get_sessionmaker()() as session:
        placed = (
            await session.execute(
                sa.select(Call.id)
                .where(
                    Call.direction == "outbound",
                    Call.contact_e164 == to,
                    Call.our_e164 == from_,
                    Call.ended_at.is_(None),
                    Call.created_at >= datetime.now(timezone.utc) - timedelta(minutes=5),
                )
                .limit(1)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
    return placed is not None


# Literal path, declared BEFORE the parameterised /{carrier_name}/voice below: that route
# would otherwise be the first match for "signalwire/sip-dial" and try to verify it as an
# ordinary SignalWire carrier callback, which is not the question SignalWire is asking.
@router.post("/signalwire/sip-dial")
async def signalwire_sip_dial(request: Request) -> Response:
    """Answer the LaML webhook for the SIP leg LiveKit opened to SignalWire.

    LiveKit INVITEs our SignalWire Domain Application, which then asks us what to do with
    the call and gives up with 480 if nobody answers. We answer with a <Dial> to the PSTN
    number, using our own SignalWire number as the caller id. No DB, no outbound I/O: this
    sits on the call path and SignalWire's timeout is short.
    """
    from app.providers.signalwire import sip_dial

    settings = request.app.state.settings
    raw = await request.body()

    token = settings.signalwire_api_token.get_secret_value()
    if not sip_dial.verify(
        request.headers, raw, sip_dial.signing_url(settings.public_base_url), token
    ):
        # Refused, never "handled anyway": this endpoint can make us pay for a phone call.
        # The shape of what SignalWire actually sent is logged (content type, which
        # signature header, body length and the form field NAMES) because a mismatch is
        # otherwise undebuggable; no values and no secrets.
        log.warning(
            "signalwire_sip_dial_unverified",
            content_type=request.headers.get("content-type", ""),
            sig_headers=[
                name
                for name in request.headers.keys()
                if "signature" in name.lower()
            ],
            body_len=len(raw),
            field_names=sorted(
                name for name, _ in parse_qsl(raw.decode("utf-8", "replace"))
            ),
            # A label for which header/url/hash combination matched, never a signature.
            signed_candidate=sip_dial.diagnose(
                request.headers, raw, settings.public_base_url, token
            ),
        )
        return Response(content=b"", media_type="application/xml", status_code=403)

    fields = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    to = fields.get("To", "")
    from_ = fields.get("From", "")
    call_sid = fields.get("CallSid", "")

    if not (sip_dial.is_e164(to) and sip_dial.is_e164(from_)):
        # The numbers ARE logged: the caller id is our own org's number and a refused call
        # is undebuggable without the pair.
        log.warning("signalwire_sip_dial_bad_numbers", call_sid=call_sid, to=to, from_=from_)
        return Response(content=b"", media_type="application/xml", status_code=403)

    # Billing v2 overuse guard: only bridge a call OUR app just placed (start_room_call
    # commits the Call row - after the credit gate and hold - before it dials). Anything
    # else reaching SignalWire's domain app would be a call nobody paid for.
    if not await _signalwire_placed_call(to, from_):
        log.warning("signalwire_sip_dial_no_placed_call", call_sid=call_sid, to=to, from_=from_)
        return Response(content=b"", media_type="application/xml", status_code=403)

    log.info("signalwire_sip_dial", call_sid=call_sid, to=to, from_=from_)
    return Response(content=sip_dial.build_laml(to, from_), media_type="application/xml")


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
        await voice_service.handle_livekit_event(
            session, bus, event, api=getattr(request.app.state, "livekit", None)
        )
    except Exception:
        # F4's LiveKit sibling: one bad event must never fail the ack - LiveKit has no
        # documented retry contract to lean on here, so swallowing (not 500ing) is the
        # safer default rather than inviting an infinite redelivery loop.
        log.exception("livekit_webhook_event_failed", event_type=event.get("event"))

    try:
        await supervisor_svc.enforce_coaching_privacy(
            session, getattr(request.app.state, "livekit", None), event
        )
    except Exception:  # noqa: BLE001 - enforcement must never fail the webhook ack
        log.exception("coaching_enforcement_failed", event_type=event.get("event"))

    try:
        await assistant_dispatch.on_livekit_event(
            session, request.app.state.livekit, settings, event
        )
    except Exception:  # noqa: BLE001 - the ack must not depend on a dispatch
        log.exception("assistant_dispatch_hook_failed", event_type=event.get("event"))

    # P43: monitored softphone calls get the silent call-monitor listener.
    try:
        from app.services import monitor_calls

        await monitor_calls.on_livekit_event(session, request.app.state.livekit, settings, event)
    except Exception:  # noqa: BLE001 - monitoring must never fail the webhook ack
        log.exception("call_monitor_hook_failed", event_type=event.get("event"))

    return JSONResponse(status_code=200, content={"status": "ok"})


@router.post("/didit")
async def didit_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """P44: Didit identity verification results.

    Authenticity, freshness and idempotency are all settled before any KYC state moves:
    didit_client.verify_webhook proves the payload (not just the envelope) came from Didit
    and is inside the replay window, and the event ledger below makes a retried delivery a
    no-op. Didit retries at ~1 and ~4 minutes on 5xx/404/timeout and then stops, so a
    handler that raises leaves no ledger row and the retry is processed normally.
    """
    payload_bytes = await request.body()
    payload = didit_client.verify_webhook(
        request.app.state.settings, payload_bytes, request.headers
    )

    event_id = payload.get("event_id")
    if event_id:
        from datetime import datetime, timezone

        from sqlalchemy.exc import IntegrityError

        from app.models import IdentityWebhookEvent

        ledger_id = f"didit:{event_id}"[:320]
        if await session.get(IdentityWebhookEvent, ledger_id) is not None:
            return Response(status_code=204)
        try:
            async with session.begin_nested():
                session.add(
                    IdentityWebhookEvent(
                        id=ledger_id,
                        provider="didit",
                        event_type=str(payload.get("webhook_type") or "")[:128] or None,
                        received_at=datetime.now(timezone.utc),
                    )
                )
                await session.flush()
        except IntegrityError:
            return Response(status_code=204)
    else:
        # A payload with no event_id cannot be deduplicated, so it is processed without a
        # ledger row rather than dropped - exactly what the Stripe endpoint does. The
        # handlers below are themselves write-idempotent (a verified person stays
        # verified), which is what keeps that safe.
        log.warning("didit_webhook_without_event_id", session=str(payload.get("session_id")))

    from app.services import kyc as kyc_svc

    await kyc_svc.handle_didit_event(session, request.app.state.settings, payload)
    await session.commit()
    return Response(status_code=204)


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    if not stripe_signature:
        raise UnauthenticatedError("We could not verify that this came from our payment provider.")

    payload = await request.body()
    event, secret_source = stripe_client.verify_webhook_any(
        request.app.state.settings,
        payload,
        stripe_signature,
    )

    # P41: durable replay protection. The ledger row commits with whatever the event
    # changed, so a failed handler leaves no row and Stripe's retry is processed normally.
    event_id = event.get("id")
    event_type = str(event.get("type") or "")
    # P43 (audit): the Identity endpoint's secret signs Identity events and nothing else.
    # The billing secret still signs anything, because with no separate Identity endpoint
    # configured Stripe legitimately delivers identity.* to the main one - but the reverse
    # (an Identity secret vouching for a refund or a payment) is never legitimate.
    if secret_source == "identity" and not event_type.startswith("identity."):
        log.warning("stripe_webhook_secret_mismatch", event_type=event_type)
        raise UnauthenticatedError("We could not verify that this came from our payment provider.")
    if event_id:
        from datetime import datetime, timezone

        from sqlalchemy.exc import IntegrityError

        from app.models import StripeEvent

        if await session.get(StripeEvent, event_id) is not None:
            return Response(status_code=204)
        try:
            async with session.begin_nested():
                session.add(
                    StripeEvent(
                        id=str(event_id)[:255],
                        type=event_type[:128],
                        received_at=datetime.now(timezone.utc),
                    )
                )
                await session.flush()
        except IntegrityError:
            return Response(status_code=204)

    if event_type.startswith("identity.verification_session."):
        from app.services import kyc as kyc_svc

        await kyc_svc.handle_identity_event(session, request.app.state.settings, event)
        await session.commit()
        return Response(status_code=204)

    from app.services import number_purchases, tendlc

    if await tendlc.handle_event(session, event):
        return Response(status_code=204)

    if await number_purchases.handle_event(session, request, event):
        return Response(status_code=204)

    if event_type in subscriptions_svc.HANDLED_EVENT_TYPES:
        # Replay protection is the StripeEvent ledger above - a second delivery of the same
        # event id already returned 204 and never reached here, so the handler itself does
        # not need (and must not add) a second idempotency key.
        await subscriptions_svc.handle_event(session, event)
        await session.commit()
        return Response(status_code=204)

    from app.services import card_risk

    if event_type in card_risk.HANDLED_EVENT_TYPES:
        # P44c: early fraud warnings and chargebacks. Replay-safe through the StripeEvent
        # ledger above and the per-reference ledger guards inside.
        await card_risk.handle_stripe_event(session, request.app.state.settings, event)
        await session.commit()
        return Response(status_code=204)

    if event_type != "payment_intent.succeeded":
        if event_id:
            await session.commit()
        return Response(status_code=204)

    intent = event.get("data", {}).get("object", {})
    metadata = intent.get("metadata") or {}
    from app.services import payments as payments_svc

    if await payments_svc.handle_bundle_intent(session, intent):
        return Response(status_code=204)
    if metadata.get("kind") != "credit_topup":
        log.warning(
            "stripe_webhook_unexpected_intent",
            event_type=event.get("type"),
            metadata=list(metadata.keys()),
        )
        return Response(status_code=204)

    org_id_str = metadata.get("org_id")
    try:
        org_id = uuid.UUID(org_id_str)
    except (TypeError, ValueError):
        log.warning("stripe_webhook_invalid_org", metadata=metadata)
        return Response(status_code=204)

    org = await session.get(Org, org_id)
    if org is None:
        log.warning("stripe_webhook_unknown_org", org_id=str(org_id))
        return Response(status_code=204)

    intent_id = intent.get("id")
    try:
        amount_received = int(intent.get("amount_received", 0))
    except (TypeError, ValueError):
        amount_received = 0

    if not intent_id or amount_received <= 0:
        log.warning(
            "stripe_webhook_unattributable_intent",
            org_id=str(org_id),
            metadata=metadata,
        )
        return Response(status_code=204)

    set_org_context(session, org_id)

    # credits.topup is idempotent on (org, entry_type='topup', reference=intent id),
    # so a replayed event is a no-op rather than a double credit.
    await credits.topup(
        session,
        org_id,
        amount_micros=amount_received * 10_000,
        reference=intent_id,
        note="Card payment",
    )
    await payments_svc.record_topup_paid(
        session,
        org_id,
        intent_id=intent_id,
        amount_micros=amount_received * 10_000,
        kind="auto_recharge" if metadata.get("source") == "auto_recharge" else "topup",
    )
    await session.commit()

    # The sweeper owns low-balance warnings. Calling check_balance_warnings here
    # would email on every replayed/retried webhook inside the request path.
    return {"ok": True}
