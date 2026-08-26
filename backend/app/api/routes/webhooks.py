"""Carrier webhook ingestion.

No JWT and no X-Org-Id: the carrier's HTTP Basic credentials are the only gate. Everything
this endpoint does is DB work — no external I/O of any kind — which is what keeps the ack
far inside Bandwidth's timeout without needing a queue.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.providers.bandwidth import webhooks as bw_webhooks
from app.services import messaging as svc

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
log = structlog.get_logger("webhooks")

CARRIER = "bandwidth"


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
